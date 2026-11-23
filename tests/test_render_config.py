import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import render_config  # noqa: E402

TEMPLATE = (Path(__file__).resolve().parent.parent / "openclaw/openclaw.template.json").read_text()
OK = {"TELEGRAM_OWNER_ID": "123456789", "ZAI_MODEL": "zai/glm-5.3"}


def test_renders_owner_only_once_in_allow_from():
    cfg = render_config.render(TEMPLATE, OK)
    tg = cfg["channels"]["telegram"]
    assert tg["allowFrom"] == ["123456789"]
    assert tg["dmPolicy"] == "allowlist" and tg["groupPolicy"] == "disabled"
    assert cfg["commands"]["ownerAllowFrom"] == ["telegram:123456789"]


@pytest.mark.parametrize("bad", ["", "abc", "12", "-5", "123456789, 987654321", "*"])
def test_bad_owner_id_is_rejected(bad):
    with pytest.raises(ValueError):
        render_config.render(TEMPLATE, {**OK, "TELEGRAM_OWNER_ID": bad})


def test_no_plaintext_secret_or_dangerous_flags_in_template():
    for word in ("dangerouslyDisableDeviceAuth", "allowInsecureAuth", "botToken"):
        assert word not in TEMPLATE
