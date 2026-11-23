from datetime import datetime, timedelta, timezone

from app.audit import AuditLog
from app.monitor.runner import MonitorRunner
from app.monitor.store import MonitorStore

T0 = datetime(2026, 11, 22, 12, 0, 0, tzinfo=timezone.utc)
SK = "sk-" + "Ab12Cd34Ef56Gh78Ij90"


class Notif:
    def __init__(self):
        self.calls = []

    def notify(self, kind, key, text, urgent=True):
        self.calls.append((kind, key, text, urgent)); return "sent"

    def flush_pending(self): return 0
    def flush_merged(self): return 0
    def merged_count(self): return 0


class Rig:
    def __init__(self, store=None, digest_s=3600):
        self.store = store or MonitorStore(":memory:")
        self.now = T0
        self.n = Notif()
        self.audit = AuditLog(self.store, lambda: self.now)
        self.runner = MonitorRunner(None, None, self.n, self.store, 60, audit=self.audit, digest_s=digest_s, now=lambda: self.now)

    def auto(self, handle, text="y"):
        self.audit.write("openclaw", "reply", handle, "auto", detail=text)


def test_three_auto_confirms_become_exactly_one_digest_after_the_window():
    r = Rig()
    r.runner.run_once()                                     # 记下窗口起点
    r.now += timedelta(minutes=5)
    r.auto("term_aaaaaa111111", "y"); r.auto("term_aaaaaa111111", "yes"); r.auto("term_bbbbbb222222", "1")
    r.now += timedelta(minutes=10); r.runner.run_once()
    assert r.n.calls == []                                  # 窗口没到不发
    r.now += timedelta(hours=1); r.runner.run_once()
    assert len(r.n.calls) == 1
    kind, key, text, urgent = r.n.calls[0]
    assert kind == "auto-digest" and urgent is False and "3 次" in text and "111111" in text and "2 次" in text
    r.now += timedelta(hours=2); r.runner.run_once()
    assert len(r.n.calls) == 1                              # 下一个窗口没有新的 auto，不发


def test_no_auto_confirms_means_no_message():
    r = Rig()
    r.runner.run_once(); r.now += timedelta(hours=2); r.runner.run_once()
    assert r.n.calls == []


def test_non_auto_decisions_are_not_counted():
    r = Rig()
    r.runner.run_once()
    r.audit.write("openclaw", "reply", "term_x", "approval-required"); r.audit.write("openclaw", "reply", "term_x", "denied")
    r.audit.write("openclaw", "app.stop", "info", "auto")
    r.now += timedelta(hours=2); r.runner.run_once()
    assert r.n.calls == []


def test_restart_does_not_repeat_the_digest(tmp_path):
    db = str(tmp_path / "m.db")
    r = Rig(MonitorStore(db)); r.runner.run_once()
    r.auto("term_aaaaaa111111")
    r.now += timedelta(hours=2); r.runner.run_once()
    assert len(r.n.calls) == 1
    r.store.close()
    r2 = Rig(MonitorStore(db)); r2.now = r.now
    r2.runner.run_once(); r2.now += timedelta(minutes=30); r2.runner.run_once()
    assert r2.n.calls == []


def test_digest_text_is_sanitized_by_the_audit_detail():
    r = Rig()
    r.runner.run_once()
    r.auto("term_aaaaaa111111", f"password=hunter2 {SK}")
    r.now += timedelta(hours=2); r.runner.run_once()
    assert "hunter2" not in r.n.calls[0][2] and SK not in r.n.calls[0][2]


def test_runner_without_audit_skips_the_digest():
    r = MonitorRunner(None, None, Notif(), MonitorStore(":memory:"), 60)
    assert r.run_once()["errors"] == []
