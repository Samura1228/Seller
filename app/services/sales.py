"""Reservations, sales, withdrawal, and keeping the posts in chats in sync with the item."""

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload
from telethon import TelegramClient, errors

from app.config import get_settings
from app.db.enums import (
    Actor,
    ApprovalStatus,
    InquiryStatus,
    ItemStatus,
    PublicationStatus,
    ReservationStatus,
)
from app.db.models import Approval, Chat, Inquiry, Item, Publication, Reservation, Sale
from app.db.session import session_scope
from app.services import marketplaces
from app.services.items import set_status
from app.services.listing import render_publication
from app.userbot.sender import delete_post, edit_post_text

log = logging.getLogger(__name__)

ACTIVE_FOR_SALE = (ItemStatus.READY, ItemStatus.PUBLISHED, ItemStatus.HAS_INQUIRIES, ItemStatus.RESERVED)


class SaleError(Exception):
    pass


@dataclass
class SaleResult:
    sold_out: bool
    left: int
    final_price: Decimal


@dataclass
class PostsResult:
    done: int = 0
    failed: list[str] = field(default_factory=list)
    bazaraki_to_remove: list[str] = field(default_factory=list)  # marketplaces the helper will clean up


async def _open_status(s, item: Item) -> ItemStatus:
    """Status an item returns to when it is no longer reserved."""
    if item.first_published_at is None:
        return ItemStatus.READY
    has_inq = await s.scalar(select(func.count()).select_from(Inquiry).where(Inquiry.item_id == item.id))
    return ItemStatus.HAS_INQUIRIES if has_inq else ItemStatus.PUBLISHED


async def reserve(item_id: int, buyer_id: int | None = None) -> None:
    async with session_scope() as s:
        item = await s.get(Item, item_id)
        if item.status not in (ItemStatus.READY, ItemStatus.PUBLISHED, ItemStatus.HAS_INQUIRIES):
            raise SaleError("забронировать можно только вещь в продаже")
        await set_status(s, item, ItemStatus.RESERVED, Actor.ME, f"reserved for buyer {buyer_id}")
        s.add(Reservation(item_id=item_id, buyer_id=buyer_id, quantity=1))


async def unreserve(item_id: int) -> ItemStatus:
    async with session_scope() as s:
        item = await s.get(Item, item_id)
        if item.status != ItemStatus.RESERVED:
            raise SaleError("вещь не в брони")
        await _release_reservations(s, item_id, ReservationStatus.RELEASED)
        new = await _open_status(s, item)
        await set_status(s, item, new, Actor.ME, "reservation released")
        return new


async def _release_reservations(s, item_id: int, status: ReservationStatus) -> None:
    for r in await s.scalars(select(Reservation).where(
        Reservation.item_id == item_id, Reservation.status == ReservationStatus.ACTIVE
    )):
        r.status = status


async def sell(item_id: int, price: Decimal | None = None, buyer_id: int | None = None, quantity: int = 1) -> SaleResult:
    """Records the sale of `quantity` units. The last unit makes the item SOLD."""
    async with session_scope() as s:
        item = await s.get(Item, item_id)
        if item.status not in ACTIVE_FOR_SALE:
            raise SaleError("эта вещь не в продаже")
        quantity = max(1, min(quantity, item.quantity_left))
        final = price if price is not None else item.price
        if final is None:
            raise SaleError("у вещи нет цены")
        s.add(Sale(item_id=item_id, buyer_id=buyer_id, quantity=quantity, final_price=final))
        item.quantity_sold += quantity
        if item.quantity_left == 0:
            await _release_reservations(s, item_id, ReservationStatus.CONVERTED)
            await set_status(s, item, ItemStatus.SOLD, Actor.ME, f"sold for {final}")
            await _close_open_cards(s, item_id)
            await _close_inquiries(s, item_id)
            return SaleResult(sold_out=True, left=0, final_price=final)
        if item.status == ItemStatus.RESERVED:
            await _release_reservations(s, item_id, ReservationStatus.CONVERTED)
            await set_status(s, item, await _open_status(s, item), Actor.ME, "one unit sold, rest back on sale")
        return SaleResult(sold_out=False, left=item.quantity_left, final_price=final)


async def _close_open_cards(s, item_id: int) -> None:
    for a in await s.scalars(select(Approval).where(
        Approval.item_id == item_id, Approval.status.in_([ApprovalStatus.PENDING, ApprovalStatus.SKIPPED])
    )):
        a.status, a.resolved_at = ApprovalStatus.REJECTED, datetime.now(UTC)


async def _close_inquiries(s, item_id: int) -> None:
    for q in await s.scalars(select(Inquiry).where(Inquiry.item_id == item_id, Inquiry.status != InquiryStatus.HANDLED)):
        q.status = InquiryStatus.HANDLED


# ---------- posts in chats ----------

async def remove_posts(client: TelegramClient | None, item_id: int) -> PostsResult:
    """Deletes the item's posts in every chat (sold out / withdrawn). Queued posts are dropped."""
    res = PostsResult()
    async with session_scope() as s:
        rows = (await s.execute(
            select(Publication, Chat).join(Chat, Chat.id == Publication.chat_id)
            .where(Publication.item_id == item_id, Publication.status == PublicationStatus.POSTED)
        )).all()
        for p in await s.scalars(select(Publication).where(
            Publication.item_id == item_id,
            Publication.status.in_([PublicationStatus.DRAFT, PublicationStatus.APPROVED, PublicationStatus.QUEUED]),
        )):
            await s.delete(p)
        for ext in await marketplaces.item_gone(s, item_id):  # the helper on the Mac deletes these ads
            res.bazaraki_to_remove.append(marketplaces.NAMES[ext.platform])

    for pub, chat in rows:
        name = chat.title or chat.username or str(chat.id)
        if client is None:
            res.failed.append(f"{name}: нет сессии аккаунта для продаж")
            continue
        try:
            await delete_post(client, chat.tg_chat_id, chat.username, list(pub.tg_message_ids))
        except (errors.MessageDeleteForbiddenError, errors.ChatAdminRequiredError, errors.ChannelPrivateError,
                errors.UserBannedInChannelError, ValueError) as e:
            res.failed.append(f"{name}: {type(e).__name__}")
            continue
        except errors.RPCError as e:
            log.exception("Deleting publication %s failed", pub.id)
            res.failed.append(f"{name}: {type(e).__name__}")
            continue
        async with session_scope() as s:
            p = await s.get(Publication, pub.id)
            p.status, p.deleted_at = PublicationStatus.DELETED, datetime.now(UTC)
        res.done += 1
    return res


async def refresh_posts(client: TelegramClient | None, item_id: int) -> PostsResult:
    """Re-renders posted texts (quantity / price changed) and edits them in place."""
    res = PostsResult()
    seller = get_settings().seller_username
    async with session_scope() as s:
        item = await s.get(Item, item_id, options=[selectinload(Item.photos)])
        rows = (await s.execute(
            select(Publication, Chat).join(Chat, Chat.id == Publication.chat_id)
            .where(Publication.item_id == item_id, Publication.status == PublicationStatus.POSTED)
        )).all()
        jobs = [(p, c, render_publication(item, p.text, p.hashtags or [], p.language, seller)) for p, c in rows]

    for pub, chat, text in jobs:
        name = chat.title or chat.username or str(chat.id)
        if client is None:
            res.failed.append(f"{name}: нет сессии аккаунта для продаж")
            continue
        msg_id = pub.text_message_id or (pub.tg_message_ids[0] if pub.tg_message_ids else None)
        if msg_id is None:
            continue
        try:
            await edit_post_text(client, chat.tg_chat_id, chat.username, msg_id, text)
        except errors.MessageNotModifiedError:
            pass
        except (errors.RPCError, ValueError) as e:
            res.failed.append(f"{name}: {type(e).__name__}")
            continue
        res.done += 1
    return res
