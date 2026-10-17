"""Buyer messages → inquiries matched to items (DMs, replies to our posts)."""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import any_, literal, or_, select

from app.ai import prompts
from app.ai.client import structured
from app.ai.schemas import InquiryMatch
from app.config import get_settings
from app.db.enums import Actor, InquirySource, InquiryStatus, ItemStatus, MatchMethod, PublicationStatus
from app.db.models import Buyer, Inquiry, Item, Publication
from app.db.session import session_scope
from app.services.items import set_status
from app.services.listing import fmt_eur
from app.services.sales import ACTIVE_FOR_SALE

log = logging.getLogger(__name__)

CONVERSATION_WINDOW = timedelta(hours=6)
MIN_CONFIDENCE = 0.6


@dataclass
class BuyerInfo:
    tg_user_id: int
    username: str | None
    display_name: str | None


@dataclass
class Incoming:
    msg_id: int
    text: str


@dataclass
class Outcome:
    inquiry_id: int
    is_new: bool  # False: an earlier unidentified inquiry got its item


async def upsert_buyer(s, b: BuyerInfo) -> Buyer:
    buyer = await s.scalar(select(Buyer).where(Buyer.tg_user_id == b.tg_user_id))
    if buyer is None:
        buyer = Buyer(tg_user_id=b.tg_user_id)
        s.add(buyer)
    buyer.username = b.username
    buyer.display_name = (b.display_name or "")[:255] or None
    await s.flush()
    return buyer


async def find_publication(tg_chat_id: int, msg_id: int) -> int | None:
    from app.db.models import Chat

    async with session_scope() as s:
        return await s.scalar(
            select(Publication.id).join(Chat, Chat.id == Publication.chat_id)
            .where(Chat.tg_chat_id == tg_chat_id, literal(msg_id) == any_(Publication.tg_message_ids),
                   Publication.status.in_([PublicationStatus.POSTED, PublicationStatus.MARKED_SOLD]))
        )


async def _mark_has_inquiries(s, item: Item) -> None:
    if item.status == ItemStatus.PUBLISHED:
        await set_status(s, item, ItemStatus.HAS_INQUIRIES, Actor.SYSTEM, "first inquiry")


async def handle_reply(buyer: BuyerInfo, tg_chat_id: int, msg: Incoming, publication_id: int) -> Outcome | None:
    """A reply to one of our posts in a chat: the item is known for sure."""
    async with session_scope() as s:
        if await s.scalar(select(Inquiry.id).where(Inquiry.tg_chat_id == tg_chat_id, Inquiry.tg_message_id == msg.msg_id)):
            return None
        b = await upsert_buyer(s, buyer)
        pub = await s.get(Publication, publication_id)
        item = await s.get(Item, pub.item_id)
        inq = Inquiry(item_id=item.id, publication_id=pub.id, buyer_id=b.id, source=InquirySource.REPLY,
                      tg_chat_id=tg_chat_id, tg_message_id=msg.msg_id, text=msg.text[:4000],
                      match_method=MatchMethod.REPLY, match_confidence=1.0, status=InquiryStatus.NEW)
        s.add(inq)
        await _mark_has_inquiries(s, item)
        await s.flush()
        return Outcome(inq.id, True)


def _items_prompt(items: list[Item]) -> str:
    lines = []
    for i in items:
        bits = [i.display_name]
        if i.brand or i.model:
            bits.append(" ".join(filter(None, [i.brand, i.model])))
        if i.specs:
            bits.append(", ".join(f"{k}: {v}" for k, v in list(i.specs.items())[:4]))
        state = "SOLD" if i.status == ItemStatus.SOLD else ("reserved" if i.status == ItemStatus.RESERVED else "for sale")
        lines.append(f"[{i.id}] {' | '.join(bits)} | {fmt_eur(i.price)} | {state}")
    return "\n".join(lines)


async def handle_dm(buyer: BuyerInfo, messages: list[Incoming]) -> Outcome | None:
    """A batch of private messages from one person. Returns what to notify the owner about (or None)."""
    if not messages:
        return None
    now = datetime.now(UTC)
    text = "\n".join(m.text for m in messages).strip()[:4000]
    last_id = max(m.msg_id for m in messages)

    async with session_scope() as s:
        if await s.scalar(select(Inquiry.id).where(Inquiry.tg_chat_id == buyer.tg_user_id, Inquiry.tg_message_id >= last_id)):
            return None  # already processed (e.g. after a restart scan)
        b = await upsert_buyer(s, buyer)
        previous = await s.scalar(
            select(Inquiry).where(Inquiry.buyer_id == b.id, Inquiry.created_at >= now - CONVERSATION_WINDOW)
            .order_by(Inquiry.id.desc()).limit(1))
        prev_item = await s.get(Item, previous.item_id) if previous and previous.item_id else None
        items = list(await s.scalars(select(Item).where(or_(
            Item.status.in_(ACTIVE_FOR_SALE),
            (Item.status == ItemStatus.SOLD) & (Item.sold_at >= now - timedelta(days=14)),
        )).order_by(Item.id).limit(120)))
        buyer_id = b.id

    if not items:
        return None
    context = f"Earlier in this conversation the buyer asked about item [{prev_item.id}] {prev_item.display_name}.\n\n" \
        if prev_item else ""
    match = await structured(
        purpose="inquiry_match", output=InquiryMatch, system=prompts.load("inquiry_match"),
        content=f"Seller's items:\n{_items_prompt(items)}\n\n{context}Buyer's message(s):\n{text}",
        model=get_settings().ai_model_fast, max_tokens=600,
    )
    valid_ids = {i.id for i in items}
    item_id = match.item_id if match.item_id in valid_ids and match.confidence >= MIN_CONFIDENCE else None

    async with session_scope() as s:
        previous = await s.get(Inquiry, previous.id) if previous else None
        if previous is not None:
            if previous.item_id is None:
                if item_id is None:
                    return None  # still unclear; the owner was already told about this person
                # The unclear first message is now clarified: attach the item and notify again.
                previous.item_id = item_id
                previous.match_method, previous.match_confidence = MatchMethod.AI_TEXT, match.confidence
                previous.status = InquiryStatus.NEW
                previous.text = f"{previous.text}\n{text}"[:4000]
                previous.tg_message_id = last_id
                previous.summary, previous.intent = match.summary_ru, match.intent
                await _mark_has_inquiries(s, await s.get(Item, item_id))
                return Outcome(previous.id, False)
            if item_id is None or item_id == previous.item_id:
                return None  # same conversation: the owner is already talking to this buyer
            # a different item in the same conversation → new inquiry below
        elif item_id is None and not match.sales_related:
            return None  # spam / unrelated: don't bother the owner

        inq = Inquiry(
            item_id=item_id, buyer_id=buyer_id, source=InquirySource.DM, tg_chat_id=buyer.tg_user_id,
            tg_message_id=last_id, text=text, intent=match.intent, summary=match.summary_ru,
            price_offer=Decimal(match.price_offer_eur) if match.price_offer_eur else None,
            match_method=MatchMethod.AI_TEXT if item_id else None, match_confidence=match.confidence,
            status=InquiryStatus.NEW if item_id else InquiryStatus.UNIDENTIFIED,
        )
        s.add(inq)
        if item_id:
            await _mark_has_inquiries(s, await s.get(Item, item_id))
        await s.flush()
        return Outcome(inq.id, True)


async def assign_item(inquiry_id: int, item_id: int) -> None:
    async with session_scope() as s:
        inq = await s.get(Inquiry, inquiry_id)
        inq.item_id, inq.match_method, inq.match_confidence = item_id, MatchMethod.MANUAL, 1.0
        inq.status = InquiryStatus.NEW
        await _mark_has_inquiries(s, await s.get(Item, item_id))


async def dismiss(inquiry_id: int) -> None:
    async with session_scope() as s:
        (await s.get(Inquiry, inquiry_id)).status = InquiryStatus.HANDLED


async def candidate_items(limit: int = 6) -> list[Item]:
    """Items to offer when the owner assigns an unidentified inquiry: most recently published first."""
    async with session_scope() as s:
        return list(await s.scalars(
            select(Item).where(Item.status.in_(ACTIVE_FOR_SALE))
            .order_by(Item.first_published_at.desc().nulls_last(), Item.id.desc()).limit(limit)))
