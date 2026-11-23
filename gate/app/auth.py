"""Bearer token 鉴权依赖。"""
import hmac

from fastapi import Depends, HTTPException, Request

from .config import Settings


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def require_token(request: Request, settings: Settings = Depends(get_settings)) -> None:
    header = request.headers.get("authorization", "")
    scheme, _, supplied = header.partition(" ")
    ok = scheme.lower() == "bearer" and hmac.compare_digest(
        supplied.strip().encode(), settings.token.encode()
    )
    if not ok:
        raise HTTPException(status_code=401, detail="unauthorized", headers={"WWW-Authenticate": "Bearer"})
