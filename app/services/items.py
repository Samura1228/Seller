"""The only place where item status changes. Every change is logged to item_events."""

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import Actor, ItemStatus, PriceReason
from app.db.models import Item, ItemEvent, PriceHistory

S = ItemStatus
ALLOWED: dict[ItemStatus, set[ItemStatus]] = {
    S.NEW: {S.RECOGNIZED, S.WITHDRAWN},
    S.RECOGNIZED: {S.PENDING_APPROVAL, S.WITHDRAWN},
    S.PENDING_APPROVAL: {S.RECOGNIZED, S.READY, S.WITHDRAWN},
    S.READY: {S.PENDING_APPROVAL, S.PUBLISHED, S.RESERVED, S.SOLD, S.WITHDRAWN},
    S.PUBLISHED: {S.HAS_INQUIRIES, S.RESERVED, S.SOLD, S.WITHDRAWN},
    S.HAS_INQUIRIES: {S.PUBLISHED, S.RESERVED, S.SOLD, S.WITHDRAWN},
    S.RESERVED: {S.PUBLISHED, S.HAS_INQUIRIES, S.SOLD, S.WITHDRAWN},
    S.SOLD: set(),
    S.WITHDRAWN: {S.READY},
}


class InvalidTransition(Exception):
    pass


async def log_event(
    s: AsyncSession, item: Item, from_status: ItemStatus | None, to_status: ItemStatus, actor: Actor, note: str | None = None
) -> None:
    s.add(ItemEvent(item_id=item.id, from_status=from_status, to_status=to_status, actor=actor, note=note))


async def set_status(s: AsyncSession, item: Item, new: ItemStatus, actor: Actor, note: str | None = None) -> None:
    old = item.status
    if old == new:
        return
    if new not in ALLOWED[old]:
        raise InvalidTransition(f"{old} -> {new}")
    item.status = new
    now = datetime.now(UTC)
    if new == S.READY:
        item.approved_at = now
    elif new == S.SOLD:
        item.sold_at = now
    elif new == S.WITHDRAWN:
        item.withdrawn_at = now
    await log_event(s, item, old, new, actor, note)


async def set_price(
    s: AsyncSession, item: Item, price: Decimal, min_price: Decimal | None, reason: PriceReason, actor: Actor
) -> None:
    if item.price == price and item.min_price == min_price:
        return
    item.price = price
    item.min_price = min_price
    s.add(PriceHistory(item_id=item.id, price=price, min_price=min_price, reason=reason, actor=actor))
