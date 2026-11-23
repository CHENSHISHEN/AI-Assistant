"""assistant-gate 入口：create_app() 组装 FastAPI 应用。"""
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI

from .actions import ReplyService
from .app_actions import AppActions
from .approvals import ApprovalService
from .audit import AuditLog
from .config import Settings, load_settings
from .monitor import MonitorBundle
from .monitor.apps import AppsMonitor, WorkbenchActuator, WorkbenchClient
from .monitor.debounce import Debouncer
from .monitor.notifier import Notifier
from .monitor.runner import MonitorRunner
from .monitor.sessions import SessionsMonitor
from .monitor.store import MonitorStore
from .orca import OrcaClient
from .routes_actions import install_approval_error_handler, router as actions_router
from .routes_apps import install_workbench_error_handlers, router as apps_router
from .routes_monitor import router as monitor_router
from .routes_sessions import install_error_handlers, router as sessions_router


def create_app(settings: Settings, orca=None, monitor=None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        runner = getattr(monitor, "runner", None) if monitor is not None else None
        if runner is not None:
            await runner.start()
        try:
            yield
        finally:
            if runner is not None:
                await runner.stop()

    app = FastAPI(title="assistant-gate", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.settings = settings
    app.state.orca = orca
    app.state.monitor = monitor          # MonitorBundle 或 None（测试默认不启动监视器）
    install_error_handlers(app)
    install_approval_error_handler(app)
    install_workbench_error_handlers(app)

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    app.include_router(monitor_router)     # 含 /sessions/pending，先于带参数的 sessions 路由
    app.include_router(actions_router)
    app.include_router(apps_router)
    app.include_router(sessions_router)
    return app


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def app_from_env() -> FastAPI:  # uvicorn --factory app.main:app_from_env
    settings = load_settings()
    orca = OrcaClient(settings.orca_environment, binary=settings.orca_bin)
    store = MonitorStore(settings.db_path)
    notifier = Notifier(settings, store)
    wb = WorkbenchClient(settings.workbench_url) if settings.workbench_url else None
    apps = AppsMonitor(wb, Debouncer(2), store, notifier, settings, now=_utcnow)
    apps.restore()
    sessions = SessionsMonitor(orca, store, notifier, now=_utcnow,
                               wait_threshold_s=settings.wait_threshold_s, digest_window_s=settings.digest_window_s)
    audit = AuditLog(store, _utcnow)
    secret_hex = store.kv_get("approval_secret")
    if not secret_hex:                       # 审批码的 HMAC 密钥：首次启动随机生成，只存网关自己的库
        secret_hex = secrets.token_hex(32)
        store.kv_set("approval_secret", secret_hex)
    approvals = ApprovalService(store, notifier, audit, _utcnow, secret=bytes.fromhex(secret_hex))
    replies = ReplyService(orca, store, approvals, audit, settings, _utcnow)
    app_actions = None
    if wb is not None:
        actuator = WorkbenchActuator(settings.workbench_url, settings.app_actions)   # 开关关着时不发任何请求
        app_actions = AppActions(wb, actuator, approvals, audit, settings)
    runner = MonitorRunner(apps, sessions, notifier, store, settings.monitor_interval_s, audit=audit,
                           digest_s=settings.auto_digest_s)
    bundle = MonitorBundle(store=store, notifier=notifier, apps=apps, sessions=sessions, runner=runner,
                           approvals=approvals, audit=audit, replies=replies, app_actions=app_actions, workbench=wb)
    return create_app(settings, orca, bundle)
