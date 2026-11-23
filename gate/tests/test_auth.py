import pytest

from app.config import load_settings


def test_missing_token_is_401(client):
    assert client.get("/sessions").status_code == 401


def test_wrong_token_is_401(client):
    assert client.get("/sessions", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_non_bearer_scheme_is_401(client):
    assert client.get("/sessions", headers={"Authorization": "Basic dGVzdA=="}).status_code == 401


def test_right_token_passes_auth(client, auth):
    # 骨架阶段 /sessions 尚未实现，但必须已经过了鉴权（不是 401）
    assert client.get("/sessions", headers=auth).status_code != 401


def test_healthz_is_open(client):
    assert client.get("/healthz").json() == {"ok": True}


def test_401_body_does_not_echo_token(client):
    r = client.get("/sessions", headers={"Authorization": "Bearer wrong-secret-value"})
    assert "wrong-secret-value" not in r.text


def test_empty_token_file_refuses_to_start(tmp_path):
    f = tmp_path / "token"
    f.write_text("  \n")
    with pytest.raises(ValueError):
        load_settings({"GATE_TOKEN_FILE": str(f), "GATE_ORCA_ENVIRONMENT": "x", "GATE_BIND_HOST": "127.0.0.1"})


def test_missing_token_file_refuses_to_start(tmp_path):
    with pytest.raises(ValueError):
        load_settings({"GATE_TOKEN_FILE": str(tmp_path / "nope"), "GATE_ORCA_ENVIRONMENT": "x", "GATE_BIND_HOST": "127.0.0.1"})


def test_load_settings_strips_token_and_defaults(tmp_path):
    f = tmp_path / "token"
    f.write_text("abc123\n")
    s = load_settings({"GATE_TOKEN_FILE": str(f), "GATE_ORCA_ENVIRONMENT": "env1", "GATE_BIND_HOST": "172.28.0.1"})
    assert (s.token, s.orca_environment, s.bind_host, s.bind_port) == ("abc123", "env1", "172.28.0.1", 8500)
    assert (s.read_default_chars, s.read_max_chars, s.orca_bin) == (4000, 20000, "orca")


@pytest.mark.parametrize("host", ["0.0.0.0", "", "::"])
def test_refuses_wildcard_bind(tmp_path, host):
    f = tmp_path / "token"
    f.write_text("abc")
    with pytest.raises(ValueError):
        load_settings({"GATE_TOKEN_FILE": str(f), "GATE_ORCA_ENVIRONMENT": "x", "GATE_BIND_HOST": host})
