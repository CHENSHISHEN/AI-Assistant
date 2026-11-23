"""应用监视：只读 Workbench，应用挂了/恢复才通知（设计 7 节、D5）。

- Workbench 客户端只有两个只读方法，且只允许 GET /api/status 与 GET /api/entries；
  没有任何发送 POST/PATCH/DELETE 的代码路径。
- 连续 2 次异常才算状态变化（Debouncer）；变化才通知；同一故障恢复之前只发一次。
- Workbench 自己连不上/返回非 JSON/数据过期：走"不可达/过期"分支，不把旧数据当现在的状态。
- 第一次成功采集只建立基线，不通知。
"""
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, Optional

import re

import httpx

from ..config import Settings
from .debounce import Debouncer
from .store import MonitorStore
from .timefmt import hhmm_cst, iso_utc

STALE_AFTER = timedelta(minutes=5)
_ALLOWED_PATHS = ("/api/status", "/api/entries")
WB_KEY = "workbench"


class WorkbenchUnreachable(Exception):
    pass


class WorkbenchRejected(Exception):
    """Workbench 能连上，但拒绝了请求（4xx，例如条目不存在）。"""
    def __init__(self, status: int):
        super().__init__(f"HTTP {status}")
        self.status = status


class ActionsDisabled(Exception):
    """应用启停开关关着：不发任何请求。"""


_APP_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_LOGS_PATH_RE = re.compile(r"^/api/entries/[a-z0-9][a-z0-9-]{0,63}/logs$")
VERBS = ("start", "stop", "restart")


class WorkbenchClient:
    def __init__(self, base_url: str, client: Optional[httpx.Client] = None, timeout: float = 3.0):
        self._base = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=timeout)
        self._timeout = timeout

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        if path not in _ALLOWED_PATHS and not _LOGS_PATH_RE.match(path):      # 只读白名单
            raise ValueError("不允许的路径")
        try:
            r = self._client.get(self._base + path, params=params, timeout=self._timeout)
            if 400 <= r.status_code < 500:
                raise WorkbenchRejected(r.status_code)
            if r.status_code != 200:
                raise WorkbenchUnreachable(f"HTTP {r.status_code}")
            data = r.json()
        except (WorkbenchUnreachable, WorkbenchRejected):
            raise
        except Exception as e:  # noqa: BLE001  连接失败、超时、非 JSON
            raise WorkbenchUnreachable(type(e).__name__) from None
        if not isinstance(data, dict):
            raise WorkbenchUnreachable("unexpected body")
        return data

    def status(self) -> dict:
        d = self._get("/api/status")
        states = d.get("states")
        if not isinstance(states, dict):
            raise WorkbenchUnreachable("unexpected body")
        return {"states": {str(k): str(v) for k, v in states.items()}, "checked_at": d.get("checked_at")}

    def logs(self, app_id: str, tail: int = 200) -> str:
        """GET /api/entries/{id}/logs?tail=N（不带 follow）。"""
        if not isinstance(app_id, str) or not _APP_ID_RE.match(app_id):
            raise ValueError("非法的应用 id")
        d = self._get(f"/api/entries/{app_id}/logs", params={"tail": max(1, min(int(tail), 1000))})
        return str(d.get("logs", ""))

    def entries(self) -> Dict[str, str]:
        d = self._get("/api/entries")
        return {str(e["id"]): str(e.get("name") or e["id"]) for e in d.get("entries", []) if isinstance(e, dict) and "id" in e}


class WorkbenchActuator:
    """应用启停的**写**客户端，与只读的 WorkbenchClient 分开。

    enabled 为 False（默认）时不发任何请求；只能发 POST /api/entries/{id}/{start|stop|restart}，不带 Origin。
    调用它之前必须已经过审批流。
    """

    def __init__(self, base_url: str, enabled: bool, client: Optional[httpx.Client] = None, timeout: float = 30.0):
        self._base, self._enabled = base_url.rstrip("/"), bool(enabled)
        self._client, self._timeout = client or httpx.Client(timeout=timeout), timeout

    @property
    def enabled(self) -> bool:
        return self._enabled

    def act(self, app_id: str, verb: str) -> dict:
        if not self._enabled:
            raise ActionsDisabled()
        if verb not in VERBS or not isinstance(app_id, str) or not _APP_ID_RE.match(app_id):
            raise ValueError("非法的应用或动作")
        try:
            r = self._client.post(f"{self._base}/api/entries/{app_id}/{verb}", timeout=self._timeout)
        except Exception as e:  # noqa: BLE001
            raise WorkbenchUnreachable(type(e).__name__) from None
        if 400 <= r.status_code < 500:
            raise WorkbenchRejected(r.status_code)
        if r.status_code != 200:
            raise WorkbenchUnreachable(f"HTTP {r.status_code}")
        return {"ok": True}


def _parse(ts) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(str(ts))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


class AppsMonitor:
    def __init__(self, wb: Optional[WorkbenchClient], debouncer: Debouncer, store: MonitorStore, notifier,
                 settings: Settings, now: Callable[[], datetime]):
        self._wb, self._deb, self._store, self._n, self._s, self._now = wb, debouncer, store, notifier, settings, now

    def restore(self) -> None:
        """从库恢复已确认的告警状态（重启后不重复通知）。"""
        for a in self._store.all_alerts():
            if a["key"] == WB_KEY or a["key"].startswith("app:"):
                self._deb.restore(a["key"], a["bad"])

    def _event(self, key: str, ev: Optional[str], now: datetime, text_down: str, text_up: str, notify: bool) -> None:
        if ev == "down":
            sent = False
            if notify:
                self._n.notify("app", key, text_down)
                sent = True
            self._store.set_alert(key, True, iso_utc(now), sent)
        elif ev == "up":
            prev = self._store.get_alert(key)
            if notify and prev and prev["notified"]:       # 没报过故障就不报恢复
                self._n.notify("app", key, text_up)
            self._store.set_alert(key, False, iso_utc(now), False)

    def poll(self) -> dict:
        now = self._now()
        snap: Dict = {"collected_at": iso_utc(now), "workbench": "disabled", "apps": {}}
        if self._wb is None:
            return snap
        try:
            st = self._wb.status()
        except WorkbenchUnreachable:
            ev = self._deb.observe(WB_KEY, True)
            snap["workbench"] = "unreachable" if self._deb.state(WB_KEY) else "retrying"
            self._event(WB_KEY, ev, now, "Workbench 不可达，应用状态暂时看不到了", "Workbench 已恢复可达",
                        self._s.notify_workbench_down)
            return snap
        ev = self._deb.observe(WB_KEY, False)             # 能连上 = 好
        self._event(WB_KEY, ev, now, "", f"Workbench 已恢复可达（采集于 {hhmm_cst(now)}）", self._s.notify_workbench_down)
        checked = _parse(st.get("checked_at"))
        if checked is None or now - checked > STALE_AFTER:
            snap["workbench"] = "stale"                    # 数据过期：不做判定，不通知
            return snap
        snap["workbench"] = "ok"
        try:
            names = self._wb.entries()
        except WorkbenchUnreachable:
            names = {}
        when = hhmm_cst(checked)
        baseline = self._store.kv_get("apps_baseline_done") is None
        for app_id, state in sorted(st["states"].items()):
            snap["apps"][app_id] = {"name": names.get(app_id, app_id), "state": state}
            if state not in ("running", "stopped"):
                continue                                   # unknown / none 不参与判定
            key, bad = f"app:{app_id}", state == "stopped"
            if baseline:                                   # 基线：只记录，不通知
                self._deb.restore(key, bad)
                self._store.set_alert(key, bad, iso_utc(now), False)
                continue
            name = names.get(app_id, app_id)
            self._event(key, self._deb.observe(key, bad), now,
                        f"应用 {name} 已停止（Workbench 采集于 {when}）",
                        f"应用 {name} 已恢复（Workbench 采集于 {when}）", True)
        if baseline:
            self._store.kv_set("apps_baseline_done", iso_utc(now))
        return snap
