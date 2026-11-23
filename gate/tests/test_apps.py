import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.config import Settings
from app.monitor.apps import AppsMonitor, WorkbenchClient, WorkbenchUnreachable
from app.monitor.debounce import Debouncer
from app.monitor.store import MonitorStore

T0 = datetime(2026, 11, 22, 12, 0, 0, tzinfo=timezone.utc)


class FakeWorkbench:
    """用 httpx.MockTransport 模拟 Workbench；绝不连真实的 127.0.0.1:8400。"""

    def __init__(self):
        self.states = {"info": "running", "blog": "running"}
        self.checked_at = T0.isoformat()
        self.mode = "ok"                # ok | down | http500 | notjson
        self.requests = []
        self.names = {"info": "Information", "blog": "博客"}

    def handler(self, req: httpx.Request):
        self.requests.append((req.method, req.url.path))
        if self.mode == "down":
            raise httpx.ConnectError("refused", request=req)
        if self.mode == "http500":
            return httpx.Response(500, text="oops")
        if self.mode == "notjson":
            return httpx.Response(200, text="<html>hi</html>")
        if req.url.path == "/api/status":
            return httpx.Response(200, json={"states": self.states, "checked_at": self.checked_at})
        if req.url.path == "/api/entries":
            return httpx.Response(200, json={"entries": [{"id": k, "name": v} for k, v in self.names.items()],
                                             "registry_error": None})
        return httpx.Response(404)

    def client(self):
        return WorkbenchClient("http://wb.test", client=httpx.Client(transport=httpx.MockTransport(self.handler)))


class Rec:
    def __init__(self):
        self.calls = []

    def notify(self, kind, key, text, urgent=True):
        self.calls.append((kind, key, text))
        return "sent"


class Harness:
    def __init__(self, wb=None, store=None, notify_down=False, baseline=True):
        self.wb = wb or FakeWorkbench()
        self.store = store or MonitorStore(":memory:")
        self.rec = Rec()
        self.now = T0
        self.settings = Settings(token="t", orca_environment="e", bind_host="127.0.0.1",
                                 workbench_url="http://wb.test", notify_workbench_down=notify_down)
        self.deb = Debouncer(2)
        self.mon = AppsMonitor(self.wb.client(), self.deb, self.store, self.rec, self.settings, now=lambda: self.now)
        if baseline:
            self.poll()   # 基线周期（静默）

    def poll(self, advance=60):
        self.now += timedelta(seconds=advance)
        self.wb.checked_at = self.now.isoformat()
        return self.mon.poll()


def test_client_only_issues_get_requests():
    wb = FakeWorkbench(); c = wb.client()
    c.status(); c.entries()
    assert {m for m, _ in wb.requests} == {"GET"}
    assert {p for _, p in wb.requests} == {"/api/status", "/api/entries"}
    assert {n for n in dir(WorkbenchClient) if not n.startswith("_")} == {"status", "entries", "logs"}


def test_stop_then_recover_yields_exactly_one_alert_each():
    # 验收标准的原型：停掉一个应用 1 条提醒，恢复后 1 条恢复通知
    h = Harness()
    seq = ["running", "running", "stopped", "stopped", "stopped", "running", "running", "running"]
    for s in seq:
        h.wb.states["info"] = s
        h.poll()
    texts = [c[2] for c in h.rec.calls]
    assert len(texts) == 2, texts
    assert "已停止" in texts[0] and "Information" in texts[0]
    assert "已恢复" in texts[1] and "Information" in texts[1]
    assert "重启" not in texts[0]          # P2 不带"回复重启"那句


def test_alert_text_has_collection_time_cst():
    h = Harness()
    h.wb.states["info"] = "stopped"; h.poll(); h.poll()
    assert "采集于 20:03" in h.rec.calls[0][2]        # 第二次 stopped 发生在 12:03 UTC = 20:03 东八区


def test_single_stopped_blip_does_not_notify():
    h = Harness()
    h.wb.states["info"] = "stopped"; h.poll()
    h.wb.states["info"] = "running"; h.poll(); h.poll()
    assert h.rec.calls == []


def test_unreachable_branch_silent_by_default():
    h = Harness()
    h.wb.mode = "down"
    s1 = h.poll(); s2 = h.poll()
    assert s2["workbench"] == "unreachable" and s2["apps"] == {} and h.rec.calls == []
    h.wb.mode = "ok"; h.poll(); h.poll()
    assert h.rec.calls == []


def test_unreachable_notifies_once_when_enabled_and_recovery_once():
    h = Harness(notify_down=True)
    h.wb.mode = "down"
    for _ in range(4):
        h.poll()
    assert len(h.rec.calls) == 1 and "Workbench" in h.rec.calls[0][2]
    h.wb.mode = "ok"
    for _ in range(3):
        h.poll()
    assert len(h.rec.calls) == 2 and "恢复" in h.rec.calls[1][2]


@pytest.mark.parametrize("mode", ["http500", "notjson"])
def test_bad_http_or_body_is_unreachable(mode):
    h = Harness()
    h.wb.mode = mode
    h.poll()
    assert h.poll()["workbench"] == "unreachable"


def test_single_unreachable_blip_is_not_reported():
    h = Harness(notify_down=True)
    h.wb.mode = "down"; h.poll()
    h.wb.mode = "ok"; h.poll(); h.poll()
    assert h.rec.calls == []


def test_stale_checked_at_makes_no_decision_and_no_alert():
    # Review Focus 3：Workbench 返回的数据太旧，不能当成现在的状态
    h = Harness()
    h.wb.states["info"] = "stopped"
    for _ in range(4):
        h.now += timedelta(seconds=60)
        h.wb.checked_at = (h.now - timedelta(minutes=10)).isoformat()
        snap = h.mon.poll()
    assert snap["workbench"] == "stale" and h.rec.calls == []


def test_missing_checked_at_is_stale():
    h = Harness()
    h.wb.states["info"] = "stopped"; h.wb.checked_at = None
    assert h.mon.poll()["workbench"] == "stale"


def test_baseline_cycle_does_not_notify_already_stopped_apps():
    wb = FakeWorkbench(); wb.states["info"] = "stopped"
    h = Harness(wb=wb)               # 基线周期里 info 已经是 stopped
    h.poll(); h.poll()
    assert h.rec.calls == []
    wb.states["info"] = "running"; h.poll(); h.poll()
    assert h.rec.calls == []         # 没报过停止，也就不报恢复


def test_unknown_and_none_states_are_ignored():
    h = Harness()
    h.wb.states.update({"info": "unknown", "blog": "none"})
    for _ in range(4):
        h.poll()
    assert h.rec.calls == []


def test_disabled_when_no_workbench_client():
    s = Settings(token="t", orca_environment="e", bind_host="127.0.0.1")
    mon = AppsMonitor(None, Debouncer(2), MonitorStore(":memory:"), Rec(), s, now=lambda: T0)
    assert mon.poll()["workbench"] == "disabled"


def test_restart_does_not_repeat_alert(tmp_path):
    # Review Focus 1
    db = str(tmp_path / "m.db")
    h = Harness(store=MonitorStore(db))
    h.wb.states["info"] = "stopped"; h.poll(); h.poll()
    assert len(h.rec.calls) == 1
    h.store.close()
    h2 = Harness(wb=h.wb, store=MonitorStore(db), baseline=False)
    h2.mon.restore()
    h2.now = h.now
    h2.poll(); h2.poll(); h2.poll()
    assert h2.rec.calls == []        # 仍是 stopped，已通知过，不重复
    h2.wb.states["info"] = "running"; h2.poll(); h2.poll()
    assert len(h2.rec.calls) == 1 and "已恢复" in h2.rec.calls[0][2]


def test_snapshot_lists_apps_with_names_and_states():
    h = Harness()
    snap = h.poll()
    assert snap["apps"]["info"] == {"name": "Information", "state": "running"}
    assert snap["workbench"] == "ok" and snap["collected_at"].startswith("2026-11-22T")


def test_names_failure_falls_back_to_id():
    h = Harness()
    h.wb.names = {}
    h.wb.states["info"] = "stopped"; h.poll(); h.poll()
    assert "info" in h.rec.calls[0][2]


def test_workbench_never_receives_a_write():
    h = Harness()
    h.wb.states["info"] = "stopped"
    for _ in range(5):
        h.poll()
    assert {m for m, _ in h.wb.requests} == {"GET"}
