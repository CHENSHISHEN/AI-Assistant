import json
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.approvals import ApprovalError, ApprovalService
from app.audit import AuditLog
from app.monitor.store import MonitorStore

T0 = datetime(2026, 11, 22, 12, 0, 0, tzinfo=timezone.utc)
SK = "sk-" + "Ab12Cd34Ef56Gh78Ij90"


class Notif:
    """假通知器：记录 send_now 的文本，可设为发送失败。"""
    def __init__(self):
        self.sent, self.ok = [], True

    def send_now(self, kind, key, text):
        self.sent.append((kind, key, text))
        return self.ok

    def code_of(self, i=-1):
        return re.search(r"批准码 (\d{6})", self.sent[i][2]).group(1)


class Env:
    def __init__(self, **kw):
        self.store = MonitorStore(":memory:")
        self.n = Notif()
        self.now = T0
        self.audit = AuditLog(self.store, lambda: self.now)
        self.svc = ApprovalService(self.store, self.n, self.audit, lambda: self.now, secret=b"s" * 32, **kw)
        self.calls = []
        self.ex = {"echo": lambda p: (self.calls.append(p), {"done": True})[1]}

    def create(self, summary="向会话 X 发送: y", payload=None):
        return self.svc.create("reply", summary, payload or {"handle": "term_1", "text": "y"}, "echo")


def test_code_is_6_digits_sent_via_send_now_and_never_returned_or_stored():
    e = Env()
    r = e.create()
    code = e.n.code_of()
    assert re.fullmatch(r"\d{6}", code) and r["approval_id"] in e.n.sent[0][2]
    assert "批准 %s %s" % (r["approval_id"], code) in e.n.sent[0][2]
    assert e.n.sent[0][0] == "approval"
    blob = json.dumps(r) + json.dumps(e.svc.pending()) + e.store.dump_text() + json.dumps(e.audit.recent())
    assert code not in blob                                   # Review Focus 1


def test_confirm_executes_stored_payload_exactly_once_under_concurrency():
    e = Env()
    aid = e.create(payload={"handle": "term_1", "text": "y"})["approval_id"]
    code = e.n.code_of()
    started = threading.Barrier(2)
    gate = {"n": 0}

    def slow(p):
        gate["n"] += 1
        time.sleep(0.15)
        return {"ok": True}

    results = []

    def go():
        started.wait()
        try:
            results.append(e.svc.confirm(aid, code, {"echo": slow})["status"])
        except ApprovalError as err:
            results.append(err.status)

    ts = [threading.Thread(target=go) for _ in range(2)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert sorted(map(str, results)) == ["410", "executed"] and gate["n"] == 1     # Review Focus 2


def test_confirm_runs_the_payload_saved_at_creation():
    e = Env()
    aid = e.create(payload={"handle": "term_1", "text": "y"})["approval_id"]
    e.svc.confirm(aid, e.n.code_of(), e.ex)
    assert e.calls == [{"handle": "term_1", "text": "y"}]
    import inspect
    assert list(inspect.signature(ApprovalService.confirm).parameters) == ["self", "approval_id", "code", "executors"]


def test_wrong_code_5_times_voids_the_approval_even_with_the_right_code_after():
    e = Env()
    aid = e.create()["approval_id"]
    right = e.n.code_of()
    wrong = "000000" if right != "000000" else "111111"
    for i in range(5):
        with pytest.raises(ApprovalError) as ex:
            e.svc.confirm(aid, wrong, e.ex)
        assert ex.value.status == 403
    with pytest.raises(ApprovalError) as ex:
        e.svc.confirm(aid, right, e.ex)
    assert ex.value.status == 410 and e.calls == []


def test_expired_unknown_and_used_are_distinct_errors_and_never_execute():
    e = Env()
    aid = e.create()["approval_id"]; code = e.n.code_of()
    e.now += timedelta(seconds=601)
    with pytest.raises(ApprovalError) as ex:
        e.svc.confirm(aid, code, e.ex)
    assert ex.value.status == 410 and ex.value.code == "expired"
    with pytest.raises(ApprovalError) as ex:
        e.svc.confirm("ZZZZ", "123456", e.ex)
    assert ex.value.status == 404
    e2 = Env()
    a2 = e2.create()["approval_id"]; c2 = e2.n.code_of()
    e2.svc.confirm(a2, c2, e2.ex)
    with pytest.raises(ApprovalError) as ex:
        e2.svc.confirm(a2, c2, e2.ex)
    assert ex.value.status == 410 and e.calls == [] and len(e2.calls) == 1        # Review Focus 3


def test_expired_approval_is_not_listed_as_pending():
    e = Env()
    e.create(); assert len(e.svc.pending()) == 1
    e.now += timedelta(seconds=601); assert e.svc.pending() == []


def test_global_lock_after_10_wrong_guesses_across_many_approvals():
    e = Env()
    guesses = 0
    for _ in range(2):                       # 两张单各猜 5 次 = 10 次错误
        aid = e.create()["approval_id"]
        wrong = "000000" if e.n.code_of() != "000000" else "111111"
        for _ in range(5):
            guesses += 1
            with pytest.raises(ApprovalError):
                e.svc.confirm(aid, wrong, e.ex)
    assert guesses == 10
    with pytest.raises(ApprovalError) as ex:
        e.create()
    assert ex.value.status == 429 and ex.value.code == "approvals_locked"
    with pytest.raises(ApprovalError) as ex:
        e.svc.confirm("ABCD", "123456", e.ex)
    assert ex.value.status == 429
    assert sum(1 for k, _, _ in e.n.sent if k == "approval-lock") == 1               # 只通知一次
    assert [a for a in e.audit.recent() if a["decision"] == "locked"]
    e.now += timedelta(seconds=3601)
    e.create()                                                                        # 1 小时后解锁


def test_max_pending_cap():
    e = Env(max_pending=5)
    for _ in range(5):
        e.create()
    with pytest.raises(ApprovalError) as ex:
        e.create()
    assert ex.value.status == 429 and ex.value.code == "too_many_pending"


def test_delivery_failure_voids_the_approval():
    e = Env()
    e.n.ok = False
    with pytest.raises(ApprovalError) as ex:
        e.create()
    assert ex.value.status == 502 and e.svc.pending() == []


def test_executor_exception_is_recorded_as_failed_and_user_notified_without_traceback():
    e = Env()
    aid = e.create()["approval_id"]; code = e.n.code_of()

    def boom(p):
        raise RuntimeError(f"disk error password=hunter2 /home/x {SK}")

    with pytest.raises(ApprovalError) as ex:
        e.svc.confirm(aid, code, {"echo": boom})
    assert ex.value.status == 500 and "hunter2" not in str(ex.value) and "Traceback" not in str(ex.value)
    last = e.n.sent[-1][2]
    assert "执行失败" in last and "hunter2" not in last and SK not in last
    assert any(a["decision"] == "failed" for a in e.audit.recent())


def test_missing_executor_is_a_failure_not_a_crash():
    e = Env()
    aid = e.create()["approval_id"]
    with pytest.raises(ApprovalError) as ex:
        e.svc.confirm(aid, e.n.code_of(), {})
    assert ex.value.status == 500


def test_audit_written_for_create_confirm_wrong_code_expire():
    e = Env()
    a1 = e.create()["approval_id"]; c1 = e.n.code_of()
    with pytest.raises(ApprovalError):
        e.svc.confirm(a1, "000000" if c1 != "000000" else "111111", e.ex)
    e.svc.confirm(a1, c1, e.ex)
    a2 = e.create()["approval_id"]; c2 = e.n.code_of()
    e.now += timedelta(seconds=700)
    with pytest.raises(ApprovalError):
        e.svc.confirm(a2, c2, e.ex)
    decisions = [a["decision"] for a in e.audit.recent()]
    for d in ("approval-required", "denied", "approved", "executed", "expired"):
        assert d in decisions, decisions
    assert c1 not in json.dumps(e.audit.recent()) and c2 not in json.dumps(e.audit.recent())


def test_audit_detail_is_sanitized_and_limited():
    e = Env()
    e.audit.write("gate", "reply", "term_1", "auto", detail="password=hunter2 " + "x" * 500)
    row = e.audit.recent(1)[0]
    assert "hunter2" not in row["detail"] and len(row["detail"]) <= 200
    with pytest.raises(ValueError):
        e.audit.write("gate", "x", "y", "nonsense")


def test_summary_is_sanitized_in_message_and_pending():
    e = Env()
    e.svc.create("reply", f"回复 password=hunter2 {SK}", {}, "echo")
    assert "hunter2" not in e.n.sent[0][2] and SK not in e.n.sent[0][2]
    assert "hunter2" not in json.dumps(e.svc.pending())


def test_code_comparison_is_constant_time_and_secret_not_in_repr():
    src = (Path(__file__).resolve().parent.parent / "app" / "approvals.py").read_text(encoding="utf-8")
    assert "hmac.compare_digest" in src and "==" not in re.findall(r"code_hmac\S*\s*[=!]=", src)
    assert "ssss" not in repr(Env().svc)


def test_pending_has_no_code_and_no_payload():
    e = Env()
    e.create(payload={"handle": "term_secret_payload", "text": "y"})
    p = e.svc.pending()[0]
    assert set(p) == {"approval_id", "kind", "summary", "expires_at"}


def test_bad_code_format_is_422_and_not_counted():
    e = Env()
    aid = e.create()["approval_id"]
    for bad in ("12345", "1234567", "abcdef", "", "12 456"):
        with pytest.raises(ApprovalError) as ex:
            e.svc.confirm(aid, bad, e.ex)
        assert ex.value.status == 422
    assert e.store.approval_get(aid)["attempts"] == 0


def test_cancel_makes_it_unusable():
    e = Env()
    aid = e.create()["approval_id"]; code = e.n.code_of()
    e.svc.cancel(aid)
    with pytest.raises(ApprovalError) as ex:
        e.svc.confirm(aid, code, e.ex)
    assert ex.value.status == 410


def test_result_notification_sent_to_user_on_success():
    e = Env()
    aid = e.create(summary="重启应用 info")["approval_id"]
    e.svc.confirm(aid, e.n.code_of(), e.ex)
    assert e.n.sent[-1][0] == "approval-result" and "已执行" in e.n.sent[-1][2] and "重启应用 info" in e.n.sent[-1][2]
