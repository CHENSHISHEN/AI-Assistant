"""应用接口：只读列表/日志，以及启停（审批流，开关默认关）。全部需要 Bearer。"""
from datetime import datetime, timezone
from typing import Dict

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse

from .auth import require_token
from .monitor.apps import ActionsDisabled, WorkbenchRejected, WorkbenchUnreachable
from .monitor.timefmt import iso_utc
from .redact import redact_json, sanitize
from .routes_monitor import get_monitor

router = APIRouter(dependencies=[Depends(require_token)])


def _wb(mon):
    if mon.workbench is None:
        raise HTTPException(status_code=503, detail="monitor_disabled")
    return mon.workbench


@router.get("/apps")
def apps(mon=Depends(get_monitor)) -> Dict:
    wb = _wb(mon)
    st, names = wb.status(), wb.entries()
    items = [{"id": k, "name": names.get(k, k), "state": v} for k, v in sorted(st["states"].items())]
    return redact_json({"collected_at": iso_utc(datetime.now(timezone.utc)), "workbench_checked_at": st["checked_at"], "items": items})


@router.get("/apps/{app_id}/logs")
def app_logs(app_id: str, tail: int = Query(200, ge=1, le=1000), mon=Depends(get_monitor)) -> Dict:
    text, truncated = sanitize(_wb(mon).logs(app_id, tail), 20000, keep="tail")
    return {"collected_at": iso_utc(datetime.now(timezone.utc)), "id": app_id, "text": text, "truncated": truncated}


@router.post("/apps/{app_id}/{verb}")
def app_action(app_id: str, verb: str, mon=Depends(get_monitor)):
    if mon.app_actions is None:
        raise HTTPException(status_code=503, detail="monitor_disabled")
    status, body = mon.app_actions.request(app_id, verb)
    return JSONResponse(redact_json(body), status_code=status)


def install_workbench_error_handlers(app) -> None:
    @app.exception_handler(WorkbenchUnreachable)
    async def _unreachable(_r, e):
        return JSONResponse({"error": "workbench_unreachable", "detail": "Workbench 当前不可达"}, status_code=503)

    @app.exception_handler(WorkbenchRejected)
    async def _rejected(_r, e: WorkbenchRejected):
        return JSONResponse({"error": "not_found" if e.status == 404 else "workbench_rejected",
                             "detail": f"Workbench 拒绝了请求（{e.status}）"}, status_code=404 if e.status == 404 else 400)

    @app.exception_handler(ActionsDisabled)
    async def _disabled(_r, e):
        return JSONResponse({"error": "app_actions_disabled", "detail": "应用启停目前是关闭的"}, status_code=403)
