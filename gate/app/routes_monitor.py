"""监视器相关接口：/sessions/pending、/sessions/{id}/manage（只写网关本地名单，不触及 Orca）。"""
from datetime import datetime, timezone
from typing import Dict

from fastapi import APIRouter, Depends, HTTPException, Request

from .auth import require_token
from .orca import valid_handle
from .routes_sessions import get_orca
from .monitor.timefmt import iso_utc
from .redact import redact_json

router = APIRouter(dependencies=[Depends(require_token)])


def get_monitor(request: Request):
    mon = getattr(request.app.state, "monitor", None)
    if mon is None:
        raise HTTPException(status_code=503, detail="monitor_disabled")
    return mon


@router.get("/sessions/pending")
def sessions_pending(mon=Depends(get_monitor)) -> Dict:
    """纳管会话中等待确认超过阈值的；非纳管会话永远不出现在这里。数据来自最近一次监视采集。"""
    if mon.sessions is None:
        raise HTTPException(status_code=503, detail="monitor_disabled")
    return mon.sessions.pending_snapshot()


@router.post("/sessions/{handle}/manage")
def manage(handle: str, mon=Depends(get_monitor), orca=Depends(get_orca)) -> Dict:
    if not valid_handle(handle):
        raise ValueError("非法的终端句柄")
    if handle not in {t.get("handle") for t in orca.list_terminals()}:
        raise HTTPException(status_code=404, detail="会话不存在")
    mon.store.managed_add(handle, "user", iso_utc(datetime.now(timezone.utc)))
    return {"id": handle, "managed": True}


@router.delete("/sessions/{handle}/manage")
def unmanage(handle: str, mon=Depends(get_monitor)) -> Dict:
    if not valid_handle(handle):
        raise ValueError("非法的终端句柄")
    mon.store.managed_remove(handle)
    return {"id": handle, "managed": False}


@router.get("/monitor/status")
def monitor_status(request: Request, mon=Depends(get_monitor)) -> Dict:
    """监视器当前状态：最近一次采集快照、通知模式、告警状态、待发送队列。字符串一律脱敏。"""
    runner = mon.runner
    notifier = mon.notifier
    return redact_json({
        "notify_mode": request.app.state.settings.notify_mode,
        "last_cycle": runner.last if runner is not None else None,
        "alerts": mon.store.all_alerts(),
        "pending_notifications": len(mon.store.pending_notifications()),
        "merged_waiting": notifier.merged_count() if notifier is not None else 0,
        "managed": mon.store.managed_list(),
        "recent_notifications": mon.store.recent_log(20),
    })
