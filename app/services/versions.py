"""Per-chat versions of an item's master text (Publication drafts) and their approval."""

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import selectinload

from app.ai import prompts
from app.ai.client import structured
from app.ai.schemas import ChatVersion
from app.config import get_settings
from app.db.enums import ApprovalKind, ApprovalStatus, ItemStatus, Language, PublicationStatus
from app.db.models import Approval, Chat, Item, Publication
from app.db.session import session_scope
from app.services import chats as chat_service
from app.services.copywriting import master_text, text_rules_block
from app.services.listing import footer, strip_emoji

log = logging.getLogger(__name__)

SELLABLE = (ItemStatus.READY, ItemStatus.PUBLISHED, ItemStatus.HAS_INQUIRIES)
CAPTION_LIMIT = 1024  # Telegram caption limit for non-premium accounts
_gen_lock = asyncio.Lock()


def text_budget(item: Item, chat: Chat, lang: Language, seller: str) -> int:
    """Characters available for title + body so the whole post fits in one photo caption."""
    rules = chat.rules or {}
    limit = CAPTION_LIMIT if chat.can_send_photos else 4096
    if rules.get("max_text_length"):
        limit = min(limit, int(rules["max_text_length"]))
    reserved = len(footer(item, lang, seller)) + 60  # footer + hashtags + spacing
    return max(250, limit - reserved)


async def _active_chats(s) -> list[Chat]:
    return list(await s.scalars(select(Chat).where(Chat.is_active.is_(True), Chat.can_post.is_(True)).order_by(Chat.id)))


async def generate_for_item(item_id: int, chat_ids: list[int] | None = None) -> int:
    """Creates drafts for active chats that have no version of this item yet. Returns the number created."""
    settings = get_settings()
    async with _gen_lock:  # avoid racing duplicates when an item and a chat are approved at the same time
        async with session_scope() as s:
            item = await s.get(Item, item_id, options=[selectinload(Item.texts)])
            if item is None or item.status not in SELLABLE or item.quantity_left == 0:
                return 0
            chats = await _active_chats(s)
            if chat_ids is not None:
                chats = [c for c in chats if c.id in chat_ids]
            existing = list(await s.scalars(select(Publication).where(Publication.item_id == item_id)))
            have = {p.chat_id for p in existing}
            todo = [c for c in chats if c.id not in have]
            if not todo:
                return 0
            siblings = [p.text for p in existing]
            rules_text = text_rules_block(item)

            created = 0
            for chat in todo:
                lang = Language(chat_service.post_language(chat))
                master = master_text(item, lang)
                if master is None:
                    continue
                rules = chat.rules or {}
                content = (
                    f"Language: {'Russian' if lang == Language.RU else 'English'}\n"
                    f"Character budget for title + body: {text_budget(item, chat, lang, settings.seller_username)}\n"
                    f"Chat: {chat.title}\n"
                    f"Format notes: {rules.get('format_notes_ru') or '-'}\n"
                    f"Required hashtags: {' '.join(rules.get('required_hashtags') or []) or '-'}\n"
                    f"Allowed hashtags: {' '.join(rules.get('allowed_hashtags') or []) or '-'}\n\n"
                    + (f"{rules_text}\n\n" if rules_text else "")
                    + f"[Master text]\n{master.title}\n\n{master.body}\n\n"
                    + "".join(f"[Version for another chat — write differently]\n{t}\n\n" for t in siblings[-3:])
                )
                v = await structured(purpose="chat_version", output=ChatVersion, system=prompts.load("chat_version"),
                                     content=content, item_id=item_id, model=settings.ai_model_fast, max_tokens=1500)
                text = f"{strip_emoji(v.title)}\n\n{strip_emoji(v.body)}"
                hashtags = _clean_hashtags(v.hashtags, rules)
                s.add(Publication(item_id=item_id, chat_id=chat.id, text=text, hashtags=hashtags, language=lang,
                                  status=PublicationStatus.DRAFT))
                siblings.append(text)
                created += 1

            if created:
                await _ensure_previews_approval(s, item_id)
            return created


def _clean_hashtags(tags: list[str], rules: dict) -> list[str]:
    out: list[str] = []
    for t in [*(rules.get("required_hashtags") or []), *tags]:
        t = t.strip()
        if not t:
            continue
        t = t if t.startswith("#") else f"#{t}"
        if t.lower() not in {x.lower() for x in out}:
            out.append(t)
    return out[:6]


async def generate_for_chat(chat_id: int) -> int:
    async with session_scope() as s:
        ids = list(await s.scalars(select(Item.id).where(Item.status.in_(SELLABLE)).order_by(Item.id)))
    total = 0
    for item_id in ids:
        try:
            total += await generate_for_item(item_id, [chat_id])
        except Exception:
            log.exception("Version generation failed for item %s chat %s", item_id, chat_id)
    return total


async def _ensure_previews_approval(s, item_id: int) -> Approval:
    approval = await s.scalar(select(Approval).where(
        Approval.item_id == item_id, Approval.kind == ApprovalKind.CHAT_PREVIEWS,
        Approval.status.in_([ApprovalStatus.PENDING, ApprovalStatus.SKIPPED]),
    ))
    if approval is None:
        approval = Approval(item_id=item_id, kind=ApprovalKind.CHAT_PREVIEWS)
        s.add(approval)
        await s.flush()
    return approval


async def drafts(s, item_id: int) -> list[tuple[Publication, Chat]]:
    rows = await s.execute(
        select(Publication, Chat).join(Chat, Chat.id == Publication.chat_id)
        .where(Publication.item_id == item_id, Publication.status == PublicationStatus.DRAFT)
        .order_by(Chat.id)
    )
    return [(p, c) for p, c in rows.all()]


async def _resolve(s, item_id: int, status: ApprovalStatus) -> None:
    for a in await s.scalars(select(Approval).where(
        Approval.item_id == item_id, Approval.kind == ApprovalKind.CHAT_PREVIEWS,
        Approval.status.in_([ApprovalStatus.PENDING, ApprovalStatus.SKIPPED]),
    )):
        a.status, a.resolved_at = status, datetime.now(UTC)


async def approve(item_id: int) -> int:
    async with session_scope() as s:
        rows = await drafts(s, item_id)
        for p, _ in rows:
            p.status = PublicationStatus.APPROVED
        await _resolve(s, item_id, ApprovalStatus.APPROVED)
        return len(rows)


async def regenerate(item_id: int) -> int:
    async with session_scope() as s:
        await s.execute(delete(Publication).where(
            Publication.item_id == item_id, Publication.status == PublicationStatus.DRAFT
        ))
    return await generate_for_item(item_id)


async def discard(item_id: int) -> None:
    """Owner doesn't want these versions: drop drafts (they'll be regenerated only on demand)."""
    async with session_scope() as s:
        await s.execute(delete(Publication).where(
            Publication.item_id == item_id, Publication.status == PublicationStatus.DRAFT
        ))
        await _resolve(s, item_id, ApprovalStatus.REJECTED)


async def drop_unposted(item_id: int) -> None:
    """Item is back in editing: versions not yet posted are stale and will be regenerated after approval."""
    async with session_scope() as s:
        await s.execute(delete(Publication).where(
            Publication.item_id == item_id,
            Publication.status.in_([PublicationStatus.DRAFT, PublicationStatus.APPROVED]),
        ))
        await _resolve(s, item_id, ApprovalStatus.REJECTED)
