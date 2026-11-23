import json
from datetime import datetime
from pathlib import Path

import pytest

from app.orca import OrcaError, OrcaNotFound, OrcaSearchDisabled, OrcaUnavailable

FX = Path(__file__).parent / "fixtures"
HANDLE = "term_00000000-0000-4000-8000-000000000001"
SK = "sk-" + "Ab12Cd34Ef56Gh78Ij90"


def fxj(name):
    return json.loads((FX / name).read_text(encoding="utf-8"))["result"]


class FakeOrca:
    """依赖注入用的假 OrcaClient；记录调用参数。"""

    def __init__(self, terminals=None, tail=None, hits=None, worktrees=None, raises=None):
        self.terminals = fxj("terminal_list.json")["terminals"] if terminals is None else terminals
        self.tail = tail if tail is not None else ["line one", "line two"]
        self.hits = fxj("search_results.json")["hits"] if hits is None else hits
        self.worktrees = fxj("worktree_ps.json")["worktrees"] if worktrees is None else worktrees
        self.raises, self.queries, self.reads = raises, [], []

    def _maybe(self):
        if self.raises:
            raise self.raises

    def list_terminals(self):
        self._maybe(); return self.terminals

    def read_terminal(self, handle, limit=200, screen=True):
        self._maybe(); self.reads.append((handle, limit))
        return {"handle": handle, "status": "running", "tail": self.tail, "truncated": False}

    def search(self, query, limit=20):
        self._maybe(); self.queries.append(query); return self.hits

    def worktree_ps(self):
        self._maybe(); return self.worktrees


ENDPOINTS = ["/sessions", f"/sessions/{HANDLE}/read", "/search?q=test", "/worktrees"]


def test_four_endpoints_return_200_with_collected_at(make_client, auth):
    c = make_client(FakeOrca())
    for url in ENDPOINTS:
        r = c.get(url, headers=auth)
        assert r.status_code == 200, (url, r.text)
        datetime.fromisoformat(r.json()["collected_at"])  # ISO8601


def test_sessions_items_are_projected(make_client, auth):
    items = make_client(FakeOrca()).get("/sessions", headers=auth).json()["items"]
    assert len(items) == 3 and items[0]["id"] == HANDLE and items[0]["title"] == "示例任务一"
    assert "ptyId" not in items[0]


def test_read_returns_text_and_handle(make_client, auth):
    j = make_client(FakeOrca(tail=["a", "b"])).get(f"/sessions/{HANDLE}/read", headers=auth).json()
    assert j["handle"] == HANDLE and j["text"] == "a\nb" and j["truncated"] is False and j["status"] == "running"


def test_credentials_in_session_output_are_hidden_everywhere(make_client, auth):
    terms = fxj("terminal_list.json")["terminals"]
    terms[0]["preview"] = f"export password=hunter2 and {SK}"
    terms[0]["title"] = "令牌:abc123xyz"
    hits = fxj("search_results.json")["hits"]
    hits[0]["evidence"]["snippet"] = "password=hunter2"
    wts = fxj("worktree_ps.json")["worktrees"]
    wts[0]["preview"] = f"token={SK}"
    c = make_client(FakeOrca(terminals=terms, tail=["password=hunter2", SK], hits=hits, worktrees=wts))
    for url in ENDPOINTS:
        body = c.get(url, headers=auth).text
        assert "hunter2" not in body and SK not in body and "abc123xyz" not in body, url


def test_ansi_split_credential_is_hidden(make_client, auth):
    # Review Focus 1
    tail = ["sk-Ab12Cd\x1b[0m34Ef56Gh78Ij90", "password=hun\x1b[31mter2"]
    body = make_client(FakeOrca(tail=tail)).get(f"/sessions/{HANDLE}/read", headers=auth).text
    assert "Ab12Cd" not in body and "34Ef56" not in body and "hunter2" not in body


def test_max_chars_cutting_through_credential_does_not_leak(make_client, auth):
    # Review Focus 2：对每个截断位置都检查
    secret = "Ab12Cd34Ef56Gh78Ij90"
    c = make_client(FakeOrca(tail=["start " + "sk-" + secret + " end of the output"]))
    for n in range(5, 30):
        j = c.get(f"/sessions/{HANDLE}/read?max_chars={n}", headers=auth).json()
        for i in range(0, len(secret) - 7):
            assert secret[i:i + 8] not in j["text"], (n, j["text"])
        assert j["truncated"] is True and len(j["text"]) <= n


def test_max_chars_above_maximum_is_422(make_client, auth):
    assert make_client(FakeOrca()).get(f"/sessions/{HANDLE}/read?max_chars=20001", headers=auth).status_code == 422
    assert make_client(FakeOrca()).get(f"/sessions/{HANDLE}/read?max_chars=0", headers=auth).status_code == 422


def test_default_read_limit_applies(make_client, auth, settings):
    long = "x" * (settings.read_default_chars + 500)
    j = make_client(FakeOrca(tail=[long])).get(f"/sessions/{HANDLE}/read", headers=auth).json()
    assert len(j["text"]) == settings.read_default_chars and j["truncated"] is True


@pytest.mark.parametrize("url", ENDPOINTS)
def test_orca_unavailable_is_503_without_traceback(make_client, auth, url):
    # Review Focus 3
    c = make_client(FakeOrca(raises=OrcaUnavailable("orca_unavailable", "无法连接 Orca 运行时")))
    r = c.get(url, headers=auth)
    assert r.status_code == 503 and r.json()["error"] == "orca_unavailable"
    assert "Traceback" not in r.text and r.json()["detail"]


def test_search_disabled_is_503_with_its_own_error(make_client, auth):
    r = make_client(FakeOrca(raises=OrcaSearchDisabled("search_unavailable", "未开启"))).get("/search?q=a", headers=auth)
    assert r.status_code == 503 and r.json()["error"] == "search_unavailable"


def test_missing_orca_client_is_503(make_client, auth):
    assert make_client(None).get("/sessions", headers=auth).status_code == 503


def test_stale_handle_is_404_and_other_orca_error_is_502(make_client, auth):
    assert make_client(FakeOrca(raises=OrcaNotFound("terminal_handle_stale", "x"))).get(
        f"/sessions/{HANDLE}/read", headers=auth).status_code == 404
    r = make_client(FakeOrca(raises=OrcaError("weird", f"boom password=hunter2"))).get("/sessions", headers=auth)
    assert r.status_code == 502 and "hunter2" not in r.text and "Traceback" not in r.text


def test_error_detail_is_redacted(make_client, auth):
    r = make_client(FakeOrca(raises=OrcaError("x", f"failed with {SK}"))).get("/worktrees", headers=auth)
    assert SK not in r.text


def test_invalid_handle_is_422_and_orca_never_called(make_client, auth):
    from app.orca import OrcaClient
    calls = []
    real = OrcaClient("e", runner=lambda argv: calls.append(argv) or (0, "{}", ""))
    c = make_client(real)
    for bad in ("--help", "abc;rm", "term_x"):
        assert c.get(f"/sessions/{bad}/read", headers=auth).status_code == 422, bad
    assert calls == []


def test_search_query_passes_through_verbatim(make_client, auth):
    # Review Focus 4
    fake = FakeOrca()
    c = make_client(fake)
    r = c.get("/search", params={"q": "--help 密码"}, headers=auth)
    assert r.status_code == 200 and fake.queries == ["--help 密码"]
    assert c.get("/search?q=--help%20密码", headers=auth).status_code == 200
    assert fake.queries[-1] == "--help 密码"
    c.get("/search", params={"q": 'say "hi" \'x\''}, headers=auth)
    assert fake.queries[-1] == 'say "hi" \'x\''


@pytest.mark.parametrize("q", ["", "   "])
def test_empty_search_query_is_422(make_client, auth, q):
    assert make_client(FakeOrca()).get("/search", params={"q": q}, headers=auth).status_code == 422
    assert make_client(FakeOrca()).get("/search", headers=auth).status_code == 422


def test_empty_lists_are_200_with_empty_items(make_client, auth):
    c = make_client(FakeOrca(terminals=[], hits=[], worktrees=[]))
    for url in ("/sessions", "/search?q=a", "/worktrees"):
        r = c.get(url, headers=auth)
        assert r.status_code == 200 and r.json()["items"] == []


@pytest.mark.parametrize("url", ENDPOINTS)
def test_no_token_is_401(make_client, url):
    assert make_client(FakeOrca()).get(url).status_code == 401


def test_credential_split_by_terminal_soft_wrap_is_hidden(make_client, auth):
    # 真实现象：终端按列宽软换行，会在凭据中间插入换行；网关先把换行行接回去再脱敏
    from app.orca import OrcaClient
    body = (FX / "terminal_read.json").read_text(encoding="utf-8")
    c = make_client(OrcaClient("e", runner=lambda argv: (0, body, "")))
    text = c.get(f"/sessions/{HANDLE}/read", headers=auth).json()["text"]
    assert "Ab12Cd34Ef" not in text and "56Gh78Ij90" not in text and "[REDACTED]" in text
