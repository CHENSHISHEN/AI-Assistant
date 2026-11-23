import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app_from_env, create_app
from app.monitor import MonitorBundle
from app.monitor.runner import MonitorRunner
from app.monitor.store import MonitorStore


class Step:
    def __init__(self, result=None, raises=None):
        self.calls, self.result, self.raises = 0, result or {}, raises

    def poll(self):
        self.calls += 1
        if self.raises:
            raise self.raises
        return self.result


class Notif:
    def __init__(self):
        self.pending_calls = self.merged_calls = 0

    def flush_pending(self):
        self.pending_calls += 1
        return 0

    def flush_merged(self):
        self.merged_calls += 1
        return 0

    def merged_count(self):
        return 0


def make_runner(apps=None, sessions=None, interval=60):
    return MonitorRunner(apps or Step({"workbench": "ok"}), sessions or Step({"orca": "ok"}), Notif(),
                         MonitorStore(":memory:"), interval)


def test_one_failing_step_does_not_stop_the_others():
    apps = Step(raises=RuntimeError("boom password=hunter2"))
    sess = Step({"orca": "ok"})
    r = make_runner(apps, sess)
    snap = r.run_once()
    assert sess.calls == 1 and r.notifier.pending_calls == 1
    assert snap["sessions"] == {"orca": "ok"} and snap["apps"] is None
    assert snap["errors"] == ["apps: RuntimeError"]           # 只记录异常类型，不带异常文本
    assert "hunter2" not in repr(snap)


def test_failing_flush_does_not_raise():
    r = make_runner()
    r.notifier.flush_pending = lambda: (_ for _ in ()).throw(ValueError("x"))
    assert "flush_pending: ValueError" in r.run_once()["errors"]


def test_sessions_none_is_skipped():
    r = MonitorRunner(Step({"workbench": "disabled"}), None, Notif(), MonitorStore(":memory:"), 60)
    assert r.run_once()["sessions"] is None


def test_start_stop_runs_cycles_and_cancels_cleanly():
    async def scenario():
        apps = Step({"workbench": "ok"})
        r = make_runner(apps=apps, interval=0.05)
        await r.start()
        await asyncio.sleep(0.4)
        await r.stop()
        n = apps.calls
        await asyncio.sleep(0.2)
        return n, apps.calls

    n, after = asyncio.run(scenario())
    assert n >= 3 and after == n


def test_start_is_idempotent_and_stop_without_start_is_safe():
    async def scenario():
        r = make_runner(interval=10)
        await r.stop()
        await r.start(); await r.start()
        await r.stop()
    asyncio.run(scenario())


def _client(make_client, runner=None, mode="dry-run"):
    store = MonitorStore(":memory:")
    bundle = MonitorBundle(store=store, notifier=Notif(), runner=runner or make_runner())
    c = make_client(None)
    c.app.state.settings = Settings(token=c.app.state.settings.token, orca_environment="e", bind_host="127.0.0.1", notify_mode=mode)
    c.app.state.monitor = bundle
    return c, bundle


def test_status_requires_token_and_shows_mode(make_client, auth):
    c, bundle = _client(make_client, mode="dry-run")
    assert c.get("/monitor/status").status_code == 401
    bundle.runner.run_once()
    j = c.get("/monitor/status", headers=auth).json()
    assert j["notify_mode"] == "dry-run" and j["last_cycle"]["apps"] == {"workbench": "ok"}
    assert j["pending_notifications"] == 0 and "alerts" in j and "recent_notifications" in j


def test_status_is_redacted(make_client, auth):
    c, bundle = _client(make_client)
    bundle.store.set_alert("app:password=hunter2", True, "2026-11-22T00:00:00+00:00", True)
    assert "hunter2" not in c.get("/monitor/status", headers=auth).text


def test_status_before_first_cycle(make_client, auth):
    c, _ = _client(make_client)
    assert c.get("/monitor/status", headers=auth).json()["last_cycle"] is None


def test_monitor_disabled_status_is_503(make_client, auth):
    assert make_client(None).get("/monitor/status", headers=auth).status_code == 503


def test_app_from_env_wiring_with_lifespan(tmp_path, monkeypatch):
    tok = tmp_path / "gate_token"; tok.write_text("tok-value")
    env = {
        "GATE_TOKEN_FILE": str(tok), "GATE_ORCA_ENVIRONMENT": "e", "GATE_BIND_HOST": "127.0.0.1",
        "GATE_ORCA_BIN": str(tmp_path / "no-such-orca"),          # 不存在 -> 当作 Orca 不可用
        "GATE_WORKBENCH_URL": "http://127.0.0.1:1",                # 必然连不上，不触碰真实 Workbench
        "GATE_DB_PATH": str(tmp_path / "m.db"), "GATE_MONITOR_INTERVAL": "3600",
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    app = app_from_env()
    with TestClient(app) as c:
        deadline = time.time() + 15
        while time.time() < deadline:
            j = c.get("/monitor/status", headers={"Authorization": "Bearer tok-value"}).json()
            if j["last_cycle"]:
                break
            time.sleep(0.1)
        assert j["notify_mode"] == "dry-run"
        assert j["last_cycle"]["sessions"]["orca"] == "unavailable"
        assert j["last_cycle"]["apps"]["workbench"] in ("retrying", "unreachable")
        assert j["last_cycle"]["errors"] == []
    assert (tmp_path / "m.db").exists()
