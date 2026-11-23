"""网关配置：全部来自环境变量；token 来自文件，不进环境变量、不进日志。"""
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class Settings:
    token: str
    orca_environment: str
    bind_host: str
    orca_bin: str = "orca"
    bind_port: int = 8500
    read_default_chars: int = 4000
    read_max_chars: int = 20000
    # ---- 监视器与通知（P2）----
    notify_mode: str = "dry-run"          # dry-run 只记录将要发送的内容；live 才真发
    telegram_token_file: str = ""
    telegram_chat_id: str = ""
    telegram_api_base: str = "https://api.telegram.org"
    notify_workbench_down: bool = False   # Workbench 自身不可达是否推送
    workbench_url: str = ""               # 空 = 不监视应用
    monitor_interval_s: int = 60
    max_alerts_per_hour: int = 12
    wait_threshold_s: int = 300           # 会话等待确认多久算"超过 5 分钟"
    digest_window_s: int = 600            # 会话完成汇总窗口
    db_path: str = "/data/monitor.db"
    notify_prefix: str = ""               # 给所有通知加前缀（例如测试时用 "[测试] "）
    # ---- 动手能力（P3）----
    user_directed_reply: str = "approval"  # approval | trust-flag：用户"当次明确要求"的回复怎么放行（见计划）
    app_actions: bool = False             # 应用启停开关：默认关，对真实 Workbench 的写操作保持关闭
    reply_per_hour: int = 20              # 同一会话每小时自动确认上限
    auto_digest_s: int = 3600             # 自动确认汇总窗口

    def __repr__(self) -> str:  # 避免 token 出现在日志/回溯里
        return f"Settings(orca_environment={self.orca_environment!r}, bind={self.bind_host}:{self.bind_port})"


# 网关只允许监听具体的桥接/回环地址，不允许通配
_WILDCARD_HOSTS = {"", "0.0.0.0", "::", "[::]"}


def _read_secret_file(path: str) -> str:
    try:
        value = Path(path).read_text(encoding="utf-8").strip()
    except OSError as e:
        raise ValueError(f"无法读取密钥文件 {path}: {e.strerror}") from None
    if not value:
        raise ValueError(f"密钥文件 {path} 为空")
    return value


def load_settings(env: Mapping[str, str] = os.environ) -> Settings:
    token_file = env.get("GATE_TOKEN_FILE")
    if not token_file:
        raise ValueError("缺少 GATE_TOKEN_FILE")
    environment = env.get("GATE_ORCA_ENVIRONMENT", "")
    if not environment:
        raise ValueError("缺少 GATE_ORCA_ENVIRONMENT")
    host = env.get("GATE_BIND_HOST", "")
    if host in _WILDCARD_HOSTS:
        raise ValueError("GATE_BIND_HOST 必须是具体的桥接或回环地址，不能是通配地址")
    mode = env.get("GATE_NOTIFY_MODE", "dry-run")
    if mode not in ("dry-run", "live"):
        raise ValueError("GATE_NOTIFY_MODE 只能是 dry-run 或 live")
    tg_file = env.get("GATE_TELEGRAM_TOKEN_FILE", "")
    chat_id = env.get("GATE_TELEGRAM_CHAT_ID", "")
    if mode == "live":
        if not chat_id or not chat_id.isdigit():
            raise ValueError("live 模式需要纯数字的 GATE_TELEGRAM_CHAT_ID")
        _read_secret_file(tg_file or "")  # 缺失或为空直接拒绝启动
    udr = env.get("GATE_USER_DIRECTED_REPLY", "approval")
    if udr not in ("approval", "trust-flag"):
        raise ValueError("GATE_USER_DIRECTED_REPLY 只能是 approval 或 trust-flag")
    return Settings(
        token=_read_secret_file(token_file),
        orca_environment=environment,
        bind_host=host,
        orca_bin=env.get("GATE_ORCA_BIN", "orca"),
        bind_port=int(env.get("GATE_BIND_PORT", "8500")),
        notify_mode=mode,
        telegram_token_file=tg_file,
        telegram_chat_id=chat_id,
        telegram_api_base=env.get("GATE_TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/"),
        notify_workbench_down=env.get("GATE_NOTIFY_WORKBENCH_DOWN", "0").lower() in ("1", "true", "yes"),
        workbench_url=env.get("GATE_WORKBENCH_URL", "").rstrip("/"),
        monitor_interval_s=int(env.get("GATE_MONITOR_INTERVAL", "60")),
        max_alerts_per_hour=int(env.get("GATE_MAX_ALERTS_PER_HOUR", "12")),
        wait_threshold_s=int(env.get("GATE_WAIT_THRESHOLD_S", "300")),
        digest_window_s=int(env.get("GATE_DIGEST_WINDOW_S", "600")),
        db_path=env.get("GATE_DB_PATH", "/data/monitor.db"),
        notify_prefix=env.get("GATE_NOTIFY_PREFIX", ""),
        user_directed_reply=udr,
        app_actions=env.get("GATE_APP_ACTIONS", "off").lower() in ("on", "1", "true", "yes"),
        reply_per_hour=int(env.get("GATE_REPLY_PER_HOUR", "20")),
        auto_digest_s=int(env.get("GATE_AUTO_DIGEST_S", "3600")),
    )
