from datetime import datetime, timedelta, timezone

from app.audit import AuditLog
from app.monitor.store import MonitorStore

T0 = datetime(2026, 11, 22, 12, 0, 0, tzinfo=timezone.utc)


def test_recent_since_and_count():
    s = MonitorStore(":memory:")
    now = {"t": T0}
    a = AuditLog(s, lambda: now["t"])
    a.write("gate", "reply", "term_1", "auto", detail="y")
    now["t"] = T0 + timedelta(hours=2)
    a.write("gate", "reply", "term_1", "auto", detail="y")
    a.write("gate", "reply", "term_2", "approval-required", rules=("delete",))
    since = (T0 + timedelta(hours=1)).isoformat(timespec="seconds")
    assert len(a.recent(50)) == 3
    assert len(a.recent(50, since=since)) == 2
    assert a.count_since("reply", "auto", since) == 1
    assert a.recent(1)[0]["rules"] == "delete"


def test_limit_is_clamped():
    s = MonitorStore(":memory:")
    a = AuditLog(s, lambda: T0)
    for i in range(3):
        a.write("gate", "x", "t", "auto")
    assert len(a.recent(0)) == 1 and len(a.recent(10_000)) == 3
