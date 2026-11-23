"""会话只读接口：/sessions、/sessions/{id}/read、/search、/worktrees。

一切对外字符串都经过 sanitize / redact_json；列表响应带 collected_at（UTC，ISO8601）。
"""
from datetime import datetime, timezone
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from .auth import get_settings, require_token
from .config import Settings
from .orca import OrcaError, OrcaNotFound, OrcaUnavailable
from .redact import redact_json, sanitize

router = APIRouter(dependencies=[Depends(require_token)])

PREVIEW_CHARS = 600
READ_LINES = 200


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_orca(request: Request):
    orca = request.app.state.orca
    if orca is None:
        raise OrcaUnavailable("orca_unavailable", "Orca 客户端未配置")
    return orca


def _text(value: Any, limit: int = PREVIEW_CHARS) -> str:
    return sanitize(value if isinstance(value, str) else "", limit)[0]


def _project_terminal(t: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": t.get("handle"),
        "title": _text(t.get("title"), 200),
        "agent": t.get("agentIdentity"),
        "worktree_id": _text(t.get("worktreeId"), 400),
        "branch": _text(t.get("branch"), 200),
        "connected": t.get("connected"),
        "writable": t.get("writable"),
        "last_output_at": t.get("lastOutputAt"),
        "preview": _text(t.get("preview")),
    }


def _project_hit(h: Dict[str, Any]) -> Dict[str, Any]:
    ev = h.get("evidence") or {}
    return {
        "session_id": h.get("sessionId"),
        "agent": h.get("agent"),
        "title": _text(h.get("title"), 200),
        "cwd": _text(h.get("cwd"), 400),
        "branch": _text(h.get("branch"), 200),
        "updated_at": h.get("updatedAt"),
        "score": h.get("score"),
        "evidence": {"role": ev.get("role"), "snippet": _text(ev.get("snippet"))} if ev else None,
    }


_WORKTREE_KEYS = ("repo", "path", "branch", "displayName", "workspaceStatus", "liveTerminalCount",
                  "lastActivityAt", "unread", "isMainWorktree", "isArchived", "terminalVerdict", "agents")


def _project_worktree(w: Dict[str, Any]) -> Dict[str, Any]:
    out = redact_json({k: w[k] for k in _WORKTREE_KEYS if k in w})
    out["preview"] = _text(w.get("preview"))
    return out


def _listing(items: List[dict]) -> Dict[str, Any]:
    return {"collected_at": _now(), "items": items}


@router.get("/sessions")
def list_sessions(request: Request, orca=Depends(get_orca)):
    items = [_project_terminal(t) for t in orca.list_terminals()]
    mon = getattr(request.app.state, "monitor", None)
    if mon is not None:                      # 纳管标记来自网关本地名单
        for it in items:
            it["managed"] = mon.store.is_managed(it["id"])
    return _listing(items)


@router.get("/sessions/{handle}/read")
def read_session(handle: str, max_chars: int = Query(None, ge=1), orca=Depends(get_orca),
                 settings: Settings = Depends(get_settings)):
    limit = settings.read_default_chars if max_chars is None else max_chars
    if limit > settings.read_max_chars:
        raise HTTPException(status_code=422, detail=f"max_chars 不能超过 {settings.read_max_chars}")
    t = orca.read_terminal(handle, limit=READ_LINES)  # handle 非法时抛 ValueError -> 422
    raw = "\n".join(str(x) for x in (t.get("tail") or []))
    text, truncated = sanitize(raw, limit, keep="tail")  # 保留最近的输出
    return {"collected_at": _now(), "handle": handle, "status": t.get("status"),
            "text": text, "truncated": truncated or bool(t.get("truncated"))}


@router.get("/search")
def search(q: str = Query(..., min_length=1), limit: int = Query(20, ge=1, le=100), orca=Depends(get_orca)):
    if not q.strip():
        raise ValueError("搜索词不能为空")
    return _listing([_project_hit(h) for h in orca.search(q, limit=limit)])


@router.get("/worktrees")
def worktrees(orca=Depends(get_orca)):
    return _listing([_project_worktree(w) for w in orca.worktree_ps()])


def install_error_handlers(app: FastAPI) -> None:
    def body(error: str, detail: str) -> Dict[str, str]:
        return {"error": error, "detail": sanitize(detail, 300)[0]}

    @app.exception_handler(OrcaUnavailable)
    async def _unavailable(_r, e: OrcaUnavailable):
        return JSONResponse(body(e.code, e.message), status_code=503)

    @app.exception_handler(OrcaNotFound)
    async def _not_found(_r, e: OrcaNotFound):
        return JSONResponse(body("not_found", e.message), status_code=404)

    @app.exception_handler(OrcaError)
    async def _orca_error(_r, e: OrcaError):
        return JSONResponse(body("orca_error", f"{e.code}: {e.message}"), status_code=502)

    @app.exception_handler(ValueError)
    async def _bad_value(_r, e: ValueError):
        return JSONResponse(body("invalid_argument", str(e)), status_code=422)
