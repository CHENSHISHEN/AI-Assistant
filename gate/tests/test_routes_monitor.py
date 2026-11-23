import pytest

from app.monitor import MonitorBundle
from app.monitor.store import MonitorStore
from tests.test_sessions import FakeOrca

HANDLE = "term_00000000-0000-4000-8000-000000000001"


class FakeSessions:
    def __init__(self):
        self.snap = {"collected_at": "2026-11-22T12:00:00+00:00", "orca": "ok", "items": []}

    def pending_snapshot(self):
        return self.snap


@pytest.fixture
def bundle():
    return MonitorBundle(store=MonitorStore(":memory:"), sessions=FakeSessions())


def test_pending_requires_token(make_client, bundle):
    c = make_client(FakeOrca())
    c.app.state.monitor = bundle
    assert c.get("/sessions/pending").status_code == 401


def test_pending_returns_snapshot_with_collected_at(make_client, auth, bundle):
    c = make_client(FakeOrca()); c.app.state.monitor = bundle
    bundle.sessions.snap["items"] = [{"id": HANDLE, "state": "blocked"}]
    j = c.get("/sessions/pending", headers=auth).json()
    assert j["collected_at"] and j["items"][0]["id"] == HANDLE


def test_monitor_disabled_is_503(make_client, auth):
    assert make_client(FakeOrca()).get("/sessions/pending", headers=auth).status_code == 503


def test_manage_post_delete_are_idempotent_and_local(make_client, auth, bundle):
    fake = FakeOrca()
    c = make_client(fake); c.app.state.monitor = bundle
    for _ in range(2):
        r = c.post(f"/sessions/{HANDLE}/manage", headers=auth)
        assert r.status_code == 200 and r.json() == {"id": HANDLE, "managed": True}
    assert bundle.store.is_managed(HANDLE)
    for _ in range(2):
        assert c.delete(f"/sessions/{HANDLE}/manage", headers=auth).json() == {"id": HANDLE, "managed": False}
    assert not bundle.store.is_managed(HANDLE)


def test_manage_unknown_handle_is_404_and_invalid_is_422(make_client, auth, bundle):
    c = make_client(FakeOrca()); c.app.state.monitor = bundle
    assert c.post("/sessions/term_99999999-0000-4000-8000-000000000000/manage", headers=auth).status_code == 404
    assert c.post("/sessions/--help/manage", headers=auth).status_code == 422
    assert c.delete("/sessions/abc;rm/manage", headers=auth).status_code == 422
    assert bundle.store.managed_list() == []


def test_manage_requires_token(make_client, bundle):
    c = make_client(FakeOrca()); c.app.state.monitor = bundle
    assert c.post(f"/sessions/{HANDLE}/manage").status_code == 401
    assert c.delete(f"/sessions/{HANDLE}/manage").status_code == 401


def test_sessions_list_has_managed_flag(make_client, auth, bundle):
    c = make_client(FakeOrca()); c.app.state.monitor = bundle
    bundle.store.managed_add(HANDLE, "user", "x")
    items = c.get("/sessions", headers=auth).json()["items"]
    by = {i["id"]: i["managed"] for i in items}
    assert by[HANDLE] is True and list(by.values()).count(False) == 2


def test_manage_does_not_call_orca_beyond_listing(make_client, auth, bundle):
    class Spy(FakeOrca):
        def read_terminal(self, *a, **k):
            raise AssertionError("manage 不应读取会话")
    c = make_client(Spy()); c.app.state.monitor = bundle
    assert c.post(f"/sessions/{HANDLE}/manage", headers=auth).status_code == 200
