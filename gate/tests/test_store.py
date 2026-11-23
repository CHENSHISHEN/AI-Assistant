from app.monitor.store import MonitorStore

T0, T1 = "2026-11-22T00:00:00+00:00", "2026-11-22T00:01:00+00:00"


def test_alert_roundtrip_and_survives_reopen(tmp_path):
    # Review Focus 1：重启后去重状态还在
    p = str(tmp_path / "m.db")
    s = MonitorStore(p)
    assert s.get_alert("app:x") is None
    s.set_alert("app:x", True, T0, True)
    s.close()
    s2 = MonitorStore(p)
    assert s2.get_alert("app:x") == {"key": "app:x", "bad": True, "since": T0, "notified": True}
    s2.set_alert("app:x", False, T1, False)
    assert s2.get_alert("app:x")["bad"] is False


def test_all_alerts_lists_everything():
    s = MonitorStore(":memory:")
    s.set_alert("a", True, T0, True); s.set_alert("b", False, T0, False)
    assert {a["key"] for a in s.all_alerts()} == {"a", "b"}


def test_managed_add_is_idempotent_and_listable():
    s = MonitorStore(":memory:")
    s.managed_add("term_1", "user", T0); s.managed_add("term_1", "user", T1)
    assert [m["handle"] for m in s.managed_list()] == ["term_1"]
    assert s.is_managed("term_1") and not s.is_managed("term_2")
    s.managed_remove("term_1"); s.managed_remove("term_1")
    assert s.managed_list() == []


def test_managed_prune_removes_dead_handles_only():
    # Review Focus 5
    s = MonitorStore(":memory:")
    for h in ("term_1", "term_2", "term_3"):
        s.managed_add(h, "user", T0)
    removed = s.managed_prune({"term_1", "term_3"})
    assert removed == ["term_2"] and {m["handle"] for m in s.managed_list()} == {"term_1", "term_3"}


def test_pending_notifications_retry_queue():
    s = MonitorStore(":memory:")
    nid = s.enqueue_notification(T0, "app", "k", "文本")
    assert [n["id"] for n in s.pending_notifications()] == [nid]
    assert s.bump_attempt(nid) == 1 and s.bump_attempt(nid) == 2
    s.mark_delivered(nid)
    assert s.pending_notifications() == []


def test_notification_log_records_hash_not_text():
    s = MonitorStore(":memory:")
    s.log_notification(T0, "app", "k", "abc123", "dry-run")
    rows = s.recent_log(10)
    assert rows[0]["kind"] == "app" and rows[0]["text_hash"] == "abc123" and "text" not in rows[0]


def test_kv_roundtrip():
    s = MonitorStore(":memory:")
    assert s.kv_get("baseline_done") is None
    s.kv_set("baseline_done", "1"); s.kv_set("baseline_done", "2")
    assert s.kv_get("baseline_done") == "2"


def test_sql_injection_in_keys_is_harmless():
    s = MonitorStore(":memory:")
    evil = "x'); DROP TABLE alerts;--"
    s.set_alert(evil, True, T0, False)
    assert s.get_alert(evil)["bad"] is True and s.get_alert("a") is None


def test_delete_alert():
    s = MonitorStore(":memory:")
    s.set_alert("a", True, T0, True); s.delete_alert("a"); s.delete_alert("a")
    assert s.get_alert("a") is None
