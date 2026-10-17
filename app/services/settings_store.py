"""Small persistent key-value state (publisher switches, pauses) in the settings table."""

from datetime import datetime
from typing import Any

from sqlalchemy.dialects.postgresql import insert

from app.db.models import Setting
from app.db.session import session_scope


async def get(key: str, default: Any = None) -> Any:
    async with session_scope() as s:
        row = await s.get(Setting, key)
        return row.value if row else default


async def put(key: str, value: Any) -> None:
    async with session_scope() as s:
        stmt = insert(Setting).values(key=key, value=value)
        await s.execute(stmt.on_conflict_do_update(index_elements=[Setting.key], set_={"value": value}))


async def get_time(key: str) -> datetime | None:
    v = await get(key)
    return datetime.fromisoformat(v) if v else None


async def put_time(key: str, value: datetime | None) -> None:
    await put(key, value.isoformat() if value else None)
