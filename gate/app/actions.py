"""动手能力的策略层：会话回复（含纳管自动确认）。应用动作见 app_actions.py。

策略矩阵（设计 6.1、D4、D9）：
  forbidden                     -> 403，审批也不放行
  dangerous（回复或请求内容）   -> 审批
  纳管 ∧ 正在等待确认 >= 阈值 ∧ 未超每小时上限 -> 自动发送（审计 auto）
  其他（非纳管、纳管但没到阈值、超限）-> 默认走审批；GATE_USER_DIRECTED_REPLY=trust-flag 时，
                                         模型带 user_requested=true 才直接发（仍记审计）
危险判断的文本 = 回复 + 会话正在请求确认的内容（agent 状态里的 tool/tool_input/prompt）+ 屏幕尾部 40 行。
"""
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, Tuple

from . import danger
from .config import Settings
from .monitor.sessions import WAIT_STATES
from .monitor.timefmt import iso_utc
from .orca import OrcaError, OrcaUnavailable, valid_handle
from .redact import sanitize

SCREEN_LINES = 40


class ReplyService:
    def __init__(self, orca, store, approvals, audit, settings: Settings, now: Callable[[], datetime]):
        self._orca, self._store, self._appr, self._audit, self._s, self._now = orca, store, approvals, audit, settings, now

    def executors(self) -> Dict[str, Callable[[dict], dict]]:
        return {"reply": self.execute_reply}

    # ---- 内部 ----
    def _screen(self, handle: str) -> str:
        try:
            t = self._orca.read_terminal(handle, limit=SCREEN_LINES)
        except OrcaUnavailable:
            raise
        except OrcaError:
            return ""
        return "\n".join(str(x) for x in (t.get("tail") or []))

    def _waiting_long_enough(self, row: Dict) -> bool:
        if not row or row.get("state") not in WAIT_STATES or not row.get("state_since_ms"):
            return False
        since = datetime.fromtimestamp(row["state_since_ms"] / 1000, timezone.utc)
        return (self._now() - since).total_seconds() >= self._s.wait_threshold_s

    def _auto_count_last_hour(self, handle: str) -> int:
        since = iso_utc(self._now() - timedelta(hours=1))
        return sum(1 for r in self._store.audit_rows("reply", "auto", since) if r["target"] == handle)

    def _approval(self, handle: str, title: str, text: str, why: str, rules) -> Tuple[int, Dict]:
        summary = f"向会话「{sanitize(title, 60)[0]}」发送：{sanitize(text, 100)[0]}（{why}）"
        r = self._appr.create("reply", summary, {"handle": handle, "text": text}, "reply")
        self._audit.write("openclaw", "reply", handle, "approval-required", rules=rules, detail=text)
        return 202, {"status": "approval_required", **r,
                     "message": "审批单已发到用户私聊；请等用户回复「批准 <审批单ID> <码>」，不要重复创建。"}

    # ---- 对外 ----
    def reply(self, handle: str, text: str, user_requested: bool = False) -> Tuple[int, Dict]:
        text = danger.sanitize_reply(text)                       # ValueError -> 422
        if not valid_handle(handle):
            raise ValueError("非法的终端句柄")
        snap = self._orca.agent_snapshot()
        if handle not in snap["alive"]:
            self._audit.write("openclaw", "reply", handle, "denied", rules=("no-such-session",), detail=text)
            return 404, {"error": "not_found", "detail": "会话不存在"}
        row = next((a for a in snap["agents"] if a["handle"] == handle), None) or {}
        title = row.get("title") or handle
        verdict = danger.check(text, row.get("tool"), row.get("tool_input"), row.get("prompt"), self._screen(handle))
        if verdict.level == "forbidden":
            self._audit.write("openclaw", "reply", handle, "forbidden", rules=verdict.rules, detail=text)
            return 403, {"error": "forbidden", "detail": "这类操作不允许由助手执行，审批也不放行", "rules": list(verdict.rules)}
        if verdict.level == "dangerous":
            return self._approval(handle, title, text, "命中危险规则：" + ",".join(verdict.rules), verdict.rules)
        if (self._store.is_managed(handle) and self._waiting_long_enough(row)
                and self._auto_count_last_hour(handle) < self._s.reply_per_hour):
            self._orca.send_text(handle, text)
            self._audit.write("openclaw", "reply", handle, "auto", detail=text)
            return 200, {"status": "sent", "mode": "auto"}
        if self._s.user_directed_reply == "trust-flag":
            if user_requested:
                self._orca.send_text(handle, text)
                self._audit.write("openclaw", "reply", handle, "auto", detail="user_requested: " + text)
                return 200, {"status": "sent", "mode": "user-requested"}
            self._audit.write("openclaw", "reply", handle, "denied", rules=("not-allowed",), detail=text)
            return 403, {"error": "not_allowed", "detail": "非纳管或未到等待阈值的会话，只有用户明确要求时才能回复"}
        why = "未纳管会话" if not self._store.is_managed(handle) else "纳管但未到等待阈值"
        return self._approval(handle, title, text, why + "，需用户批准", ())

    def execute_reply(self, payload: dict) -> dict:
        """审批通过后的执行：再次确认会话仍在、规则仍通过，才真正发送。"""
        handle, text = payload["handle"], payload["text"]
        if handle not in self._orca.agent_snapshot()["alive"]:
            raise RuntimeError("会话已不存在")
        if danger.check(text).level == "forbidden":
            raise RuntimeError("命中禁止规则")
        self._orca.send_text(handle, text)
        return {"sent": True}
