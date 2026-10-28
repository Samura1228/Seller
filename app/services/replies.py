"""Suggested replies to buyers: the owner still sends them himself from the selling account."""

import logging
from dataclasses import dataclass

from sqlalchemy.orm import selectinload
from telethon import TelegramClient

from app.ai import prompts
from app.ai.client import structured
from app.ai.schemas import ReplySuggestion
from app.config import get_settings
from app.db.enums import InquirySource, ItemStatus, Language
from app.db.models import Buyer, Inquiry, Item
from app.db.session import session_scope
from app.services.copywriting import master_text
from app.services.listing import fmt_eur
from app.services.recognition import item_facts

log = logging.getLogger(__name__)
HISTORY_LIMIT = 16


@dataclass
class Suggestion:
    text: str
    tip: str
    below_min: bool  # the reply names a price under the minimum (shouldn't happen; shown as a warning)


async def conversation(client: TelegramClient | None, buyer_tg_id: int) -> list[str]:
    """Last messages of the private chat, oldest first. Reading doesn't mark them as read."""
    if client is None:
        return []
    try:
        msgs = await client.get_messages(buyer_tg_id, limit=HISTORY_LIMIT)
    except Exception as e:  # noqa: BLE001 - the suggestion still works from the inquiry alone
        log.warning("Couldn't read the chat with %s: %s", buyer_tg_id, e)
        return []
    return [f"{'Seller' if m.out else 'Buyer'}: {m.message.strip()[:500]}"
            for m in reversed(msgs) if getattr(m, "message", None) and m.message.strip()]


async def suggest(inquiry_id: int, client: TelegramClient | None, previous: str | None = None) -> Suggestion:
    async with session_scope() as s:
        inq = await s.get(Inquiry, inquiry_id)
        item = await s.get(Item, inq.item_id, options=[selectinload(Item.texts)]) if inq.item_id else None
        buyer = await s.get(Buyer, inq.buyer_id) if inq.buyer_id else None
    history = await conversation(client, buyer.tg_user_id) if buyer and inq.source == InquirySource.DM else []
    if not history:
        history = [f"Buyer: {inq.text or ''}"]

    if item is not None:
        listing = master_text(item, Language.RU) or master_text(item, Language.EN)
        state = {ItemStatus.RESERVED: "RESERVED for another buyer", ItemStatus.SOLD: "SOLD"}.get(item.status, "for sale")
        about = (f"Item card:\n{item_facts(item)}\n\n"
                 + (f"[Listing the buyer saw]\n{listing.title}\n{listing.body}\n\n" if listing else "")
                 + f"Price: {fmt_eur(item.price)}; seller's MINIMUM (secret): {fmt_eur(item.min_price)}; "
                 f"new retail price: {fmt_eur(item.new_price_ref) if item.new_price_ref else 'unknown'}; "
                 f"left: {item.quantity_left} of {item.quantity_total}; status: {state}")
    else:
        about = "The item is not identified yet: ask the buyer which item they mean."
    content = (f"{about}\n\nSale terms: Limassol only, in-person meetup, no delivery, price negotiable.\n\n"
               f"[Conversation, oldest first]\n" + "\n".join(history)
               + (f"\n\n[Earlier suggestion — write a different one]\n{previous}" if previous else ""))
    out = await structured(purpose="reply_suggest", output=ReplySuggestion, system=prompts.load("reply_suggest"),
                           content=content[:20000], item_id=item.id if item else None,
                           model=get_settings().ai_model, max_tokens=800)
    below = bool(item and item.min_price is not None and out.price_named_eur is not None
                 and out.price_named_eur < item.min_price)
    return Suggestion(text=out.reply.strip(), tip=out.tip_ru.strip(), below_min=below)
