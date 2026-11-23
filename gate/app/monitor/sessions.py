"""会话监视：等待确认（纳管/非纳管）与完成汇总（设计 7 节、D9）。

信号来源：Orca 的 agent 状态钩子（`worktree ps` 的 agents[]，见 notes/p2-detect-spike.md）。
- 等待确认：state ∈ {blocked, waiting}；"超过 5 分钟"用 Orca 记录的 stateStartedAt 计时，网关重启不丢。
- 纳管会话：进 /sessions/pending（P3 的自动确认从这里取），并提醒用户；
  非纳管会话：只提醒用户，永远不进 pending、不代答（D9）。
- 同一等待回合（句柄 + 状态起始时间）只提醒一次，状态改变后回合结束。
- 完成：同一终端状态从 working 变 done，攒进汇总，每个窗口合并发一条。
- 第一次成功采集只建基线，存量状态不推送。Orca 不可用时本周期只记录，不抛异常。
"""
import json
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from ..orca import OrcaError, OrcaUnavailable
from ..redact import sanitize
from .store import MonitorStore
from .timefmt import hhmm_cst, iso_utc

WAIT_STATES = ("blocked", "waiting")
DIGEST_LINES = 10
PRUNE_AFTER = 3        # 纳管句柄连续这么多次采集都不在存活列表里，才从名单移出


def _s(value, limit: int) -> str:
    return sanitize(value if isinstance(value, str) else "", limit)[0]


class SessionsMonitor:
    def __init__(self, orca, store: MonitorStore, notifier, now: Callable[[], datetime],
                 wait_threshold_s: int = 300, digest_window_s: int = 600, prune_after: int = PRUNE_AFTER):
        self._orca, self._store, self._n, self._now = orca, store, notifier, now
        self._threshold, self._window, self._prune_after = wait_threshold_s, digest_window_s, prune_after
        self.last: Dict = {"collected_at": None, "orca": "not-polled", "pending": [], "agents": 0}

    # ---- 持久化的小状态（JSON 放在键值表里）----
    def _kv_json(self, key: str, default):
        raw = self._store.kv_get(key)
        try:
            return json.loads(raw) if raw else default
        except ValueError:
            return default

    def _prune_managed(self, alive) -> None:
        """纳管名单清理：句柄连续 N 次缺席才移出；Orca 偶尔返回空列表（alive 为空而名单非空）时跳过本次，不累计。"""
        managed = [m["handle"] for m in self._store.managed_list()]
        alive = set(alive)
        if not managed:
            self._store.kv_set("managed_absent", "{}")
            return
        if not alive:
            return
        absent = self._kv_json("managed_absent", {})
        keep: Dict[str, int] = {}
        for h in managed:
            if h in alive:
                continue
            n = int(absent.get(h, 0)) + 1
            if n >= self._prune_after:
                self._store.managed_remove(h)
            else:
                keep[h] = n
        self._store.kv_set("managed_absent", json.dumps(keep))

    def _remind_text(self, a: Dict, waited_s: float, managed: bool, when: str) -> str:
        detail = " ".join(x for x in (_s(a.get("tool"), 60), _s(a.get("tool_input"), 200) or _s(a.get("prompt"), 200)) if x)
        head = f"会话「{_s(a.get('title'), 80)}」（{_s(a.get('agent'), 20)}）已等待确认 {int(waited_s // 60)} 分钟"
        tail = "（已纳管）" if managed else "，未纳管，助手不会代答，请你处理"
        return f"{head}{tail}：{detail}（采集于 {when}）" if detail else f"{head}{tail}（采集于 {when}）"

    def poll(self) -> Dict:
        now = self._now()
        snap: Dict = {"collected_at": iso_utc(now), "orca": "ok", "pending": [], "agents": 0}
        try:
            data = self._orca.agent_snapshot()
        except OrcaUnavailable:
            snap["orca"] = "unavailable"
            self.last = snap
            return snap
        except OrcaError:
            snap["orca"] = "error"
            self.last = snap
            return snap
        baseline = self._store.kv_get("sessions_baseline_done") is None
        when = hhmm_cst(now)
        self._prune_managed(data["alive"])
        prev: Dict[str, Dict] = self._kv_json("sess_prev", {})
        cur_prev: Dict[str, Dict] = {}
        digest: List[Dict] = self._kv_json("sess_digest", [])
        episodes = set()
        for a in data["agents"]:
            handle, state = a["handle"], a.get("state")
            since_ms = a.get("state_since_ms") or 0
            cur_prev[handle] = {"state": state, "since": since_ms}
            snap["agents"] += 1
            if state in WAIT_STATES and since_ms:
                since = datetime.fromtimestamp(since_ms / 1000, timezone.utc)
                waited = (now - since).total_seconds()
                if waited >= self._threshold:
                    ep = f"wait:{handle}@{since_ms}"
                    episodes.add(ep)
                    managed = self._store.is_managed(handle)
                    if managed:
                        snap["pending"].append({
                            "id": handle, "title": _s(a.get("title"), 200), "agent": a.get("agent"), "state": state,
                            "waiting_for_s": int(waited), "prompt": _s(a.get("prompt"), 300),
                            "tool": _s(a.get("tool"), 60), "tool_input": _s(a.get("tool_input"), 300),
                        })
                    if self._store.get_alert(ep) is None:
                        if baseline:
                            self._store.set_alert(ep, True, iso_utc(now), False)    # 存量：静默记下
                        else:
                            self._n.notify("session", ep, self._remind_text(a, waited, managed, when))
                            self._store.set_alert(ep, True, iso_utc(now), True)
            p = prev.get(handle)
            if not baseline and p and p.get("state") == "working" and state == "done":
                digest.append({"ts": iso_utc(now), "title": _s(a.get("title"), 80), "agent": _s(a.get("agent") or "", 20)})
        for al in self._store.all_alerts():                                       # 清掉已结束回合的记录
            if al["key"].startswith("wait:") and al["key"] not in episodes:
                self._store.delete_alert(al["key"])
        # 完成汇总：最早一条攒满一个窗口就合并发一条
        if digest:
            oldest = datetime.fromisoformat(digest[0]["ts"])
            if (now - oldest).total_seconds() >= self._window:
                lines = [f"- {d['title'] or '(无标题)'}（{d['agent']}）" for d in digest[:DIGEST_LINES]]
                more = len(digest) - DIGEST_LINES
                text = f"{len(digest)} 个会话已完成（采集于 {when}）：\n" + "\n".join(lines) + (f"\n……还有 {more} 个" if more > 0 else "")
                self._n.notify("digest", "digest", text, urgent=False)
                digest = []
        self._store.kv_set("sess_prev", json.dumps(cur_prev))
        self._store.kv_set("sess_digest", json.dumps(digest, ensure_ascii=False))
        if baseline:
            self._store.kv_set("sessions_baseline_done", iso_utc(now))
        self.last = snap
        return snap

    def pending_snapshot(self) -> Dict:
        return {"collected_at": self.last["collected_at"], "orca": self.last["orca"], "items": self.last["pending"]}
