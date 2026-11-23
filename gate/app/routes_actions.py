"""动手接口：会话回复、审批、审计（应用动作见 routes_apps.py）。全部需要 Bearer。"""
from typing import Dict

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .approvals import ApprovalError
from .auth import require_token
from .redact import redact_json
from .routes_monitor import get_monitor

router = APIRouter(dependencies=[Depends(require_token)])


class ReplyBody(BaseModel):
    text: str
    user_requested: bool = False


class ConfirmBody(BaseModel):
    code: str


def _need(mon, name: str):
    comp = getattr(mon, name, None)
    if comp is None:
        raise HTTPException(status_code=503, detail="monitor_disabled")
    return comp


def executors(mon) -> Dict:
    ex = {}
    if mon.replies is not None:
        ex.update(mon.replies.executors())
    if mon.app_actions is not None:
        ex.update(mon.app_actions.executors())
    return ex


@router.post("/sessions/{handle}/reply")
def reply(handle: str, body: ReplyBody, mon=Depends(get_monitor)):
    status, payload = _need(mon, "replies").reply(handle, body.text, body.user_requested)
    return JSONResponse(redact_json(payload), status_code=status)


@router.get("/approvals")
def approvals(mon=Depends(get_monitor)):
    return {"items": _need(mon, "approvals").pending()}


@router.post("/approvals/{approval_id}/confirm")
def confirm(approval_id: str, body: ConfirmBody, mon=Depends(get_monitor)):
    return redact_json(_need(mon, "approvals").confirm(approval_id, body.code, executors(mon)))


@router.delete("/approvals/{approval_id}")
def cancel(approval_id: str, mon=Depends(get_monitor)):
    _need(mon, "approvals").cancel(approval_id)
    return {"approval_id": approval_id, "status": "cancelled"}


@router.get("/audit")
def audit(limit: int = Query(50, ge=1, le=500), since: str = Query(None), mon=Depends(get_monitor)):
    return {"items": _need(mon, "audit").recent(limit, since)}


def install_approval_error_handler(app) -> None:
    @app.exception_handler(ApprovalError)
    async def _approval_error(_r: Request, e: ApprovalError):
        return JSONResponse({"error": e.code, "detail": e.message}, status_code=e.status)
