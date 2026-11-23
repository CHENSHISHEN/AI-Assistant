#!/usr/bin/env python3
"""把 openclaw/openclaw.template.json 和 .env 渲染成 state/openclaw.json。

部署方向只有一个：仓库 -> 容器。每次部署都整体覆盖 state/openclaw.json，
同时把 SOUL.md / AGENTS.md 的只读挂载点（占位文件）建好，避免 dockerd 以 root 创建。
不读取任何密钥文件，不输出凭据。
"""
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLACEHOLDERS = ("TELEGRAM_OWNER_ID", "ZAI_MODEL")


def parse_env(path: Path) -> dict:
    """解析简单的 KEY=VALUE .env；忽略空行和注释。"""
    env = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def render(template: str, env: dict) -> dict:
    owner = env.get("TELEGRAM_OWNER_ID", "")
    if not re.fullmatch(r"[1-9][0-9]{4,14}", owner):
        raise ValueError("TELEGRAM_OWNER_ID 必须是纯数字的 Telegram 用户 ID")
    model = env.get("ZAI_MODEL", "")
    if not re.fullmatch(r"zai/[A-Za-z0-9._-]+", model):
        raise ValueError("ZAI_MODEL 必须形如 zai/<model-id>")
    text = template.replace("__TELEGRAM_OWNER_ID__", owner).replace("__ZAI_MODEL__", model)
    cfg = json.loads(text)
    left = re.findall(r"__[A-Z_]+__", text)
    if left:
        raise ValueError(f"模板里还有未替换的占位符: {left}")
    return cfg


def main(root: Path = ROOT) -> int:
    try:
        cfg = render((root / "openclaw/openclaw.template.json").read_text(encoding="utf-8"),
                     parse_env(root / ".env"))
    except ValueError as e:
        print(f"渲染失败: {e}", file=sys.stderr)
        return 1
    state = root / "state"
    (state / "workspace").mkdir(parents=True, exist_ok=True)
    for name in ("SOUL.md", "AGENTS.md"):  # 只读挂载点
        (state / "workspace" / name).touch(exist_ok=True)
    out = state / "openclaw.json"
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"已渲染 {out.relative_to(root)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
