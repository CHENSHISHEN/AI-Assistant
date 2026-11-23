import re
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from app.app_actions import AppActions
from app.approvals import ApprovalService
from app.audit import AuditLog
from app.config import Settings
from app.main import create_app
from app.monitor import MonitorBundle
from app.monitor.apps import ActionsDisabled, WorkbenchActuator, WorkbenchClient
from app.monitor.store import MonitorStore

TOKEN = "test-token-0123456789"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
NOW = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)
SK = "sk-" + "Ab12Cd34Ef56Gh78Ij90"


class Notif:
    def __init__(self):
        self.sent = []
        self.live = True

    def delivers(self):
        return self.live

    def send_now(self, kind, key, text):
        self.sent.append((kind, key, text)); return True

    def code(self):
        return [re.search(r"批准码 (\d{6})", t).group(1) for k, _, t in self.sent if k == "approval"][-1]

    def merged_count(self):
        return 0


class FakeWB:
    """假 Workbench（MockTransport）：记录所有请求，绝不连真实的 8400。"""
    def __init__(self):
        self.requests, self.mode = [], "ok"
        self.logs = "line1\npassword=hunter2\n" + SK

    def handler(self, req: httpx.Request):
        self.requests.append((req.method, req.url.path, dict(req.url.params)))
        if self.mode == "down":
            raise httpx.ConnectError("refused", request=req)
        p = req.url.path
        if req.method == "GET" and p == "/api/status":
            return httpx.Response(200, json={"states": {"info": "running", "blog": "stopped"}, "checked_at": NOW.isoformat()})
        if req.method == "GET" and p == "/api/entries":
            return httpx.Response(200, json={"entries": [{"id": "info", "name": "Information"}, {"id": "blog", "name": "博客"}]})
        if req.method == "GET" and p.endswith("/logs"):
            return httpx.Response(200, json={"logs": self.logs})
        if req.method == "POST" and p.startswith("/api/entries/"):
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"detail": "x"})

    def http(self):
        return httpx.Client(transport=httpx.MockTransport(self.handler))

    def writes(self):
        return [r for r in self.requests if r[0] != "GET"]


class Rig:
    def __init__(self, enabled):
        self.wbm = FakeWB()
        self.settings = Settings(token=TOKEN, orca_environment="e", bind_host="127.0.0.1", app_actions=enabled)
        self.store, self.n = MonitorStore(":memory:"), Notif()
        self.audit = AuditLog(self.store, lambda: NOW)
        self.appr = ApprovalService(self.store, self.n, self.audit, lambda: NOW, secret=b"k" * 32)
        wb = WorkbenchClient("http://wb.test", client=self.wbm.http())
        self.actuator = WorkbenchActuator("http://wb.test", enabled, client=self.wbm.http())
        self.actions = AppActions(wb, self.actuator, self.appr, self.audit, self.settings)
        self.bundle = MonitorBundle(store=self.store, notifier=self.n, approvals=self.appr, audit=self.audit,
                                    app_actions=self.actions, workbench=wb)
        self.c = TestClient(create_app(self.settings, None, self.bundle))


def test_switch_off_returns_403_and_makes_no_request_and_no_approval():
    # Review Focus 8
    r = Rig(False)
    resp = r.c.post("/apps/info/restart", headers=AUTH)
    assert resp.status_code == 403 and resp.json()["error"] == "app_actions_disabled"
    assert r.wbm.requests == [] and r.appr.pending() == [] and r.n.sent == []
    assert r.audit.recent(1)[0]["decision"] == "denied"


def test_actuator_disabled_never_touches_the_network():
    wbm = FakeWB()
    act = WorkbenchActuator("http://wb.test", False, client=wbm.http())
    with pytest.raises(ActionsDisabled):
        act.act("info", "stop")
    assert wbm.requests == []


def test_switch_on_creates_approval_and_does_not_call_workbench_until_confirmed():
    r = Rig(True)
    resp = r.c.post("/apps/info/restart", headers=AUTH)
    assert resp.status_code == 202 and resp.json()["status"] == "approval_required"
    assert r.wbm.writes() == []
    assert "重启应用「Information」" in r.n.sent[0][2]


def test_confirm_then_exactly_one_post_to_the_right_path():
    r = Rig(True)
    aid = r.c.post("/apps/info/restart", headers=AUTH).json()["approval_id"]
    ok = r.c.post(f"/approvals/{aid}/confirm", json={"code": r.n.code()}, headers=AUTH)
    assert ok.status_code == 200 and ok.json()["status"] == "executed"
    assert r.wbm.writes() == [("POST", "/api/entries/info/restart", {})]
    assert r.c.post(f"/approvals/{aid}/confirm", json={"code": r.n.code()}, headers=AUTH).status_code == 410
    assert len(r.wbm.writes()) == 1
    assert any(a["action"] == "app.restart" for a in r.audit.recent(20))


def test_unknown_app_is_404_and_bad_verb_or_id_is_422():
    r = Rig(True)
    assert r.c.post("/apps/nope/stop", headers=AUTH).status_code == 404
    for url in ("/apps/info/delete", "/apps/info/Restart", "/apps/Info/stop", "/apps/..%2Fx/stop"):
        assert r.c.post(url, headers=AUTH).status_code in (404, 422), url
    assert r.wbm.writes() == [] and r.appr.pending() == []


def test_switch_turned_off_between_approval_and_confirm_fails_without_writing():
    r = Rig(True)
    aid = r.c.post("/apps/info/stop", headers=AUTH).json()["approval_id"]
    r.actuator._enabled = False                     # 模拟开关在审批期间被关掉（重启后的新实例）
    resp = r.c.post(f"/approvals/{aid}/confirm", json={"code": r.n.code()}, headers=AUTH)
    assert resp.status_code == 500 and r.wbm.writes() == []


def test_apps_listing_and_logs_are_sanitized_and_limited():
    r = Rig(False)
    j = r.c.get("/apps", headers=AUTH).json()
    assert [i["id"] for i in j["items"]] == ["blog", "info"] and j["items"][0]["state"] == "stopped" and j["collected_at"]
    lg = r.c.get("/apps/info/logs?tail=5", headers=AUTH).json()
    assert "hunter2" not in lg["text"] and SK not in lg["text"] and "line1" in lg["text"]
    assert ("GET", "/api/entries/info/logs", {"tail": "5"}) in r.wbm.requests
    assert r.c.get("/apps/info/logs?tail=0", headers=AUTH).status_code == 422
    assert r.c.get("/apps/Bad_Id/logs", headers=AUTH).status_code == 422


def test_logs_follow_is_never_requested():
    r = Rig(False)
    r.c.get("/apps/info/logs", headers=AUTH)
    assert all("follow" not in str(q) for _, _, q in r.wbm.requests)


def test_workbench_unreachable_is_503():
    r = Rig(True)
    r.wbm.mode = "down"
    assert r.c.get("/apps", headers=AUTH).status_code == 503
    assert r.c.post("/apps/info/stop", headers=AUTH).status_code == 503


def test_auth_required():
    r = Rig(True)
    for m, u in (("GET", "/apps"), ("GET", "/apps/info/logs"), ("POST", "/apps/info/stop")):
        assert r.c.request(m, u).status_code == 401


def test_read_client_still_has_no_write_methods():
    assert {n for n in dir(WorkbenchClient) if not n.startswith("_")} == {"status", "entries", "logs"}
    assert not any(hasattr(WorkbenchClient, n) for n in ("act", "post", "start", "stop", "restart"))


def test_reads_never_produce_a_write_request():
    r = Rig(False)
    r.c.get("/apps", headers=AUTH); r.c.get("/apps/info/logs", headers=AUTH)
    assert r.wbm.writes() == []
