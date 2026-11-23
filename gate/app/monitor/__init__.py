"""监视器：应用状态、会话等待与完成提醒。"""
from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class MonitorBundle:
    """app.state.monitor：路由和调度器共用的监视器组件。"""
    store: Any
    notifier: Any = None
    apps: Any = None
    sessions: Any = None
    runner: Optional[Any] = None
    approvals: Any = None     # ApprovalService
    audit: Any = None         # AuditLog
    replies: Any = None       # ReplyService
    app_actions: Any = None   # AppActions
    workbench: Any = None     # WorkbenchClient（只读）
