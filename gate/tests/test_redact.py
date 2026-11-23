"""输出关卡测试。凭据都是现场拼出来的假值，避免仓库里出现真实形式的字符串。"""
import pytest

from app.redact import REDACTED, redact, redact_json, sanitize, strip_ansi

SK = "sk-" + "Ab12Cd34Ef56Gh78Ij90"
ZHIPU = "0123456789abcdef0123456789abcdef" + "." + "AbCdEfGh12345678"
TG = "1234567890" + ":" + "AAH" + "x" * 32
JWT = "eyJhbGciOiJIUzI1NiJ9" + "." + "eyJzdWIiOiIxMjM0NTY3ODkwIn0" + "." + "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV"
PEM = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEAxyz123456\nabcdefghijkl\n-----END RSA PRIVATE KEY-----"


def hidden(text, secret):
    out = redact(text)
    assert secret not in out, out
    assert REDACTED in out, out


@pytest.mark.parametrize("text,secret", [
    ("password=hunter2", "hunter2"),
    ('PASSWORD: "hunter2"', "hunter2"),
    ('{"password": "hunter2"}', "hunter2"),
    ("Authorization: Bearer abcdef1234567890", "abcdef1234567890"),
    (f"key is {SK} ok", SK),
    (f"zhipu {ZHIPU} end", ZHIPU),
    (f"bot {TG} end", TG),
    (f"before\n{PEM}\nafter", "MIIEowIBAAKCAQEAxyz123456"),
    ("DATABASE_URL=postgres://user:s3cretpw@db.local:5432/app", "s3cretpw"),
    ("mysql://root:rootpw99@127.0.0.1/db", "rootpw99"),
    ("MY_SERVICE_SECRET=topsecretvalue", "topsecretvalue"),
    ("API_TOKEN=tok_abcdefghij", "tok_abcdefghij"),
    ("DB_PASSWD=p4ss", "p4ss"),
    ("OPENAI_KEY=abcd1234efgh", "abcd1234efgh"),
    (f"jwt {JWT}", JWT),
    ("ghp_" + "a" * 36, "a" * 36),
])
def test_each_credential_form_is_hidden(text, secret):
    hidden(text, secret)


def test_env_key_name_is_kept_value_masked():
    out = redact("MY_SERVICE_SECRET=topsecretvalue")
    assert out.startswith("MY_SERVICE_SECRET=")


@pytest.mark.parametrize("text,secret", [
    ("密码是 abc123xyz", "abc123xyz"),
    ("令牌:abc123xyz", "abc123xyz"),
    ("令牌：abc123xyz", "abc123xyz"),
    ("密钥=abc123xyz", "abc123xyz"),
    ("口令为 abc123xyz,请保管", "abc123xyz"),
])
def test_chinese_labelled_credentials_are_hidden(text, secret):
    hidden(text, secret)


def test_chinese_label_keeps_surrounding_text():
    out = redact("数据库的密码是 abc123xyz，请勿外传")
    assert "abc123xyz" not in out and "请勿外传" in out


def test_ansi_split_credential_is_hidden():
    # Review Focus 1：转义序列把凭据从中间切开
    text = "sk-Ab12Cd\x1b[0m34Ef56Gh78Ij90 and password=hun\x1b[31mter2"
    out, _ = sanitize(text)
    assert "Ab12Cd" not in out and "34Ef56" not in out
    assert "hunter2" not in out and "hun" not in out.replace("[REDACTED]", "")


def test_strip_ansi_variants():
    assert strip_ansi("\x1b[1;31mred\x1b[0m") == "red"
    assert strip_ansi("a\x1b]0;title\x07b") == "ab"
    assert strip_ansi("a\x1b]8;;http://x\x1b\\link\x1b]8;;\x1b\\b") == "alinkb"
    assert strip_ansi("\x1b(Bplain\x1b[?25h") == "plain"


def test_literal_escape_notation_in_command_text_is_stripped():
    # 真实现象：终端回显的命令行里是字面的 \033[0m（反斜杠文本，不是真正的 ESC），同样会把凭据切开
    out, _ = sanitize(r'printf "key sk-Ab12Cd\033[0m34Ef56Gh78Ij90 end"')
    assert "Ab12Cd" not in out and "34Ef56" not in out
    for lit in (r"\x1b[31m", r"\e[0m", r"\u001b[1;32m"):
        assert strip_ansi("a" + lit + "b") == "ab"


@pytest.mark.parametrize("limit", range(3, 40))
def test_truncation_never_leaks_partial_credential(limit):
    # Review Focus 2：先脱敏后截断；任何截断位置都不能漏出凭据片段
    secret = "Ab12Cd34Ef56Gh78Ij90"
    text = "log: key " + "sk-" + secret + " tail text here and more"
    out, truncated = sanitize(text, limit=limit)
    for i in range(0, len(secret) - 7):
        assert secret[i:i + 8] not in out
    assert len(out) <= limit and truncated is True


def test_sanitize_reports_not_truncated_when_short():
    out, truncated = sanitize("hello", limit=100)
    assert (out, truncated) == ("hello", False)


@pytest.mark.parametrize("text", [
    "今天天气不错，我们去公园。",
    "commit 3f2a1b9 Author: Dev <dev@example.com>\n    fix: token bucket refill 逻辑",
    "build finished in 12.3s, 0 errors",
    "git log --oneline\nabc1234 add feature\ndef5678 fix bug",
    "请用 key 参数调用，例如 f(key=lambda x: x)",
])
def test_ordinary_text_is_unchanged(text):
    assert redact(text) == text


def test_redact_is_idempotent():
    once = redact("password=hunter2 and 令牌:abc123xyz")
    assert redact(once) == once


def test_redact_json_recurses_and_keeps_keys():
    data = {"a": [f"x {SK}", {"b": "password=hunter2"}], "n": 3, "ok": True, "password": "plainvalue", "name": "fine"}
    out = redact_json(data)
    assert set(out) == set(data) and out["n"] == 3 and out["ok"] is True and out["name"] == "fine"
    flat = repr(out)
    assert SK not in flat and "hunter2" not in flat and "plainvalue" not in flat
    assert data["password"] == "plainvalue"  # 不修改入参


@pytest.mark.parametrize("limit", range(3, 30))
def test_tail_truncation_never_leaks_partial_credential(limit):
    secret = "Ab12Cd34Ef56Gh78Ij90"
    text = "head text here and more " + "sk-" + secret + " end"
    out, truncated = sanitize(text, limit=limit, keep="tail")
    for i in range(0, len(secret) - 7):
        assert secret[i:i + 8] not in out
    assert len(out) <= limit and truncated is True
    assert out == redact(text)[-limit:]


def test_tail_keeps_the_end():
    out, truncated = sanitize("0123456789", limit=4, keep="tail")
    assert (out, truncated) == ("6789", True)
