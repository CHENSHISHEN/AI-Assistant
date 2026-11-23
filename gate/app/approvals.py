"""审批流（设计 6.4）：模型无法自己批准自己。

1. 网关拦下操作，生成审批单和 6 位一次性码，10 分钟有效；
2. 网关**直接**用 Bot token 调 sendMessage 把说明和码发给用户私聊（Bot 收不到自己发的消息，模型看不到码）；
3. 用户回复"批准 <ID> <码>"，OpenClaw 把 ID 和码转给 confirm；核对一致才执行**创建时保存的**动作，只能用一次；
4. 错码 5 次作废该单；累计错误太多锁定审批；执行结果通知用户并写审计。

库里只存 HMAC(码)，不存明文码；任何查询接口都不返回码。
"""
import hashlib
import hmac
import json
import secrets
import string
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Mapping

from .audit import AuditLog
from .monitor.store import MonitorStore
from .monitor.timefmt import hhmm_cst, iso_utc
from .redact import sanitize

_ID_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # 去掉易混的 I/O/0/1


class ApprovalError(Exception):
    def __init__(self, status: int, code: str, message: str = ""):
        super().__init__(f"{code}: {message}")
        self.status, self.code, self.message = status, code, message


class ApprovalAbort(Exception):
    """执行器发现审批单所依据的上下文已变化：作废这张单、通知用户。message 是可以给用户看的受控文字。"""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class ApprovalService:
    def __init__(self, store: MonitorStore, notifier, audit: AuditLog, now: Callable[[], datetime], secret: bytes,
                 ttl_s: int = 600, max_attempts: int = 5, max_pending: int = 5,
                 lock_after_failures: int = 10, lock_s: int = 3600):
        self._store, self._n, self._audit, self._now, self._secret = store, notifier, audit, now, secret
        self._ttl, self._max_attempts, self._max_pending = ttl_s, max_attempts, max_pending
        self._lock_after, self._lock_s = lock_after_failures, lock_s

    def __repr__(self) -> str:
        return "ApprovalService()"

    # ---- 内部 ----
    def _mac(self, aid: str, code: str) -> str:
        return hmac.new(self._secret, f"{aid}:{code}".encode(), hashlib.sha256).hexdigest()

    def _locked(self) -> bool:
        until = self._store.kv_get("approvals_locked_until")
        return bool(until) and iso_utc(self._now()) < until

    # ---- 创建 ----
    def create(self, kind: str, summary: str, payload: dict, executor_key: str) -> Dict:
        now = self._now()
        if self._locked():
            raise ApprovalError(429, "approvals_locked", "审批因多次错误码被暂时锁定")
        if not self._n.delivers():              # dry-run：码发不出去，不能当作审批单创建成功
            self._audit.write("gate", kind, "-", "failed", detail="通知处于 dry-run，审批码无法送达，未创建审批单")
            raise ApprovalError(503, "approval_delivery_unavailable",
                                "通知目前是 dry-run 模式，审批码没法发给用户，所以没有创建审批单；请如实告诉用户")
        if len(self._store.approvals_pending(iso_utc(now))) >= self._max_pending:
            raise ApprovalError(429, "too_many_pending", "未决审批单太多")
        summary = sanitize(summary, 200)[0]
        for _ in range(20):
            aid = "".join(secrets.choice(_ID_ALPHABET) for _ in range(4))
            if self._store.approval_get(aid) is None:
                break
        code = f"{secrets.randbelow(10**6):06d}"
        expires = now + timedelta(seconds=self._ttl)
        self._store.approval_insert(aid, kind, summary, json.dumps(payload, ensure_ascii=False), executor_key,
                                    self._mac(aid, code), iso_utc(now), iso_utc(expires))
        text = (f"[审批] {summary}\n审批单 {aid}，批准码 {code}，{self._ttl // 60} 分钟内有效"
                f"（到 {hhmm_cst(expires)}）。\n要批准请回复：批准 {aid} {code}\n不是你要的操作就忽略，过期自动作废。")
        if not self._n.send_now("approval", aid, text):
            self._store.approval_update(aid, status="void", clear_code=True)
            self._audit.write("gate", kind, aid, "failed", detail="审批码消息发送失败，审批单作废")
            raise ApprovalError(502, "approval_delivery_failed", "审批消息没发出去，审批单已作废")
        self._audit.write("gate", kind, aid, "approval-required", detail=summary)
        return {"approval_id": aid, "expires_at": iso_utc(expires), "summary": summary}

    # ---- 确认 ----
    def confirm(self, approval_id: str, code: str, executors: Mapping[str, Callable[[dict], dict]]) -> Dict:
        now = self._now()
        if not isinstance(code, str) or not (code.isdigit() and len(code) == 6):
            raise ApprovalError(422, "invalid_code_format", "批准码是 6 位数字")
        if self._locked():
            raise ApprovalError(429, "approvals_locked", "审批因多次错误码被暂时锁定")
        row = self._store.approval_get(approval_id)
        if row is None:
            raise ApprovalError(404, "not_found", "审批单不存在")
        if row["status"] != "pending":
            raise ApprovalError(410, "gone", "审批单已使用或已作废")
        if iso_utc(now) >= row["expires_at"]:
            if self._store.approval_claim(approval_id, "pending", "expired"):
                self._store.approval_update(approval_id, clear_code=True)
                self._audit.write("gate", row["kind"], approval_id, "expired", detail=row["summary"])
            raise ApprovalError(410, "expired", "审批单已过期")
        if not hmac.compare_digest(self._mac(approval_id, code), row["code_hmac"]):
            return self._wrong(approval_id, row, now)
        if not self._store.approval_claim(approval_id, "pending", "executing"):   # 并发确认只有一个成功
            raise ApprovalError(410, "gone", "审批单已使用或已作废")
        self._audit.write("user", row["kind"], approval_id, "approved", detail=row["summary"])
        executor = executors.get(row["executor_key"])
        payload = json.loads(row["payload_json"])                                  # 只用创建时保存的动作
        try:
            if executor is None:
                raise RuntimeError("没有对应的执行器")
            result = executor(payload)
            self._store.approval_update(approval_id, status="executed", result_json=json.dumps(result, ensure_ascii=False), clear_code=True)
            self._audit.write("gate", row["kind"], approval_id, "executed", detail=row["summary"])
            self._n.send_now("approval-result", approval_id, f"[审批结果] 审批单 {approval_id} 已执行：{row['summary']}")
            return {"status": "executed", "result": result}
        except ApprovalAbort as e:                                                 # 上下文变了：作废并通知用户
            self._store.approval_update(approval_id, status="failed", result_json="{}", clear_code=True)
            self._audit.write("gate", row["kind"], approval_id, "failed", detail=e.message)
            self._n.send_now("approval-result", approval_id, f"[审批结果] 审批单 {approval_id} 已作废、没有执行：{e.message}")
            raise ApprovalError(409, "context_changed", e.message) from None
        except Exception as e:  # noqa: BLE001  不把异常文本（可能含路径、堆栈）返回给模型
            self._store.approval_update(approval_id, status="failed", result_json="{}", clear_code=True)
            self._audit.write("gate", row["kind"], approval_id, "failed", detail=f"{type(e).__name__}")
            self._n.send_now("approval-result", approval_id, f"[审批结果] 审批单 {approval_id} 执行失败：{row['summary']}（{type(e).__name__}）")
            raise ApprovalError(500, "execution_failed", "执行失败") from None

    def _wrong(self, aid: str, row: Dict, now: datetime):
        attempts = self._store.approval_bump_attempts(aid)
        self._store.approval_fail_add(iso_utc(now))
        self._audit.write("user", row["kind"], aid, "denied", rules=("wrong-code",), detail=f"第 {attempts} 次错误码")
        if attempts >= self._max_attempts:
            self._store.approval_update(aid, status="void", clear_code=True)
            self._audit.write("gate", row["kind"], aid, "denied", rules=("too-many-attempts",), detail="错误码达上限，审批单作废")
        recent = self._store.approval_fail_count(iso_utc(now - timedelta(hours=1)))
        if recent >= self._lock_after and not self._locked():
            self._store.kv_set("approvals_locked_until", iso_utc(now + timedelta(seconds=self._lock_s)))
            self._audit.write("gate", "approvals", "*", "locked", detail=f"一小时内累计 {recent} 次错误码")
            self._n.send_now("approval-lock", "lock", f"[审批] 一小时内出现 {recent} 次错误的批准码，审批已锁定 {self._lock_s // 60} 分钟。"
                                                      "如果这不是你操作的，说明有人（或模型）在猜码。")
        left = max(0, self._max_attempts - attempts)
        raise ApprovalError(403, "wrong_code", f"批准码不对（这张单还剩 {left} 次机会）" if left else "批准码不对，这张审批单已作废")

    # ---- 查询 / 取消 ----
    def pending(self) -> List[Dict]:
        return [{"approval_id": r["id"], "kind": r["kind"], "summary": r["summary"], "expires_at": r["expires_at"]}
                for r in self._store.approvals_pending(iso_utc(self._now()))]

    def cancel(self, approval_id: str) -> None:
        if self._store.approval_claim(approval_id, "pending", "cancelled"):
            self._store.approval_update(approval_id, clear_code=True)
            self._audit.write("user", "approval", approval_id, "denied", detail="已取消")
