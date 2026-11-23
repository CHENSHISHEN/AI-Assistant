import json
import re
from pathlib import Path

import httpx
import pytest

from app.config import Settings, load_settings
from app.monitor.notifier import Notifier
from app.monitor.store import MonitorStore

TOKEN = "1234567890" + ":" + "AAH" + "x" * 32          # 假 token（形式与真实一致）
SK = "sk-" + "Ab12Cd34Ef56Gh78Ij90"
FORBIDDEN = ("get" + "Updates", "set" + "Webhook", "delete" + "Webhook")


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def make(tmp_path, handler, mode="live", max_per_hour=12):
    tf = tmp_path / "tg"
    tf.write_text(TOKEN + "\n")
    s = Settings(token="t", orca_environment="e", bind_host="127.0.0.1", notify_mode=mode,
                 telegram_token_file=str(tf), telegram_chat_id="123456789", max_alerts_per_hour=max_per_hour)
    store = MonitorStore(":memory:")
    clock = Clock()
    n = Notifier(s, store, client=httpx.Client(transport=httpx.MockTransport(handler)), clock=clock)
    return n, store, clock


def ok_handler(calls):
    def h(req: httpx.Request):
        calls.append(req)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
    return h


def test_live_sends_exactly_one_sendMessage_request(tmp_path):
    calls = []
    n, store, _ = make(tmp_path, ok_handler(calls))
    assert n.notify("app", "k1", "[测试] 应用 X 已停止") == "sent"
    assert len(calls) == 1
    req = calls[0]
    assert req.method == "POST" and req.url.path == f"/bot{TOKEN}/sendMessage"
    body = json.loads(req.content)
    assert body["chat_id"] == "123456789" and body["text"] == "[测试] 应用 X 已停止"
    assert TOKEN not in req.content.decode()          # token 只在 URL 里，不在 body
    assert store.recent_log(5)[0]["outcome"] == "sent"


def test_only_sendMessage_is_ever_called(tmp_path):
    calls = []
    n, store, clock = make(tmp_path, ok_handler(calls))
    for i in range(3):
        n.notify("app", f"k{i}", f"msg {i}")
    n.flush_pending()
    assert {r.url.path.rsplit("/", 1)[-1] for r in calls} == {"sendMessage"}


def test_gate_source_never_mentions_receive_or_webhook_methods():
    root = Path(__file__).resolve().parent.parent / "app"
    hits = [str(p) for p in root.rglob("*.py") if any(w in p.read_text(encoding="utf-8") for w in FORBIDDEN)]
    assert hits == []


def test_dry_run_makes_no_http_call_and_logs(tmp_path):
    calls = []
    n, store, _ = make(tmp_path, ok_handler(calls), mode="dry-run")
    assert n.notify("app", "k", "hello") == "dry-run"
    assert calls == [] and store.recent_log(5)[0]["outcome"] == "dry-run"


def test_text_is_sanitized_in_request_and_log(tmp_path):
    calls = []
    n, store, _ = make(tmp_path, ok_handler(calls))
    n.notify("app", "k", f"出错 password=hunter2 key {SK}")
    sent = json.loads(calls[0].content)["text"]
    assert "hunter2" not in sent and SK not in sent and "[REDACTED]" in sent
    assert "hunter2" not in repr(store.recent_log(5)) and "hunter2" not in repr(store.pending_notifications())


def test_log_stores_hash_only(tmp_path):
    n, store, _ = make(tmp_path, ok_handler([]))
    n.notify("app", "k", "some text")
    row = store.recent_log(1)[0]
    assert re.fullmatch(r"[0-9a-f]{16}", row["text_hash"]) and "some text" not in repr(row)


@pytest.mark.parametrize("failure", ["http500", "429", "neterr"])
def test_failure_is_queued_not_raised_then_retried_once(tmp_path, failure):
    # Review Focus 4
    state = {"up": False, "sent": 0}

    def h(req):
        if not state["up"]:
            if failure == "neterr":
                raise httpx.ConnectError("boom", request=req)
            return httpx.Response(429 if failure == "429" else 500, json={"ok": False})
        state["sent"] += 1
        return httpx.Response(200, json={"ok": True})

    n, store, _ = make(tmp_path, h)
    assert n.notify("app", "k", "hello") == "queued"
    assert len(store.pending_notifications()) == 1
    assert n.flush_pending() == 0                       # 仍然失败
    state["up"] = True
    assert n.flush_pending() == 1 and state["sent"] == 1
    assert n.flush_pending() == 0 and state["sent"] == 1  # 只发一次
    assert store.pending_notifications() == []


def test_gives_up_after_five_attempts(tmp_path):
    n, store, _ = make(tmp_path, lambda req: httpx.Response(500, json={"ok": False}))
    n.notify("app", "k", "hello")
    for _ in range(10):
        n.flush_pending()
    assert store.pending_notifications() == []
    assert "dropped" in [r["outcome"] for r in store.recent_log(20)]


def test_telegram_ok_false_with_200_counts_as_failure(tmp_path):
    n, _s, _ = make(tmp_path, lambda req: httpx.Response(200, json={"ok": False, "description": "bad"}))
    assert n.notify("app", "k", "x") == "queued"


def test_rate_limit_merges_excess_alerts_then_digest(tmp_path):
    # Review Focus 7
    calls = []
    n, store, clock = make(tmp_path, ok_handler(calls), max_per_hour=3)
    results = [n.notify("session", f"s{i}", f"会话 {i} 在等确认") for i in range(6)]
    assert results == ["sent"] * 3 + ["merged"] * 3 and len(calls) == 3
    assert n.flush_merged() == 0                         # 窗口还满，不能发
    clock.t += 3601
    assert n.flush_merged() == 3                         # 一小时后合并成一条
    assert len(calls) == 4
    text = json.loads(calls[-1].content)["text"]
    assert "另有 3 条" in text and "会话 5" in text


def test_non_urgent_digest_bypasses_the_limit(tmp_path):
    calls = []
    n, _s, _ = make(tmp_path, ok_handler(calls), max_per_hour=1)
    n.notify("app", "a", "x")
    assert n.notify("digest", "d", "完成汇总", urgent=False) == "sent"


def test_long_text_is_truncated(tmp_path):
    calls = []
    n, _s, _ = make(tmp_path, ok_handler(calls))
    n.notify("app", "k", "字" * 10000)
    assert len(json.loads(calls[0].content)["text"]) <= 3500


def test_token_file_is_reread_each_send(tmp_path):
    calls = []
    n, _s, _ = make(tmp_path, ok_handler(calls))
    n.notify("app", "k1", "a")
    (tmp_path / "tg").write_text("999999999:" + "B" * 35 + "\n")
    n.notify("app", "k2", "b")
    assert "/bot999999999:" in str(calls[1].url)


def test_token_never_in_exception_log_or_repr(tmp_path):
    def h(req):
        raise httpx.ConnectError(f"cannot reach {req.url}", request=req)
    n, store, _ = make(tmp_path, h)
    n.notify("app", "k", "hello")
    n.flush_pending()
    assert TOKEN not in repr(store.recent_log(20)) and TOKEN not in repr(store.pending_notifications())
    assert TOKEN not in repr(n)


def test_live_mode_requires_token_file_and_chat_id(tmp_path):
    tok = tmp_path / "gt"; tok.write_text("gate-token")
    base = {"GATE_TOKEN_FILE": str(tok), "GATE_ORCA_ENVIRONMENT": "e", "GATE_BIND_HOST": "127.0.0.1"}
    with pytest.raises(ValueError):
        load_settings({**base, "GATE_NOTIFY_MODE": "live", "GATE_TELEGRAM_CHAT_ID": "123456"})
    empty = tmp_path / "empty"; empty.write_text("\n")
    with pytest.raises(ValueError):
        load_settings({**base, "GATE_NOTIFY_MODE": "live", "GATE_TELEGRAM_CHAT_ID": "123456",
                       "GATE_TELEGRAM_TOKEN_FILE": str(empty)})
    good = tmp_path / "good"; good.write_text(TOKEN)
    with pytest.raises(ValueError):
        load_settings({**base, "GATE_NOTIFY_MODE": "live", "GATE_TELEGRAM_TOKEN_FILE": str(good), "GATE_TELEGRAM_CHAT_ID": ""})
    s = load_settings({**base, "GATE_NOTIFY_MODE": "live", "GATE_TELEGRAM_TOKEN_FILE": str(good), "GATE_TELEGRAM_CHAT_ID": "123456"})
    assert s.notify_mode == "live"


def test_invalid_mode_refuses(tmp_path):
    tok = tmp_path / "gt"; tok.write_text("gate-token")
    with pytest.raises(ValueError):
        load_settings({"GATE_TOKEN_FILE": str(tok), "GATE_ORCA_ENVIRONMENT": "e", "GATE_BIND_HOST": "127.0.0.1",
                       "GATE_NOTIFY_MODE": "yes"})


def test_default_mode_is_dry_run_and_settings_repr_has_no_token(tmp_path):
    tok = tmp_path / "gt"; tok.write_text("gate-token-value")
    s = load_settings({"GATE_TOKEN_FILE": str(tok), "GATE_ORCA_ENVIRONMENT": "e", "GATE_BIND_HOST": "127.0.0.1"})
    assert s.notify_mode == "dry-run" and "gate-token-value" not in repr(s)


def test_prefix_is_prepended_to_every_message(tmp_path):
    calls = []
    n, _s, _ = make(tmp_path, ok_handler(calls))
    n._s = Settings(token="t", orca_environment="e", bind_host="127.0.0.1", notify_mode="live",
                    telegram_token_file=n._s.telegram_token_file, telegram_chat_id="123456789", notify_prefix="[测试] ")
    n.notify("app", "k", "应用 X 已停止")
    assert json.loads(calls[0].content)["text"] == "[测试] 应用 X 已停止"


def test_prefix_from_env(tmp_path):
    tok = tmp_path / "gt"; tok.write_text("gate-token")
    s = load_settings({"GATE_TOKEN_FILE": str(tok), "GATE_ORCA_ENVIRONMENT": "e", "GATE_BIND_HOST": "127.0.0.1",
                       "GATE_NOTIFY_PREFIX": "[测试] "})
    assert s.notify_prefix == "[测试] "


def test_send_now_bypasses_queue_and_rate_limit_and_reports_failure(tmp_path):
    calls = []
    n, store, _ = make(tmp_path, ok_handler(calls), max_per_hour=1)
    n.notify("app", "a", "x")                                   # 占满限流名额
    assert n.send_now("approval", "ID1", "[审批] 批准码 123456") is True
    assert "123456" in json.loads(calls[-1].content)["text"]    # 码不是凭据形式，不会被脱敏掉
    n2, store2, _ = make(tmp_path, lambda r: httpx.Response(500, json={"ok": False}))
    assert n2.send_now("approval", "ID2", "x") is False
    assert store2.pending_notifications() == []                 # 不入重试队列


def test_send_now_in_dry_run_does_not_call_http(tmp_path):
    calls = []
    n, store, _ = make(tmp_path, ok_handler(calls), mode="dry-run")
    assert n.send_now("approval", "ID", "x") is True and calls == []
    assert store.recent_log(1)[0]["outcome"] == "dry-run"


def test_delivers_is_true_only_in_live_mode(tmp_path):
    live, _s, _ = make(tmp_path, ok_handler([]), mode="live")
    dry, _s2, _ = make(tmp_path, ok_handler([]), mode="dry-run")
    assert live.delivers() is True and dry.delivers() is False
