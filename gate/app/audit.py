"""审计日志：网关每个动作（自动、拦截、审批、执行、失败）都记一行。

只存脱敏后的摘要（≤200 字）和原文哈希；审批码永远不进审计。
"""
import hashlib
from datetime import datetime
from typing import Callable, Dict, List, Optional, Sequence

from .monitor.store import MonitorStore
from .monitor.timefmt import iso_utc
from .redact import redact_json, sanitize

DECISIONS = ("auto", "user-directed", "approval-required", "approved", "denied", "forbidden", "executed", "failed", "expired", "locked")


class AuditLog:
    def __init__(self, store: MonitorStore, now: Callable[[], datetime]):
        self._store, self._now = store, now

    def write(self, actor: str, action: str, target: str, decision: str, rules: Sequence[str] = (), detail: str = "") -> int:
        if decision not in DECISIONS:
            raise ValueError(f"未知的审计决定: {decision}")
        text_hash = hashlib.sha256((detail or "").encode("utf-8")).hexdigest()[:16]
        return self._store.audit_insert(iso_utc(self._now()), actor, action, sanitize(target, 120)[0], decision,
                                        ",".join(rules), sanitize(detail or "", 200)[0], text_hash)

    def recent(self, limit: int = 50, since: Optional[str] = None) -> List[Dict]:
        return redact_json(self._store.audit_recent(max(1, min(limit, 500)), since))

    def count_since(self, action: str, decision: str, since: str) -> int:
        return self._store.audit_count(action, decision, since)
