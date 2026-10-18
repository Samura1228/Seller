"""Chats: add/inspect, rules parsing, language, activation. Telegram access goes through the userbot client."""

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from telethon import TelegramClient

from app.ai import prompts
from app.ai.client import structured
from app.ai.schemas import ChatRules
from app.config import get_settings
from app.db.enums import ApprovalKind, ApprovalStatus, ChatLanguage, RulesStatus
from app.db.models import Approval, Chat
from app.db.session import session_scope
from app.userbot.chat_inspector import ChatCheckError, ChatSnapshot, detect_language, inspect_chat

log = logging.getLogger(__name__)


@dataclass
class AddResult:
    ref: str | int
    chat_id: int | None = None
    error: str | None = None
    note: str | None = None
    auto_rules: bool = False  # rules were read confidently and posting is allowed


def effective_language(chat: Chat) -> ChatLanguage:
    return chat.language_override or chat.language or ChatLanguage.RU


def post_language(chat: Chat) -> str:
    """Language for posts: mixed chats get Russian unless the owner overrides."""
    lang = effective_language(chat)
    return "en" if lang == ChatLanguage.EN else "ru"


def pick_topic(rules: ChatRules, snap: ChatSnapshot) -> int | None:
    ids = {t["id"] for t in snap.forum_topics if not t.get("closed")}
    if rules.sale_topic_id in ids:
        return rules.sale_topic_id
    return snap.guessed_topic_id


def needs_owner(rules: ChatRules, snap: ChatSnapshot) -> bool:
    return (
        (snap.kind == "forum" and pick_topic(rules, snap) is None)
        or not rules.rules_found
        or rules.confidence < 0.7
        or rules.posting_allowed is None
        or snap.pinned_media_without_text > 0
        or bool(rules.questions_ru)
    )


async def parse_rules(snap: ChatSnapshot, chat_id: int | None = None) -> ChatRules:
    sample = "\n---\n".join(snap.recent_texts[:25])
    content = (
        f"Chat: {snap.title} (@{snap.username or '-'}), type: {snap.kind}, members: {snap.members_count}, "
        f"slow mode: {snap.slowmode_seconds or 0}s\n\n"
        f"{snap.rules_source() or '[No description and no pinned messages]'}\n\n"
        f"[Sample of recent messages]\n{sample or '-'}"
    )
    return await structured(purpose="chat_rules", output=ChatRules, system=prompts.load("chat_rules"),
                            content=content[:30000], max_tokens=2000)


async def add_chat(client: TelegramClient, ref: str | int) -> AddResult:
    """Inspects a chat, parses rules, stores it (inactive) and queues a rules card for the owner.
    A channel where only admins post is replaced by its discussion group, if it has one."""
    try:
        snap = await inspect_chat(client, ref)
    except ChatCheckError as e:
        return AddResult(ref=ref, error=str(e))
    note = None
    if snap.kind == "channel" and not snap.can_post and snap.linked_chat_id:
        try:
            linked = await inspect_chat(client, snap.linked_chat_id)
        except (ChatCheckError, ValueError) as e:
            return AddResult(ref=ref, error=f"канал, а его чат-обсуждение недоступен ({e})")
        note = f"канал «{snap.title}» → добавил его чат «{linked.title}»"
        snap = linked

    rules = await parse_rules(snap)
    async with session_scope() as s:
        chat = await s.scalar(select(Chat).where(Chat.tg_chat_id == snap.tg_chat_id))
        if chat is None:
            chat = Chat(tg_chat_id=snap.tg_chat_id)
            s.add(chat)
        apply_snapshot(chat, snap)
        chat.rules = rules.model_dump(mode="json")
        if snap.kind == "forum":
            chat.forum_topic_id = pick_topic(rules, snap)
            if chat.forum_topic_id and chat.check_note and "выбери тему" in chat.check_note:
                chat.check_note = "; ".join(p for p in chat.check_note.split("; ") if "выбери тему" not in p) or None
        chat.rules_status = RulesStatus.NEEDS_ME if needs_owner(rules, snap) else RulesStatus.AUTO
        await s.flush()
        await ensure_rules_approval(s, chat.id)
        auto = chat.rules_status == RulesStatus.AUTO and chat.can_post and rules.posting_allowed is True
        return AddResult(ref=ref, chat_id=chat.id, note=note, auto_rules=auto)


def apply_snapshot(chat: Chat, snap: ChatSnapshot) -> None:
    chat.username = snap.username
    chat.title = (snap.title or "")[:255]
    chat.kind = snap.kind
    chat.members_count = snap.members_count
    chat.slowmode_seconds = snap.slowmode_seconds
    chat.can_post = snap.can_post
    chat.can_send_photos = snap.can_send_photos
    chat.check_note = "; ".join(snap.problems) or None
    chat.checked_at = datetime.now(UTC)
    chat.rules_raw = snap.rules_source()[:20000] or None
    chat.language = ChatLanguage(detect_language(snap.recent_texts))
    chat.forum_topics = snap.forum_topics
    if chat.forum_topic_id and chat.forum_topic_id not in {t["id"] for t in snap.forum_topics}:
        chat.forum_topic_id = None  # the topic was deleted
    if not snap.can_post:
        chat.is_active = False


async def ensure_rules_approval(s, chat_id: int) -> Approval:
    approval = await s.scalar(select(Approval).where(
        Approval.chat_id == chat_id, Approval.kind == ApprovalKind.CHAT_RULES,
        Approval.status.in_([ApprovalStatus.PENDING, ApprovalStatus.SKIPPED]),
    ))
    if approval is None:
        approval = Approval(chat_id=chat_id, kind=ApprovalKind.CHAT_RULES)
        s.add(approval)
        await s.flush()
    return approval


async def edit_rules(chat_id: int, correction: str) -> None:
    async with session_scope() as s:
        chat = await s.get(Chat, chat_id)
        current = json.dumps(chat.rules or {}, ensure_ascii=False, indent=1)
    rules = await structured(
        purpose="chat_rules_edit", output=ChatRules, system=prompts.load("chat_rules_edit"),
        content=f"Current rules (JSON):\n{current}\n\nOwner's correction:\n{correction}",
        model=get_settings().ai_model_fast, max_tokens=2000,
    )
    async with session_scope() as s:
        chat = await s.get(Chat, chat_id)
        chat.rules = rules.model_dump(mode="json")
        chat.notes = "\n".join(filter(None, [chat.notes, correction.strip()]))
        chat.rules_status = RulesStatus.CONFIRMED


async def needing_recheck() -> list[int]:
    """Chats we couldn't post to (e.g. forums before topic support, or the account wasn't a member yet)."""
    async with session_scope() as s:
        return list(await s.scalars(select(Chat.id).where(Chat.can_post.is_(False)).order_by(Chat.id)))


async def known_tg_ids() -> set[int]:
    async with session_scope() as s:
        return {i for i in await s.scalars(select(Chat.tg_chat_id)) if i is not None}


async def confirm(chat_id: int) -> Chat:
    """Owner confirmed the rules: the chat becomes active for publishing."""
    async with session_scope() as s:
        chat = await s.get(Chat, chat_id)
        if not chat.can_post:
            raise ValueError(f"в этот чат нельзя публиковать: {chat.check_note}")
        if chat.kind == "forum" and not chat.forum_topic_id:
            raise ValueError("это чат с темами — сначала выбери тему для объявлений")
        chat.rules_status = RulesStatus.CONFIRMED
        chat.is_active = True
        await _resolve_rules_approval(s, chat_id, ApprovalStatus.APPROVED)
        return chat


async def deactivate(chat_id: int) -> None:
    async with session_scope() as s:
        chat = await s.get(Chat, chat_id)
        chat.is_active = False
        await _resolve_rules_approval(s, chat_id, ApprovalStatus.REJECTED)


async def set_topic(chat_id: int, topic_id: int) -> str:
    async with session_scope() as s:
        chat = await s.get(Chat, chat_id)
        topic = next((t for t in chat.forum_topics or [] if t["id"] == topic_id), None)
        if topic is None:
            raise ValueError("такой темы нет")
        chat.forum_topic_id = topic_id
        if chat.check_note and "выбери тему" in chat.check_note:
            chat.check_note = None
        return topic["title"]


async def close_card(chat_id: int) -> None:
    async with session_scope() as s:
        await _resolve_rules_approval(s, chat_id, ApprovalStatus.APPROVED)


async def cycle_language(chat_id: int) -> ChatLanguage:
    order = [ChatLanguage.RU, ChatLanguage.EN, ChatLanguage.MIXED]
    async with session_scope() as s:
        chat = await s.get(Chat, chat_id)
        current = effective_language(chat)
        chat.language_override = order[(order.index(current) + 1) % len(order)]
        return chat.language_override


async def _resolve_rules_approval(s, chat_id: int, status: ApprovalStatus) -> None:
    approvals = await s.scalars(select(Approval).where(
        Approval.chat_id == chat_id, Approval.kind == ApprovalKind.CHAT_RULES,
        Approval.status.in_([ApprovalStatus.PENDING, ApprovalStatus.SKIPPED]),
    ))
    for a in approvals:
        a.status, a.resolved_at = status, datetime.now(UTC)


async def recheck(client: TelegramClient, chat_id: int) -> AddResult:
    """Re-reads the chat (e.g. after the selling account joined it). Keeps owner-confirmed rules."""
    async with session_scope() as s:
        chat = await s.get(Chat, chat_id)
        ref = f"@{chat.username}" if chat.username else str(chat.tg_chat_id)
        confirmed = chat.rules_status == RulesStatus.CONFIRMED
    if confirmed:
        try:
            snap = await inspect_chat(client, ref)
        except ChatCheckError as e:
            return AddResult(ref=ref, chat_id=chat_id, error=str(e))
        async with session_scope() as s:
            chat = await s.get(Chat, chat_id)
            was_active = chat.is_active
            apply_snapshot(chat, snap)
            if snap.kind == "forum" and not chat.forum_topic_id:
                chat.forum_topic_id = snap.guessed_topic_id
            chat.is_active = was_active and chat.can_post and (snap.kind != "forum" or bool(chat.forum_topic_id))
        return AddResult(ref=ref, chat_id=chat_id)
    return await add_chat(client, ref)
