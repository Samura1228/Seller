"""Tools of the /agent assistant: it reads everything; changes are only PROPOSED and applied after the owner's ✅."""

import io
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from PIL import Image
from sqlalchemy import func, or_, select
from sqlalchemy.orm import selectinload

from app.ai.client import image_block
from app.config import get_settings
from app.db.enums import ItemStatus, Language, PublicationStatus
from app.db.models import AiUsage, Buyer, Chat, ExternalListing, Inquiry, Item, PriceHistory, Publication, Sale
from app.db.session import session_scope
from app.services import pricing
from app.services.copywriting import master_text
from app.services.listing import fmt_eur
from app.services.recognition import item_facts

log = logging.getLogger(__name__)

ACTIONS = ("set_price", "reserve", "unreserve", "mark_sold", "withdraw", "edit_text")

TOOLS: list[dict[str, Any]] = [
    {"name": "list_items", "description": "List the seller's items with status, price, minimum, inquiries and where "
     "they are posted. Optional filter by status or a text query on the name.",
     "input_schema": {"type": "object", "properties": {
         "status": {"type": "string", "enum": [s.value for s in ItemStatus]},
         "query": {"type": "string", "description": "Part of the item name, any language"}}}},
    {"name": "get_item", "description": "Everything about one item: facts, condition, price and minimum, new retail "
     "price, price history, approved listing texts, chat posts, marketplace ads, recent inquiries.",
     "input_schema": {"type": "object", "properties": {"item_id": {"type": "integer"}}, "required": ["item_id"]}},
    {"name": "get_item_photos", "description": "Look at an item's photos (up to 4).",
     "input_schema": {"type": "object", "properties": {"item_id": {"type": "integer"}}, "required": ["item_id"]}},
    {"name": "list_inquiries", "description": "Recent buyer inquiries (newest first), optionally for one item.",
     "input_schema": {"type": "object", "properties": {"item_id": {"type": "integer"},
                                                       "limit": {"type": "integer", "description": "default 15"}}}},
    {"name": "read_conversation", "description": "Last messages of the private chat with a buyer (by inquiry id or "
     "@username), from the selling account. Reading doesn't mark messages as read.",
     "input_schema": {"type": "object", "properties": {"inquiry_id": {"type": "integer"},
                                                       "username": {"type": "string"}}}},
    {"name": "list_chats", "description": "Telegram buy/sell chats: active or not, language, limits, last post.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "stats", "description": "Overall numbers: items by status, sold count, revenue, average days to sell, "
     "inquiries, AI spend.", "input_schema": {"type": "object", "properties": {}}},
    {"name": "propose_change", "description": "Propose a change. It is NOT applied: the owner sees a card with "
     "✅/❌ buttons and decides. Actions: set_price (price, optional min_price), reserve, unreserve, "
     "mark_sold (optional price = final price), withdraw (stop selling, delete posts), edit_text (instruction: "
     "how to change the listing text, in Russian).",
     "input_schema": {"type": "object", "properties": {
         "action": {"type": "string", "enum": list(ACTIONS)},
         "item_id": {"type": "integer"},
         "price": {"type": "number"}, "min_price": {"type": "number"},
         "instruction": {"type": "string"},
         "summary_ru": {"type": "string", "description": "One line for the owner: what changes and why"}},
         "required": ["action", "item_id", "summary_ru"]}},
]


@dataclass
class Proposal:
    action: str
    item_id: int
    summary_ru: str
    price: Decimal | None = None
    min_price: Decimal | None = None
    instruction: str | None = None


@dataclass
class ToolContext:
    userbot: Any = None
    proposals: list[Proposal] = field(default_factory=list)


def _d(v) -> str | None:
    return fmt_eur(v) if v is not None else None


def _days(dt) -> int | None:
    return (datetime.now(UTC) - dt).days if dt else None


async def run_tool(name: str, args: dict, ctx: ToolContext) -> str | list[dict]:
    fn = _TOOLS.get(name)
    if fn is None:
        return f"unknown tool {name}"
    try:
        return await fn(args, ctx)
    except Exception as e:  # noqa: BLE001 - the model sees the error and can recover
        log.exception("Agent tool %s failed", name)
        return f"error: {type(e).__name__}: {e}"


def _json(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, default=str)[:15000]


async def _list_items(args: dict, ctx: ToolContext) -> str:
    async with session_scope() as s:
        q = select(Item).order_by(Item.id)
        if args.get("status"):
            q = q.where(Item.status == ItemStatus(args["status"]))
        if args.get("query"):
            like = f"%{args['query']}%"
            q = q.where(or_(Item.title.ilike(like), Item.brand.ilike(like), Item.model.ilike(like)))
        items = list(await s.scalars(q.limit(80)))
        inq = dict((await s.execute(select(Inquiry.item_id, func.count()).group_by(Inquiry.item_id))).all())
        posts = dict((await s.execute(select(Publication.item_id, func.count()).where(
            Publication.status == PublicationStatus.POSTED).group_by(Publication.item_id))).all())
    return _json([{"id": i.id, "name": i.display_name, "status": i.status.value, "price": _d(i.price),
                   "min_price": _d(i.min_price), "left": f"{i.quantity_left}/{i.quantity_total}",
                   "days_listed": _days(i.first_published_at), "inquiries": inq.get(i.id, 0),
                   "posts_in_chats": posts.get(i.id, 0)} for i in items])


async def _get_item(args: dict, ctx: ToolContext) -> str:
    item_id = int(args["item_id"])
    async with session_scope() as s:
        item = await s.get(Item, item_id, options=[selectinload(Item.texts)])
        if item is None:
            return "no such item"
        history = list(await s.scalars(select(PriceHistory).where(PriceHistory.item_id == item_id)
                                       .order_by(PriceHistory.id.desc()).limit(10)))
        research = await pricing.latest_research(s, item_id)
        pubs = (await s.execute(select(Publication, Chat).join(Chat, Chat.id == Publication.chat_id)
                                .where(Publication.item_id == item_id))).all()
        exts = list(await s.scalars(select(ExternalListing).where(ExternalListing.item_id == item_id)))
        inqs = (await s.execute(select(Inquiry, Buyer).outerjoin(Buyer, Buyer.id == Inquiry.buyer_id)
                                .where(Inquiry.item_id == item_id).order_by(Inquiry.id.desc()).limit(10))).all()
        ru, en = master_text(item, Language.RU), master_text(item, Language.EN)
        return _json({
            "id": item.id, "name": item.display_name, "status": item.status.value, "facts": json.loads(item_facts(item)),
            "price": _d(item.price), "min_price": _d(item.min_price), "new_retail_price": _d(item.new_price_ref),
            "left": f"{item.quantity_left}/{item.quantity_total}", "days_listed": _days(item.first_published_at),
            "price_reasoning": research.rationale if research else None,
            "price_history": [{"price": _d(p.price), "min": _d(p.min_price), "reason": p.reason.value,
                               "when": p.created_at.date()} for p in reversed(history)],
            "listing_ru": f"{ru.title}\n{ru.body}" if ru else None,
            "listing_en": f"{en.title}\n{en.body}" if en else None,
            "owner_text_edits": item.text_rules,
            "chat_posts": [{"chat": c.title, "status": p.status.value, "posted": p.posted_at} for p, c in pubs],
            "marketplaces": [{"platform": e.platform.value, "status": e.status.value, "url": e.url} for e in exts],
            "inquiries": [{"id": q.id, "when": q.created_at, "buyer": f"@{b.username}" if b and b.username else
                           (b.display_name if b else None), "text": (q.text or "")[:300],
                           "offer": _d(q.price_offer)} for q, b in inqs],
        })


async def _get_item_photos(args: dict, ctx: ToolContext) -> list[dict] | str:
    item_id = int(args["item_id"])
    async with session_scope() as s:
        item = await s.get(Item, item_id, options=[selectinload(Item.photos)])
    if item is None:
        return "no such item"
    blocks: list[dict] = [{"type": "text", "text": f"Photos of #{item.id} {item.display_name}"}]
    for p in item.photos[:4]:
        path = get_settings().data_dir / p.path
        if path.exists():
            blocks.append(image_block(shrink(path.read_bytes())))
    return blocks if len(blocks) > 1 else "no photos on disk"


def shrink(data: bytes, side: int = 1280) -> bytes:
    """Any image → a JPEG no larger than `side` px (keeps tokens and request size down)."""
    img = Image.open(io.BytesIO(data))
    img = img.convert("RGB")
    img.thumbnail((side, side))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=85)
    return out.getvalue()


async def _list_inquiries(args: dict, ctx: ToolContext) -> str:
    async with session_scope() as s:
        q = (select(Inquiry, Item, Buyer).outerjoin(Item, Item.id == Inquiry.item_id)
             .outerjoin(Buyer, Buyer.id == Inquiry.buyer_id).order_by(Inquiry.id.desc())
             .limit(min(int(args.get("limit") or 15), 50)))
        if args.get("item_id"):
            q = q.where(Inquiry.item_id == int(args["item_id"]))
        rows = (await s.execute(q)).all()
    return _json([{"inquiry_id": q.id, "when": q.created_at, "item": f"#{i.id} {i.display_name}" if i else None,
                   "buyer": f"@{b.username}" if b and b.username else (b.display_name if b else None),
                   "text": (q.text or "")[:300], "offer": _d(q.price_offer), "summary": q.summary}
                  for q, i, b in rows])


async def _read_conversation(args: dict, ctx: ToolContext) -> str:
    if ctx.userbot is None:
        return "the selling account is not connected"
    peer: Any = None
    if args.get("inquiry_id"):
        async with session_scope() as s:
            inq = await s.get(Inquiry, int(args["inquiry_id"]))
            buyer = await s.get(Buyer, inq.buyer_id) if inq and inq.buyer_id else None
        peer = buyer.tg_user_id if buyer else None
    elif args.get("username"):
        peer = "@" + args["username"].lstrip("@")
    if peer is None:
        return "give inquiry_id or username"
    msgs = await ctx.userbot.get_messages(peer, limit=25)
    return "\n".join(f"{m.date:%d.%m %H:%M} {'Seller' if m.out else 'Buyer'}: {m.message}"
                     for m in reversed(msgs) if getattr(m, "message", None)) or "no messages"


async def _list_chats(args: dict, ctx: ToolContext) -> str:
    async with session_scope() as s:
        chats = list(await s.scalars(select(Chat).order_by(Chat.id)))
    return _json([{"id": c.id, "title": c.title, "username": c.username, "active": c.is_active,
                   "can_post": c.can_post, "language": (c.language_override or c.language).value if
                   (c.language_override or c.language) else None, "members": c.members_count,
                   "last_post": c.last_post_at, "note": c.check_note} for c in chats])


async def _stats(args: dict, ctx: ToolContext) -> str:
    async with session_scope() as s:
        by_status = {k.value: v for k, v in (await s.execute(select(Item.status, func.count()).group_by(Item.status))).all()}
        sales = (await s.execute(select(Sale, Item).join(Item, Item.id == Sale.item_id))).all()
        inquiries = await s.scalar(select(func.count()).select_from(Inquiry))
        ai = await s.scalar(select(func.coalesce(func.sum(AiUsage.cost_usd), 0)))
    revenue = sum((sale.final_price * sale.quantity for sale, _ in sales), Decimal(0))
    days = [(sale.sold_at - item.first_published_at).days for sale, item in sales if item.first_published_at]
    return _json({"items_by_status": by_status, "sales": len(sales), "revenue": fmt_eur(revenue),
                  "avg_days_to_sell": round(sum(days) / len(days), 1) if days else None,
                  "inquiries": inquiries, "ai_spend_usd": float(ai)})


async def _propose(args: dict, ctx: ToolContext) -> str:
    action = args["action"]
    if action not in ACTIONS:
        return f"unknown action {action}"
    async with session_scope() as s:
        item = await s.get(Item, int(args["item_id"]))
    if item is None:
        return "no such item"
    p = Proposal(action=action, item_id=item.id, summary_ru=args.get("summary_ru") or action,
                 price=Decimal(str(args["price"])) if args.get("price") is not None else None,
                 min_price=Decimal(str(args["min_price"])) if args.get("min_price") is not None else None,
                 instruction=args.get("instruction"))
    if action == "set_price" and p.price is None:
        return "set_price needs price"
    if action == "edit_text" and not p.instruction:
        return "edit_text needs instruction"
    ctx.proposals.append(p)
    return "Shown to the owner as a card with ✅/❌ buttons. NOT applied yet — tell the owner to confirm it."


_TOOLS = {"list_items": _list_items, "get_item": _get_item, "get_item_photos": _get_item_photos,
          "list_inquiries": _list_inquiries, "read_conversation": _read_conversation, "list_chats": _list_chats,
          "stats": _stats, "propose_change": _propose}
