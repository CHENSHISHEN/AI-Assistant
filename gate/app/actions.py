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
from .approvals import ApprovalAbort
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
    def _screen(self, handle: str):
        """屏幕尾部文本；读取失败返回 None（调用方按"读不到上下文"处理，走审批）。Orca 不可用则抛出。"""
        try:
            t = self._orca.read_terminal(handle, limit=SCREEN_LINES)
        except OrcaUnavailable:
            raise
        except OrcaError:
            return None
        return "\n".join(str(x) for x in (t.get("tail") or []))

    @staticmethod
    def _request(row: Dict) -> Tuple[str, str]:
        """会话当前请求确认的内容（工具名、工具输入），脱敏限长后用于摘要和执行前比对。读不到则返回占位符。"""
        try:
            tin = danger.as_text(row.get("tool_input"))
        except ValueError:
            tin = "<unreadable>"
        return sanitize(danger.as_text(row.get("tool")) if row.get("tool") is not None else "", 60)[0], sanitize(tin, 300)[0]

    def _waiting_long_enough(self, row: Dict) -> bool:
        if not row or row.get("state") not in WAIT_STATES or not row.get("state_since_ms"):
            return False
        since = datetime.fromtimestamp(row["state_since_ms"] / 1000, timezone.utc)
        return (self._now() - since).total_seconds() >= self._s.wait_threshold_s

    def _auto_count_last_hour(self, handle: str) -> int:
        since = iso_utc(self._now() - timedelta(hours=1))
        return sum(1 for r in self._store.audit_rows("reply", "auto", since) if r["target"] == handle)

    def _approval(self, handle: str, title: str, text: str, why: str, rules, row: Dict) -> Tuple[int, Dict]:
        tool, tin = self._request(row)
        request = (f"会话请求：{tool} {tin[:60]}" if (tool or tin) else "会话没有请求内容")
        summary = f"向会话「{sanitize(title, 40)[0]}」发送：{sanitize(text, 60)[0]}（{request}；{why}）"
        payload = {"handle": handle, "text": text, "state_since_ms": row.get("state_since_ms"),
                   "tool": tool, "tool_input": tin, "rules": list(rules)}
        r = self._appr.create("reply", summary, payload, "reply")
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
        verdict = danger.check_context(text, row.get("tool_input"), row.get("prompt"), self._screen(handle), row.get("tool"))
        if verdict.level == "forbidden":
            self._audit.write("openclaw", "reply", handle, "forbidden", rules=verdict.rules, detail=text)
            return 403, {"error": "forbidden", "detail": "这类操作不允许由助手执行，审批也不放行", "rules": list(verdict.rules)}
        if verdict.level == "dangerous":
            return self._approval(handle, title, text, "命中危险规则：" + ",".join(verdict.rules), verdict.rules, row)
        if (self._store.is_managed(handle) and self._waiting_long_enough(row)
                and self._auto_count_last_hour(handle) < self._s.reply_per_hour):
            self._orca.send_text(handle, text)
            self._audit.write("openclaw", "reply", handle, "auto", detail=text)
            return 200, {"status": "sent", "mode": "auto"}
        if self._s.user_directed_reply == "trust-flag":
            if user_requested:
                self._orca.send_text(handle, text)
                # 单独的 decision：不计入自动确认的每小时额度，也不进自动确认汇总
                self._audit.write("openclaw", "reply", handle, "user-directed", detail=text)
                return 200, {"status": "sent", "mode": "user-requested"}
            self._audit.write("openclaw", "reply", handle, "denied", rules=("not-allowed",), detail=text)
            return 403, {"error": "not_allowed", "detail": "非纳管或未到等待阈值的会话，只有用户明确要求时才能回复"}
        why = "未纳管会话" if not self._store.is_managed(handle) else "纳管但未到等待阈值"
        return self._approval(handle, title, text, why + "，需用户批准", (), row)

    def execute_reply(self, payload: dict) -> dict:
        """审批通过后的执行：用**当前**快照重新核对，上下文变了就作废（ApprovalAbort，通知用户），不盲发。

        核对项：会话仍在；状态起点、工具名、工具输入与创建审批单时一致；对当前上下文重跑完整的 danger/forbidden 检查，
        出现禁止规则、或出现创建时没有的新危险规则，都作废。
        """
        handle, text = payload["handle"], payload["text"]
        snap = self._orca.agent_snapshot()
        if handle not in snap["alive"]:
            raise ApprovalAbort("会话已经不存在了")
        row = next((a for a in snap["agents"] if a["handle"] == handle), None) or {}
        tool, tin = self._request(row)
        if (row.get("state_since_ms") != payload.get("state_since_ms") or tool != payload.get("tool", "")
                or tin != payload.get("tool_input", "")):
            raise ApprovalAbort("会话的状态或请求内容在你批准之前已经变了，原审批单不再适用，请重新发起")
        verdict = danger.check_context(text, row.get("tool_input"), row.get("prompt"), self._screen(handle), row.get("tool"))
        if verdict.level == "forbidden":
            raise ApprovalAbort("命中禁止规则，不能执行")
        new = [r for r in verdict.rules if r not in (payload.get("rules") or [])]
        if new:
            raise ApprovalAbort("会话里出现了批准时没有的新危险内容（" + ",".join(new) + "），请重新发起")
        self._orca.send_text(handle, text)
        return {"sent": True}
