"""compose 渲染检查：tmpfs 的 uid/gid 跟随 ASSISTANT_UID/ASSISTANT_GID，不再写死 1001。"""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(shutil.which("docker") is None, reason="没有 docker")


def render(**extra):
    env = {**os.environ, "OPENCLAW_IMAGE": "ghcr.io/openclaw/openclaw:2026.11.6", "TELEGRAM_OWNER_ID": "123456789", **extra}
    out = subprocess.run(["docker", "compose", "--env-file", "/dev/null", "config", "--format", "json"], cwd=ROOT,
                         env=env, capture_output=True, text=True)
    if out.returncode != 0:
        pytest.skip("docker compose 不可用: " + out.stderr[:100])
    return json.loads(out.stdout)


def test_tmpfs_uid_gid_follow_assistant_uid_gid():
    cfg = render(ASSISTANT_UID="2000", ASSISTANT_GID="2001")
    tm = cfg["services"]["assistant-gate"]["tmpfs"]
    assert any("uid=2000" in t and "gid=2001" in t for t in tm), tm
    assert cfg["services"]["assistant-gate"]["user"] == "2000:2001"


def test_tmpfs_defaults_to_1001():
    cfg = render()
    assert any("uid=1001" in t and "gid=1001" in t for t in cfg["services"]["assistant-gate"]["tmpfs"])
