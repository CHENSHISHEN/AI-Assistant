import json
import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.actions import ReplyService
from app.approvals import ApprovalService
from app.audit import AuditLog
from app.config import Settings
from app.main import create_app
from app.monitor import MonitorBundle
from app.monitor.store import MonitorStore
from app.orca import OrcaError, OrcaUnavailable

NOW = datetime(2026, 11, 22, 12, 0, 0, tzinfo=timezone.utc)
H1 = "term_00000000-0000-4000-8000-000000000001"
H2 = "term_00000000-0000-4000-8000-000000000002"
TOKEN = "test-token-0123456789"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SK = "sk-" + "Ab12Cd34Ef56Gh78Ij90"


def ms(dt):
    return int(dt.timestamp() * 1000)


class Notif:
    def __init__(self):
        self.sent = []
        self.live = True

    def delivers(self):
        return self.live

    def send_now(self, kind, key, text):
        self.sent.append((kind, key, text))
        return True

    def code(self):
        for k, _, t in reversed(self.sent):
            if k == "approval":
                return re.search(r"批准码 (\d{6})", t).group(1)

    def merged_count(self):
        return 0


class FakeOrca:
    def __init__(self):
        self.agents = {}
        self.extra_alive = set()
        self.screen = {}
        self.sent = []
        self.raises = None
        self.screen_error = False

    def put(self, handle, state="blocked", since=None, title="任务", tool=None, tool_input=None, prompt=""):
        self.agents[handle] = {"handle": handle, "title": title, "agent": "claude", "state": state,
                               "state_since_ms": ms(since or NOW), "prompt": prompt, "tool": tool, "tool_input": tool_input}

    def agent_snapshot(self):
        if self.raises:
            raise self.raises
        return {"alive": sorted(set(self.agents) | self.extra_alive), "agents": list(self.agents.values())}

    def list_terminals(self):
        return [{"handle": h} for h in sorted(set(self.agents) | self.extra_alive)]

    def read_terminal(self, handle, limit=200, screen=True):
        if self.screen_error:
            raise OrcaError("boom", "x")
        return {"tail": self.screen.get(handle, ["$ "]), "status": "running"}

    def send_text(self, handle, text):
        self.sent.append((handle, text))
        return {"accepted": True}


class Rig:
    def __init__(self, **settings_kw):
        self.now = NOW
        self.settings = Settings(token=TOKEN, orca_environment="e", bind_host="127.0.0.1", wait_threshold_s=300, **settings_kw)
        self.store, self.n, self.orca = MonitorStore(":memory:"), Notif(), FakeOrca()
        self.audit = AuditLog(self.store, lambda: self.now)
        self.appr = ApprovalService(self.store, self.n, self.audit, lambda: self.now, secret=b"k" * 32)
        self.replies = ReplyService(self.orca, self.store, self.appr, self.audit, self.settings, lambda: self.now)
        self.bundle = MonitorBundle(store=self.store, notifier=self.n, approvals=self.appr, audit=self.audit, replies=self.replies)
        self.c = TestClient(create_app(self.settings, self.orca, self.bundle))

    def reply(self, handle, text, **kw):
        return self.c.post(f"/sessions/{handle}/reply", json={"text": text, **kw}, headers=AUTH)

    def waiting_managed(self, handle=H1, waited=400, **kw):
        self.orca.put(handle, since=self.now - timedelta(seconds=waited), **kw)
        self.store.managed_add(handle, "user", "x")


def test_managed_pending_safe_reply_is_sent_automatically_and_audited():
    r = Rig(); r.waiting_managed()
    resp = r.reply(H1, "y")
    assert resp.status_code == 200 and resp.json() == {"status": "sent", "mode": "auto"}
    assert r.orca.sent == [(H1, "y")] and r.n.sent == []
    a = r.audit.recent(1)[0]
    assert a["decision"] == "auto" and a["action"] == "reply" and a["target"] == H1


def test_managed_but_not_yet_waiting_long_enough_goes_to_approval():
    r = Rig(); r.waiting_managed(waited=100)
    resp = r.reply(H1, "y")
    assert resp.status_code == 202 and resp.json()["status"] == "approval_required" and r.orca.sent == []


def test_unmanaged_goes_to_approval_by_default_even_if_waiting():
    r = Rig(); r.orca.put(H1, since=NOW - timedelta(seconds=900))
    assert r.reply(H1, "y").status_code == 202 and r.orca.sent == []             # D9


def test_managed_session_that_is_not_waiting_goes_to_approval():
    r = Rig(); r.waiting_managed(); r.orca.put(H1, state="working", since=NOW - timedelta(seconds=900)); r.store.managed_add(H1, "user", "x")
    assert r.reply(H1, "y").status_code == 202


def test_trust_flag_mode_allows_user_requested_reply_and_records_it():
    r = Rig(user_directed_reply="trust-flag"); r.orca.put(H1, since=NOW - timedelta(seconds=10))
    resp = r.reply(H1, "继续", user_requested=True)
    assert resp.status_code == 200 and resp.json()["mode"] == "user-requested" and r.orca.sent == [(H1, "继续")]
    assert r.audit.recent(1)[0]["decision"] == "user-directed"


def test_trust_flag_mode_without_flag_is_403():
    r = Rig(user_directed_reply="trust-flag"); r.orca.put(H1)
    assert r.reply(H1, "y").status_code == 403 and r.orca.sent == []


def test_trust_flag_mode_still_requires_approval_for_dangerous():
    r = Rig(user_directed_reply="trust-flag"); r.orca.put(H1)
    assert r.reply(H1, "rm -rf build/", user_requested=True).status_code == 202


def test_danger_in_reply_text_requires_approval_even_for_managed_pending():
    r = Rig(); r.waiting_managed()
    resp = r.reply(H1, "好的,执行 rm -rf build/")
    assert resp.status_code == 202 and r.orca.sent == []
    assert any(a["decision"] == "approval-required" and "delete" in a["rules"] for a in r.audit.recent())


def test_danger_only_in_the_request_context_requires_approval():
    # Review Focus 5：回复只是 y，但会话在请求确认 rm -rf
    r = Rig(); r.waiting_managed(tool="Bash", tool_input="rm -rf build/")
    assert r.reply(H1, "y").status_code == 202 and r.orca.sent == []
    r2 = Rig(); r2.waiting_managed(); r2.orca.screen[H1] = ["Run command?", "  rm -rf build/", "(y/n)"]
    assert r2.reply(H1, "y").status_code == 202 and r2.orca.sent == []


def test_forbidden_is_403_and_creates_no_approval():
    r = Rig(); r.waiting_managed()
    resp = r.reply(H1, "请运行 orca computer click --x 1")
    assert resp.status_code == 403 and resp.json()["error"] == "forbidden"
    assert r.appr.pending() == [] and r.orca.sent == [] and r.n.sent == []
    assert r.audit.recent(1)[0]["decision"] == "forbidden"


@pytest.mark.parametrize("text", ["a\x00b", "a\x07b", "", "   ", "x" * 501, "y\nrm -rf /", "y\r\nn", "y\n"])
def test_reply_text_with_control_chars_or_empty_or_long_is_422(text):
    r = Rig(); r.waiting_managed()
    assert r.reply(H1, text).status_code == 422 and r.orca.sent == []


def test_ansi_and_osc_in_reply_are_stripped_before_sending():
    r = Rig(); r.waiting_managed()
    r.reply(H1, "\x1b[31my\x1b[0m")
    r.reply(H1, "\x1b]0;title\x07 y")
    assert r.orca.sent == [(H1, "y"), (H1, "y")]


def test_unknown_session_is_404_and_send_never_called():
    r = Rig()
    assert r.reply(H1, "y").status_code == 404 and r.orca.sent == []
    assert r.reply("--help", "y").status_code == 422


def test_orca_unavailable_is_503():
    r = Rig(); r.orca.raises = OrcaUnavailable("orca_unavailable", "x")
    assert r.reply(H1, "y").status_code == 503


def test_per_session_hourly_limit_falls_back_to_approval():
    r = Rig(reply_per_hour=3); r.waiting_managed()
    codes = [r.reply(H1, "y").status_code for _ in range(5)]
    assert codes == [200, 200, 200, 202, 202]


def test_approval_flow_end_to_end_and_model_never_sees_the_code():
    r = Rig(); r.orca.put(H1, since=NOW - timedelta(seconds=10))
    resp = r.reply(H1, "rm -rf build/")
    body = resp.json()
    assert resp.status_code == 202 and r.orca.sent == []
    code = r.n.code()
    # 模型视角：只有 gate_token 能调的接口，遍历响应都找不到码
    seen = [resp.text, r.c.get("/approvals", headers=AUTH).text, r.c.get("/audit", headers=AUTH).text]
    assert all(code not in s for s in seen)
    ok = r.c.post(f"/approvals/{body['approval_id']}/confirm", json={"code": code}, headers=AUTH)
    assert ok.status_code == 200 and ok.json()["status"] == "executed"
    assert r.orca.sent == [(H1, "rm -rf build/")]                                  # 恰好一次
    again = r.c.post(f"/approvals/{body['approval_id']}/confirm", json={"code": code}, headers=AUTH)
    assert again.status_code == 410 and len(r.orca.sent) == 1
    assert any(a["decision"] == "executed" for a in r.audit.recent())


def test_wrong_code_is_403_and_fifth_wrong_voids():
    r = Rig(); r.orca.put(H1)
    aid = r.reply(H1, "rm -rf x").json()["approval_id"]; code = r.n.code()
    wrong = "000000" if code != "000000" else "111111"
    statuses = [r.c.post(f"/approvals/{aid}/confirm", json={"code": wrong}, headers=AUTH).status_code for _ in range(5)]
    assert statuses == [403] * 5
    assert r.c.post(f"/approvals/{aid}/confirm", json={"code": code}, headers=AUTH).status_code == 410
    assert r.orca.sent == []


def test_session_vanished_between_approval_and_confirm_is_not_sent():
    # Review Focus 7
    r = Rig(); r.orca.put(H1)
    aid = r.reply(H1, "rm -rf x").json()["approval_id"]; code = r.n.code()
    r.orca.agents.clear()
    resp = r.c.post(f"/approvals/{aid}/confirm", json={"code": code}, headers=AUTH)
    assert resp.status_code == 409 and resp.json()["error"] == "context_changed" and r.orca.sent == []
    assert any(a["decision"] == "failed" for a in r.audit.recent())
    assert "已经不存在" in r.n.sent[-1][2]


def test_confirm_with_changed_parameters_is_impossible():
    r = Rig(); r.orca.put(H1)
    aid = r.reply(H1, "rm -rf x").json()["approval_id"]; code = r.n.code()
    resp = r.c.post(f"/approvals/{aid}/confirm", json={"code": code, "text": "rm -rf /", "handle": H2}, headers=AUTH)
    assert resp.status_code == 200 and r.orca.sent == [(H1, "rm -rf x")]


def test_auth_required_on_all_new_routes():
    r = Rig()
    for m, url in (("post", f"/sessions/{H1}/reply"), ("get", "/approvals"), ("post", "/approvals/ABCD/confirm"),
                   ("delete", "/approvals/ABCD"), ("get", "/audit")):
        assert r.c.request(m.upper(), url, json={"text": "y", "code": "123456"}).status_code == 401, url


def test_get_approvals_audit_and_status_never_contain_codes_or_payload():
    r = Rig(); r.orca.put(H1)
    r.reply(H1, f"rm -rf x password=hunter2 {SK}")
    code = r.n.code()
    for url in ("/approvals", "/audit"):
        t = r.c.get(url, headers=AUTH).text
        assert code not in t and "hunter2" not in t and SK not in t


def test_cancel_endpoint_and_audit_limit_param():
    r = Rig(); r.orca.put(H1)
    aid = r.reply(H1, "rm -rf x").json()["approval_id"]
    assert r.c.delete(f"/approvals/{aid}", headers=AUTH).json()["status"] == "cancelled"
    assert r.c.get("/approvals", headers=AUTH).json()["items"] == []
    assert r.c.get("/audit?limit=0", headers=AUTH).status_code == 422
    assert len(r.c.get("/audit?limit=1", headers=AUTH).json()["items"]) == 1


def test_confirm_bad_code_format_is_422():
    r = Rig(); r.orca.put(H1)
    aid = r.reply(H1, "rm -rf x").json()["approval_id"]
    assert r.c.post(f"/approvals/{aid}/confirm", json={"code": "abc"}, headers=AUTH).status_code == 422


def test_monitor_disabled_is_503_for_actions():
    from tests.conftest import TOKEN as T
    c = TestClient(create_app(Settings(token=T, orca_environment="e", bind_host="127.0.0.1"), None, None))
    assert c.post(f"/sessions/{H1}/reply", json={"text": "y"}, headers={"Authorization": f"Bearer {T}"}).status_code == 503


# ---------- 审查意见 1：换行 ----------
def test_multi_line_reply_is_rejected_so_one_reply_cannot_answer_several_prompts():
    r = Rig(); r.waiting_managed()
    resp = r.reply(H1, "y\ny\ny")
    assert resp.status_code == 422 and r.orca.sent == [] and r.appr.pending() == []


# ---------- 审查意见 2：审批单绑定上下文，执行前重新核对 ----------
def _approval_for(r, **put):
    r.orca.put(H1, since=NOW - timedelta(seconds=10), tool="Bash", tool_input="rm -rf build/", **put)
    resp = r.reply(H1, "y")
    assert resp.status_code == 202
    return resp.json()["approval_id"], r.n.code()


def test_approval_payload_and_summary_show_exactly_what_is_being_approved():
    r = Rig()
    aid, _ = _approval_for(r)
    row = r.store.approval_get(aid)
    payload = json.loads(row["payload_json"])
    assert payload["state_since_ms"] == ms(NOW - timedelta(seconds=10))
    assert payload["tool"] == "Bash" and payload["tool_input"] == "rm -rf build/" and payload["text"] == "y"
    assert "Bash" in row["summary"] and "rm -rf build/" in row["summary"]
    assert "Bash rm -rf build/" in r.n.sent[0][2]                       # 用户在私聊里就能看清批准的是哪个请求


def test_payload_context_is_sanitized():
    r = Rig()
    r.orca.put(H1, since=NOW - timedelta(seconds=10), tool="Bash", tool_input=f"curl -H 'Authorization: Bearer {SK}' password=hunter2 rm -rf x")
    r.reply(H1, "y")
    blob = r.store.approval_get(r.appr.pending()[0]["approval_id"])["payload_json"] + r.n.sent[0][2]
    assert "hunter2" not in blob and SK not in blob


def test_unchanged_context_executes():
    r = Rig()
    aid, code = _approval_for(r)
    assert r.c.post(f"/approvals/{aid}/confirm", json={"code": code}, headers=AUTH).status_code == 200
    assert r.orca.sent == [(H1, "y")]


def test_state_start_changed_voids_the_approval_and_tells_the_user():
    r = Rig()
    aid, code = _approval_for(r)
    r.orca.put(H1, since=NOW - timedelta(seconds=5), tool="Bash", tool_input="rm -rf build/")     # 新的等待回合
    resp = r.c.post(f"/approvals/{aid}/confirm", json={"code": code}, headers=AUTH)
    assert resp.status_code == 409 and resp.json()["error"] == "context_changed" and r.orca.sent == []
    assert "已作废" in r.n.sent[-1][2] and r.n.sent[-1][0] == "approval-result"


def test_request_content_changed_voids_the_approval():
    r = Rig()
    aid, code = _approval_for(r)
    r.orca.put(H1, since=NOW - timedelta(seconds=10), tool="Bash", tool_input="rm -rf / --no-preserve-root")
    resp = r.c.post(f"/approvals/{aid}/confirm", json={"code": code}, headers=AUTH)
    assert resp.status_code == 409 and r.orca.sent == []


def test_tool_name_changed_voids_the_approval():
    r = Rig()
    aid, code = _approval_for(r)
    r.orca.put(H1, since=NOW - timedelta(seconds=10), tool="Write", tool_input="rm -rf build/")
    assert r.c.post(f"/approvals/{aid}/confirm", json={"code": code}, headers=AUTH).status_code == 409


def test_new_danger_in_the_current_context_voids_the_approval():
    r = Rig()
    r.orca.put(H1, since=NOW - timedelta(seconds=10), tool="Bash", tool_input="npm test")
    aid = r.reply(H1, "y").json()["approval_id"]; code = r.n.code()        # 未纳管 -> 审批（无危险规则）
    r.orca.screen[H1] = ["Run command?", "  git push --force", "(y/n)"]     # 批准前屏幕上出现了新的危险内容
    resp = r.c.post(f"/approvals/{aid}/confirm", json={"code": code}, headers=AUTH)
    assert resp.status_code == 409 and r.orca.sent == []
    assert "rewrite-history" in r.n.sent[-1][2]


# ---------- 审查意见 3：dry-run 下审批码没发出去 ----------
def test_dry_run_notifier_makes_approval_unavailable_instead_of_silently_succeeding():
    r = Rig(); r.n.live = False
    r.orca.put(H1)
    resp = r.reply(H1, "rm -rf x")
    assert resp.status_code == 503 and resp.json()["error"] == "approval_delivery_unavailable"
    assert r.appr.pending() == [] and r.n.sent == [] and r.orca.sent == []


# ---------- 审查意见 4：forbidden 只看回复和 tool_input ----------
def test_orca_admin_words_in_prompt_or_screen_do_not_make_the_session_unreplyable():
    r = Rig(); r.waiting_managed(prompt="之前讲解过 orca account list 的用法")
    r.orca.screen[H1] = ["$ orca environment rm old", "Proceed? (y/n)"]
    resp = r.reply(H1, "y")
    assert resp.status_code == 202 and resp.status_code != 403                # 升级为审批，而不是永久禁止
    assert any("forbidden-orca-in-context" in a["rules"] for a in r.audit.recent())


def test_forbidden_in_tool_input_is_still_forbidden():
    r = Rig(); r.waiting_managed(tool="Bash", tool_input="orca computer click --x 1")
    assert r.reply(H1, "y").status_code == 403 and r.orca.sent == []


# ---------- 审查意见 5：非字符串 tool_input ----------
@pytest.mark.parametrize("tin", [{"command": "rm -rf build/"}, ["git", "push", "--force"], {"a": [{"sql": "DROP TABLE t"}]}])
def test_non_string_tool_input_is_checked(tin):
    r = Rig(); r.waiting_managed(tool="Bash", tool_input=tin)
    assert r.reply(H1, "y").status_code == 202 and r.orca.sent == []


def test_benign_non_string_tool_input_is_not_blocked():
    r = Rig(); r.waiting_managed(tool="Bash", tool_input={"command": "npm test"})
    assert r.reply(H1, "y").status_code == 200


def test_unreadable_screen_goes_to_approval():
    r = Rig(); r.waiting_managed(tool="Bash", tool_input="npm test"); r.orca.screen_error = True
    resp = r.reply(H1, "y")
    assert resp.status_code == 202 and r.orca.sent == []
    assert any("unreadable-context" in a["rules"] for a in r.audit.recent())


# ---------- 审查意见 7：用户点名的回复单独记录 ----------
def test_user_directed_replies_do_not_use_the_auto_quota_nor_the_digest():
    r = Rig(user_directed_reply="trust-flag", reply_per_hour=1)
    r.waiting_managed()
    for _ in range(3):
        assert r.reply(H1, "继续", user_requested=True).json()["mode"] in ("auto", "user-requested")
    decisions = [a["decision"] for a in r.audit.recent(20) if a["action"] == "reply"]
    assert decisions.count("auto") == 1 and decisions.count("user-directed") == 2      # 第 1 次占自动额度，之后自动额度已满
    assert r.store.audit_count("reply", "auto", "2000-01-01T00:00:00+00:00") == 1       # 汇总只数 auto


def test_user_directed_replies_never_enter_the_hourly_digest():
    from app.monitor.runner import MonitorRunner
    r = Rig(user_directed_reply="trust-flag"); r.orca.put(H1, since=NOW - timedelta(seconds=5))
    runner = MonitorRunner(None, None, r.n, r.store, 60, audit=r.audit, digest_s=3600, now=lambda: r.now)
    r.n.notify = lambda *a, **k: r.n.sent.append(("digest-call",) + a)
    runner.digest_auto_confirms()
    r.reply(H1, "好", user_requested=True)
    r.now += timedelta(hours=2)
    assert runner.digest_auto_confirms() == 0
