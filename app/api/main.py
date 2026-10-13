"""HTTP API. Stage 1: health check only. Mini App and Bazaraki endpoints come in stages 4-5."""

from fastapi import FastAPI
from sqlalchemy import text

from app.db.session import session_scope

api = FastAPI(title="Limassol Seller", docs_url=None, redoc_url=None)


@api.get("/health")
async def health() -> dict[str, str]:
    async with session_scope() as s:
        await s.execute(text("select 1"))
    return {"status": "ok"}
