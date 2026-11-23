from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.config import Settings
from app.monitor.notifier import Notifier
from app.monitor.sessions import SessionsMonitor
from app.monitor.store import MonitorStore
from app.orca import OrcaUnavailable

T0 = datetime(2026, 11, 22, 12, 0, 0, tzinfo=timezone.utc)
SK = "sk-" + "Ab12Cd34Ef56Gh78Ij90"


def ms(dt):
    return int(dt.timestamp() * 1000)


class FakeOrca:
    def __init__(self):
        self.agents = {}          # handle -> dict
        self.alive_extra = set()
        self.raises = None
        self.snapshot_calls = 0

    def set(self, handle, state, since, title=None, agent="codex", prompt="", tool=None, tool_input=None):
        self.agents[handle] = {"handle": handle, "title": title or handle, "agent": agent, "state": state,
                               "state_since_ms": ms(since), "prompt": prompt, "tool": tool, "tool_input": tool_input}

    def agent_snapshot(self):
        self.snapshot_calls += 1
        if self.raises:
            raise self.raises
        return {"alive": sorted(set(self.agents) | self.alive_extra), "agents": list(self.agents.values())}


class Rec:
    def __init__(self):
        self.calls = []

    def notify(self, kind, key, text, urgent=True):
        self.calls.append((kind, key, text, urgent))
        return "sent"


class H:
    def __init__(self, baseline=True, notifier=None, store=None):
        self.orca, self.store = FakeOrca(), store or MonitorStore(":memory:")
        self.rec = notifier or Rec()
        self.now = T0
        self.mon = SessionsMonitor(self.orca, self.store, self.rec, now=lambda: self.now,
                                   wait_threshold_s=300, digest_window_s=600)
        if baseline:
            self.poll(0)

    def poll(self, advance=60):
        self.now += timedelta(seconds=advance)
        return self.mon.poll()


def test_waiting_under_5_minutes_is_not_pending_nor_alerted():
    h = H()
    h.orca.set("term_a", "blocked", h.now - timedelta(seconds=299)); h.store.managed_add("term_a", "user", "x")
    snap = h.poll(0)
    assert snap["pending"] == [] and h.rec.calls == []


def test_waiting_over_5_minutes_managed_is_pending_and_alerts_once():
    h = H()
    h.store.managed_add("term_a", "user", "x")
    h.orca.set("term_a", "blocked", h.now, title="清理任务", tool="Bash", tool_input="rm -rf build/")
    h.poll(301)
    snap = h.poll(60)
    assert [p["id"] for p in snap["pending"]] == ["term_a"]
    p = snap["pending"][0]
    assert p["state"] == "blocked" and p["waiting_for_s"] >= 300 and p["tool"] == "Bash" and "rm -rf" in p["tool_input"]
    assert len(h.rec.calls) == 1 and "清理任务" in h.rec.calls[0][2] and "已纳管" in h.rec.calls[0][2]


def test_unmanaged_waiting_is_never_pending_but_gets_remind_only_alert():
    # D9：非纳管会话只提醒、不代答
    h = H()
    h.orca.set("term_b", "waiting", h.now, title="别人的会话")
    h.poll(301)
    snap = h.poll(60)
    assert snap["pending"] == []
    assert len(h.rec.calls) == 1 and "不会代答" in h.rec.calls[0][2]


def test_same_wait_episode_alerts_once_new_episode_alerts_again():
    h = H()
    h.orca.set("term_a", "blocked", h.now)
    for _ in range(6):
        h.poll(120)
    assert len(h.rec.calls) == 1
    h.orca.set("term_a", "working", h.now); h.poll(60)
    h.orca.set("term_a", "blocked", h.now)
    for _ in range(5):
        h.poll(120)
    assert len(h.rec.calls) == 2


def test_working_to_done_goes_into_digest_flushed_once_per_window():
    h = H()
    for i in range(3):
        h.orca.set(f"term_{i}", "working", h.now, title=f"任务{i}")
    h.poll(60)
    for i in range(3):
        h.orca.set(f"term_{i}", "done", h.now, title=f"任务{i}")
    h.poll(60)
    assert h.rec.calls == []                       # 窗口未到，先攒着
    h.poll(601)
    assert len(h.rec.calls) == 1
    kind, key, text, urgent = h.rec.calls[0]
    assert kind == "digest" and urgent is False and "3 个会话" in text and "任务0" in text and "任务2" in text
    h.poll(601)
    assert len(h.rec.calls) == 1                   # 不重复


def test_done_without_previous_working_is_not_a_completion():
    h = H()
    h.orca.set("term_x", "done", h.now); h.poll(60); h.poll(700)
    assert h.rec.calls == []


def test_baseline_cycle_is_silent():
    h = H(baseline=False)
    h.orca.set("term_a", "blocked", h.now - timedelta(hours=2), title="早就在等")
    h.poll(0)
    h.poll(60)
    assert h.rec.calls == []           # 基线周期里已经在等的会话不推送（存量不翻出来）


def test_vanished_session_is_removed_from_managed_list():
    # Review Focus 5
    h = H()
    h.store.managed_add("term_gone", "user", "x"); h.store.managed_add("term_a", "user", "x")
    h.orca.set("term_a", "working", h.now)
    h.poll(60)
    assert [m["handle"] for m in h.store.managed_list()] == ["term_a"]


def test_managed_shell_terminal_without_agent_is_kept_while_alive():
    h = H()
    h.store.managed_add("term_shell", "user", "x"); h.orca.alive_extra.add("term_shell")
    h.poll(60)
    assert h.store.is_managed("term_shell")


def test_pending_prompt_and_alert_are_sanitized():
    h = H()
    h.store.managed_add("term_a", "user", "x")
    h.orca.set("term_a", "blocked", h.now, tool="Bash", tool_input=f"curl -H 'Authorization: Bearer {SK}' password=hunter2",
               prompt="deploy password=hunter2", title="令牌:abc123xyz")
    h.poll(301)
    snap = h.poll(60)
    blob = repr(snap) + repr(h.rec.calls)
    assert "hunter2" not in blob and SK not in blob and "abc123xyz" not in blob and "[REDACTED]" in blob


def test_mass_waiting_is_rate_limited_into_one_digest(tmp_path):
    # Review Focus 7：30 个会话同时变成等待
    s = Settings(token="t", orca_environment="e", bind_host="127.0.0.1", notify_mode="dry-run", max_alerts_per_hour=5)
    store = MonitorStore(":memory:")
    clock = {"t": 1_000_000.0}
    notifier = Notifier(s, store, client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True}))),
                        clock=lambda: clock["t"])
    h = H(notifier=notifier, store=store)
    for i in range(30):
        h.orca.set(f"term_{i:02d}", "blocked", h.now, title=f"会话{i}")
    h.poll(301); h.poll(60)
    log = store.recent_log(100)
    outcomes = [r["outcome"] for r in log]
    assert outcomes.count("dry-run") == 5 and outcomes.count("merged") == 25
    assert notifier.merged_count() == 25


def test_orca_unavailable_cycle_is_recorded_not_raised():
    h = H()
    h.orca.raises = OrcaUnavailable("orca_unavailable", "x")
    snap = h.poll(60)
    assert snap["orca"] == "unavailable" and snap["pending"] == [] and h.rec.calls == []
    h.orca.raises = None
    assert h.poll(60)["orca"] == "ok"


def test_unavailable_first_cycle_does_not_consume_the_baseline():
    h = H(baseline=False)
    h.orca.raises = OrcaUnavailable("orca_unavailable", "x"); h.poll(0)
    h.orca.raises = None
    h.orca.set("term_a", "blocked", h.now - timedelta(hours=1))
    h.poll(0)                          # 这才是基线周期
    h.poll(60)
    assert h.rec.calls == []


def test_restart_does_not_realert_same_episode(tmp_path):
    db = str(tmp_path / "m.db")
    h = H(store=MonitorStore(db))
    h.orca.set("term_a", "blocked", h.now); h.poll(301); h.poll(60)
    assert len(h.rec.calls) == 1
    h.store.close()
    h2 = H(baseline=False, store=MonitorStore(db))
    h2.orca.agents = dict(h.orca.agents); h2.now = h.now
    h2.poll(60); h2.poll(120)
    assert h2.rec.calls == []


def test_stale_wait_alert_rows_are_cleaned_up():
    h = H()
    h.orca.set("term_a", "blocked", h.now); h.poll(301)
    assert any(a["key"].startswith("wait:") for a in h.store.all_alerts())
    h.orca.agents.clear(); h.poll(60)
    assert not any(a["key"].startswith("wait:") for a in h.store.all_alerts())


def test_snapshot_has_collected_at_and_counts():
    h = H()
    h.orca.set("term_a", "working", h.now)
    s = h.poll(60)
    assert s["collected_at"].startswith("2026-11-22T") and s["orca"] == "ok" and s["agents"] == 1
