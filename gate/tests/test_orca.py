import json
import subprocess
from pathlib import Path

import pytest

from app.orca import (OrcaClient, OrcaError, OrcaNotFound, OrcaSearchDisabled,
                      OrcaUnavailable, default_runner)

FX = Path(__file__).parent / "fixtures"
HANDLE = "term_00000000-0000-4000-8000-000000000001"


def fx(name: str) -> str:
    return (FX / name).read_text(encoding="utf-8")


class FakeRunner:
    """记录 argv，按预设返回 (returncode, stdout, stderr)。"""

    def __init__(self, stdout="", rc=0, stderr="", exc=None):
        self.calls, self.stdout, self.rc, self.stderr, self.exc = [], stdout, rc, stderr, exc

    def __call__(self, argv):
        self.calls.append(argv)
        if self.exc:
            raise self.exc
        return self.rc, self.stdout, self.stderr


def client(runner):
    return OrcaClient("assistant-gate", runner=runner, binary="orca")


def test_list_terminals_argv_and_parse():
    r = FakeRunner(fx("terminal_list.json"))
    items = client(r).list_terminals()
    assert r.calls == [["orca", "terminal", "list", "--environment", "assistant-gate", "--json"]]
    assert [t["handle"] for t in items][0] == HANDLE and len(items) == 3


def test_empty_terminal_list_is_empty_list():
    assert client(FakeRunner(fx("terminal_list_empty.json"))).list_terminals() == []


def test_read_terminal_argv_and_parse():
    r = FakeRunner(fx("terminal_read.json"))
    t = client(r).read_terminal(HANDLE, limit=50)
    assert r.calls == [["orca", "terminal", "read", "--terminal", HANDLE, "--limit", "50", "--screen",
                        "--environment", "assistant-gate", "--json"]]
    assert t["status"] == "running" and t["tail"][3].startswith("等待确认")


@pytest.mark.parametrize("bad", ["", "--help", "term_", "x" * 300, "term_a b", "term_a;rm", "../etc", "term_\n1"])
def test_bad_handle_is_value_error_and_never_reaches_runner(bad):
    r = FakeRunner(fx("terminal_read.json"))
    with pytest.raises(ValueError):
        client(r).read_terminal(bad)
    assert r.calls == []


def test_worktree_ps_argv_and_parse():
    r = FakeRunner(fx("worktree_ps.json"))
    items = client(r).worktree_ps()
    assert r.calls == [["orca", "worktree", "ps", "--environment", "assistant-gate", "--json"]]
    assert len(items) == 1 and items[0]["path"] == "/srv/demo/repo1"


def test_search_returns_hits():
    r = FakeRunner(fx("search_results.json"))
    hits = client(r).search("npm test", limit=5)
    assert r.calls == [["orca", "search", "--query=npm test", "--limit", "5", "--environment", "assistant-gate", "--json"]]
    assert hits[0]["title"] == "示例任务一"


def test_search_query_starting_with_dash_is_one_argv_element():
    # Review Focus 4：真实 CLI 实测，--query=<原文> 是唯一不被当成选项的写法
    r = FakeRunner(fx("search_results.json"))
    q = '--help 密码 "x"'
    client(r).search(q)
    argv = r.calls[0]
    assert f"--query={q}" in argv
    assert q not in argv and "--help" not in argv


@pytest.mark.parametrize("q", ["", "   ", "x" * 501])
def test_bad_search_query_is_value_error(q):
    with pytest.raises(ValueError):
        client(FakeRunner(fx("search_results.json"))).search(q)


@pytest.mark.parametrize("limit", [0, -1, 101])
def test_bad_search_limit_is_value_error(limit):
    with pytest.raises(ValueError):
        client(FakeRunner(fx("search_results.json"))).search("a", limit=limit)


def test_search_disabled_on_host_is_search_disabled_error():
    # 真实输出：{"kind":"unavailable","reason":"disabled"}
    with pytest.raises(OrcaSearchDisabled) as e:
        client(FakeRunner(fx("search_unavailable.json"))).search("a")
    assert isinstance(e.value, OrcaUnavailable) and e.value.code == "search_unavailable"


def test_remote_runtime_unavailable_maps_to_unavailable():
    # 真实输出：配对地址连不上时 rc=1 且 stdout 仍是 JSON 信封
    r = FakeRunner(fx("err_remote_runtime_unavailable.json"), rc=1)
    with pytest.raises(OrcaUnavailable) as e:
        client(r).list_terminals()
    assert e.value.code == "orca_unavailable"


def test_stale_handle_maps_to_not_found():
    r = FakeRunner(fx("err_terminal_handle_stale.json"), rc=1)
    with pytest.raises(OrcaNotFound):
        client(r).read_terminal(HANDLE)


def test_other_envelope_error_is_orca_error_with_code():
    body = json.dumps({"ok": False, "error": {"code": "invalid_argument", "message": "bad"}})
    with pytest.raises(OrcaError) as e:
        client(FakeRunner(body, rc=1)).list_terminals()
    assert e.value.code == "invalid_argument" and not isinstance(e.value, OrcaUnavailable)


def test_timeout_is_unavailable():
    r = FakeRunner(exc=subprocess.TimeoutExpired(cmd="orca", timeout=15))
    with pytest.raises(OrcaUnavailable):
        client(r).list_terminals()


def test_missing_binary_is_unavailable():
    with pytest.raises(OrcaUnavailable):
        client(FakeRunner(exc=FileNotFoundError("orca"))).list_terminals()


def test_non_json_output_is_orca_error_not_a_crash():
    with pytest.raises(OrcaError):
        client(FakeRunner("Segmentation fault", rc=139)).list_terminals()


def test_default_runner_uses_no_shell(monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen.update(kw, argv=argv)
        return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    default_runner(15.0)(["orca", "x"])
    assert seen["argv"] == ["orca", "x"] and not seen.get("shell") and seen["timeout"] == 15.0


def test_client_has_no_public_arbitrary_argv_method():
    public = {n for n in dir(OrcaClient) if not n.startswith("_")}
    assert public == {"list_terminals", "read_terminal", "search", "worktree_ps", "agent_snapshot", "send_text"}


def test_read_terminal_joins_soft_wrapped_rows():
    # 真实输出（--screen）里，终端在列宽处软换行，tailRows[i].wrapped=True 表示第 i 行是上一行的延续
    t = client(FakeRunner(fx("terminal_read.json"))).read_terminal(HANDLE)
    assert t["tail"][4] == "export API_KEY=" + "sk-" + "Ab12Cd34Ef56Gh78Ij90" + " done"
    assert len(t["tail"]) == 5 and "tailRows" not in t


def test_wrapped_join_keeps_trailing_spaces_of_the_previous_row():
    body = json.dumps({"ok": True, "result": {"terminal": {"tail": ["password: ", "hunter2"],
        "tailRows": [{"wrapped": False, "trailingSpaces": 1}, {"wrapped": True, "trailingSpaces": 0}]}}})
    assert client(FakeRunner(body)).read_terminal(HANDLE)["tail"] == ["password:  hunter2"]


def test_missing_or_short_tail_rows_do_not_crash():
    body = json.dumps({"ok": True, "result": {"terminal": {"tail": ["a", "b", "c"], "tailRows": [{"wrapped": True}]}}})
    assert client(FakeRunner(body)).read_terminal(HANDLE)["tail"] == ["a", "b", "c"]


def test_agent_snapshot_maps_pane_key_to_terminal_handle():
    calls = []

    def runner(argv):
        calls.append(argv)
        return 0, fx("terminal_list.json" if "terminal" in argv else "worktree_ps_agents.json"), ""

    snap = client(runner).agent_snapshot()
    assert [c[1:3] for c in calls] == [["terminal", "list"], ["worktree", "ps"]]   # 固定的两条只读命令
    assert len(snap["alive"]) == 3
    by = {a["handle"]: a for a in snap["agents"]}
    blocked = by["term_00000000-0000-4000-8000-000000000002"]
    assert blocked["state"] == "blocked" and blocked["agent"] == "claude" and blocked["tool"] == "Bash"
    assert blocked["state_since_ms"] == 1795334100000 and blocked["tool_input"] == "rm -rf build/"
    assert by["term_00000000-0000-4000-8000-000000000001"]["state"] == "working"
    assert "term_00000000-0000-4000-8000-000000000003" not in by      # 没有 agent 行的 shell 不出现


def test_agent_snapshot_ignores_agent_rows_without_a_terminal():
    import copy
    ps = json.loads(fx("worktree_ps_agents.json"))
    ps["result"]["worktrees"][0]["agents"].append({"paneKey": "ghost:pane", "state": "blocked", "stateStartedAt": 1})
    runner = lambda argv: (0, fx("terminal_list.json") if "terminal" in argv else json.dumps(ps), "")
    assert len(client(runner).agent_snapshot()["agents"]) == 2


def test_send_text_argv_is_fixed_and_text_is_one_element():
    body = json.dumps({"ok": True, "result": {"send": {"accepted": True}}})
    r = FakeRunner(body)
    out = client(r).send_text(HANDLE, '--help 继续 "x"')
    assert r.calls == [["orca", "terminal", "send", "--terminal", HANDLE, '--text=--help 继续 "x"', "--enter",
                        "--environment", "assistant-gate", "--json"]]
    assert out == {"accepted": True}


@pytest.mark.parametrize("handle,text", [("--help", "y"), ("term_x", "y"), (HANDLE, ""), (HANDLE, "   "),
                                         (HANDLE, "a\x00b"), (HANDLE, "a\x1b[31mb"), (HANDLE, "x" * 2001),
                                         (HANDLE, "y\nn"), (HANDLE, "y\r"), (HANDLE, "y\u2028n")])
def test_send_text_rejects_bad_input_without_calling_orca(handle, text):
    r = FakeRunner("{}")
    with pytest.raises(ValueError):
        client(r).send_text(handle, text)
    assert r.calls == []
