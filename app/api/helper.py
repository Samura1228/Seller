"""API for the marketplace helper on the owner's Mac. Auth: `Authorization: Bearer <HELPER_TOKEN>`."""

import hmac
from dataclasses import asdict
from typing import Any, Protocol

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.config import get_settings
from app.db.enums import ExternalStatus
from app.db.models import ItemPhoto
from app.db.session import session_scope
from app.services import marketplaces


class HelperNotifier(Protocol):
    async def marketplace_filled(self, listing_id: int, screenshot_path: str | None, note: str | None) -> None: ...
    async def marketplace_published(self, listing_id: int) -> None: ...
    async def marketplace_failed(self, listing_id: int, error: str, final: bool) -> None: ...
    async def marketplace_removed(self, listing_id: int) -> None: ...


_notifier: HelperNotifier | None = None


def set_notifier(n: HelperNotifier) -> None:
    global _notifier
    _notifier = n


async def _notify(method: str, *args) -> None:
    if _notifier is not None:
        await getattr(_notifier, method)(*args)


def require_helper(authorization: str = Header(default="")) -> None:
    token = get_settings().helper_token
    if not token:
        raise HTTPException(503, "HELPER_TOKEN is not set on the server")
    if not hmac.compare_digest(authorization.removeprefix("Bearer ").strip(), token):
        raise HTTPException(401, "bad token")


router = APIRouter(prefix="/api/helper", dependencies=[Depends(require_helper)])


class NextIn(BaseModel):
    platforms: list[str]


class PlanIn(BaseModel):
    goal: str
    snapshot: dict[str, Any]
    history: list[dict[str, Any]] = []


class PublishedIn(BaseModel):
    url: str | None = None


class FailedIn(BaseModel):
    error: str
    reason: str = "error"  # error | logged_out | blocked


@router.post("/next")
async def next_task(body: NextIn) -> dict:
    task = await marketplaces.claim(body.platforms)
    return {"task": asdict(task) if task else None}


@router.get("/photo/{photo_id}")
async def photo(photo_id: int) -> FileResponse:
    async with session_scope() as s:
        p = await s.get(ItemPhoto, photo_id)
    if p is None:
        raise HTTPException(404)
    path = get_settings().data_dir / p.path
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path)


@router.post("/tasks/{listing_id}/plan")
async def plan(listing_id: int, body: PlanIn) -> dict:
    result = await marketplaces.plan_step(listing_id, body.goal, body.snapshot, body.history)
    return result.model_dump()


@router.post("/tasks/{listing_id}/filled")
async def filled(listing_id: int, request: Request, note: str | None = None) -> dict:
    """Body: the PNG screenshot of the filled form."""
    png = await request.body()
    path = None
    if png:
        d = get_settings().data_dir / "screens"
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{listing_id}.png"
        path.write_bytes(png)
    await marketplaces.filled(listing_id, str(path) if path else None)
    await _notify("marketplace_filled", listing_id, str(path) if path else None, note)
    return {"ok": True}


@router.get("/tasks/{listing_id}/decision")
async def decision(listing_id: int) -> dict:
    return {"decision": await marketplaces.decision(listing_id)}


@router.post("/tasks/{listing_id}/no_answer")
async def no_answer(listing_id: int) -> dict:
    await marketplaces.no_answer(listing_id)
    return {"ok": True}


@router.post("/tasks/{listing_id}/published")
async def published(listing_id: int, body: PublishedIn) -> dict:
    await marketplaces.published(listing_id, body.url)
    await _notify("marketplace_published", listing_id)
    return {"ok": True}


@router.post("/tasks/{listing_id}/failed")
async def failed(listing_id: int, body: FailedIn) -> dict:
    error = {"logged_out": "не вошёл в аккаунт", "blocked": "площадка остановила"}.get(body.reason, "ошибка")
    ext = await marketplaces.failed(listing_id, f"{error}: {body.error}")
    final = ext.status == ExternalStatus.FAILED or body.reason in ("logged_out", "blocked")
    await _notify("marketplace_failed", listing_id, f"{error}: {body.error}", final)
    return {"ok": True}


@router.post("/tasks/{listing_id}/removed")
async def removed(listing_id: int) -> dict:
    await marketplaces.removed(listing_id)
    await _notify("marketplace_removed", listing_id)
    return {"ok": True}
