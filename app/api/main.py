"""HTTP API: health check and the marketplace helper's API. The Mini App endpoints come in stage 4."""

from fastapi import FastAPI
from sqlalchemy import text

from app.api.helper import router as helper_router
from app.db.session import session_scope

api = FastAPI(title="Limassol Seller", docs_url=None, redoc_url=None)
api.include_router(helper_router)


@api.get("/health")
async def health() -> dict[str, str]:
    async with session_scope() as s:
        await s.execute(text("select 1"))
    return {"status": "ok"}
