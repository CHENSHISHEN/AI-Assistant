"""应用启停的审批流（设计 D5：应用启停始终需要审批）。

- 开关 GATE_APP_ACTIONS 默认 off：POST /apps/{id}/{verb} 直接 403，不创建审批单，不碰 Workbench。
- 开关 on：校验应用存在 -> 创建审批单（202）-> 用户批准后才由 WorkbenchActuator 调 Workbench 写接口。
"""
from typing import Callable, Dict, Tuple

from .audit import AuditLog
from .config import Settings
from .monitor.apps import VERBS, WorkbenchActuator, WorkbenchClient, _APP_ID_RE

_VERB_ZH = {"start": "启动", "stop": "停止", "restart": "重启"}


class AppActions:
    def __init__(self, wb: WorkbenchClient, actuator: WorkbenchActuator, approvals, audit: AuditLog, settings: Settings):
        self._wb, self._act, self._appr, self._audit, self._s = wb, actuator, approvals, audit, settings

    def executors(self) -> Dict[str, Callable[[dict], dict]]:
        return {"app": self.execute}

    def request(self, app_id: str, verb: str) -> Tuple[int, Dict]:
        if verb not in VERBS or not _APP_ID_RE.match(app_id or ""):
            raise ValueError("非法的应用或动作")
        if not self._s.app_actions:
            self._audit.write("openclaw", f"app.{verb}", app_id, "denied", rules=("actions-disabled",))
            return 403, {"error": "app_actions_disabled", "detail": "应用启停目前是关闭的（GATE_APP_ACTIONS=off）"}
        names = self._wb.entries()                      # WorkbenchUnreachable -> 503
        if app_id not in names:
            return 404, {"error": "not_found", "detail": "应用不存在"}
        r = self._appr.create("app", f"{_VERB_ZH[verb]}应用「{names[app_id]}」({app_id})", {"id": app_id, "verb": verb}, "app")
        self._audit.write("openclaw", f"app.{verb}", app_id, "approval-required")
        return 202, {"status": "approval_required", **r, "message": "审批单已发到用户私聊；请等用户回复「批准 <审批单ID> <码>」，不要重复创建。"}

    def execute(self, payload: dict) -> dict:
        return self._act.act(payload["id"], payload["verb"])
