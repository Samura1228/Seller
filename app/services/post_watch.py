"""Watches our published posts: notices when a chat's admins (or a bot) delete one, and looks for the reason.

One get_messages call per chat per round (cheap). A post counts as removed when none of its messages
(album photos + caption/text) exist any more.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import func, select
from telethon import TelegramClient, errors, types

from app.config import Settings
from app.db.enums import PublicationStatus
from app.db.models import Chat, Item, Publication
from app.db.session import session_scope
from app.services import chats as chat_service
from app.services.versions import SELLABLE
from app.userbot.chat_inspector import ChatCheckError, inspect_chat
from app.userbot.client import resolve_chat

log = logging.getLogger(__name__)

CLUE_SCAN_LIMIT = 300  # messages after our post to look through for a moderator's / bot's explanation
CLUE_MAX = 3


@dataclass
class Removal:
    publication_id: int
    item_id: int
    item_name: str
    chat_id: int
    chat_title: str
    posted_at: datetime | None
    clues: list[str] = field(default_factory=list)  # messages that replied to / mentioned our post or account
    chat_problem: str | None = None  # e.g. the account got muted after the post
    removed_in_chat: int = 1  # removals in this chat over the last 30 days, this one included
    can_repost: bool = False


@dataclass
class CheckResult:
    checked: int = 0
    removals: list[Removal] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class WatchNotifier(Protocol):
    async def post_removed(self, removal: Removal) -> None: ...
    async def publisher_alert(self, text: str) -> None: ...


class PostWatcher:
    def __init__(self, settings: Settings, client: TelegramClient, notifier: WatchNotifier):
        self.settings = settings
        self.client = client
        self.notifier = notifier
        self._lock = asyncio.Lock()
        self._me: types.User | None = None

    async def run(self) -> None:
        await asyncio.sleep(self.settings.post_check_first_delay_seconds)
        while True:
            try:
                await self.check_all()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Post check failed")
            await asyncio.sleep(self.settings.post_check_interval_minutes * 60)

    async def check_all(self) -> CheckResult:
        async with self._lock:
            res = CheckResult()
            async with session_scope() as s:
                rows = (await s.execute(
                    select(Publication, Chat).join(Chat, Chat.id == Publication.chat_id)
                    .where(Publication.status == PublicationStatus.POSTED)
                    .order_by(Chat.id, Publication.id)
                )).all()
            by_chat: dict[int, tuple[Chat, list[Publication]]] = {}
            for pub, chat in rows:
                by_chat.setdefault(chat.id, (chat, []))[1].append(pub)
            for chat, pubs in by_chat.values():
                try:
                    res.removals += await self._check_chat(chat, pubs)
                    res.checked += len(pubs)
                except errors.FloodWaitError as e:
                    log.warning("FloodWait %ss during post check, stopping this round", e.seconds)
                    res.errors.append(f"Telegram попросил подождать {e.seconds} с — проверка прервана")
                    break
                except Exception as e:  # one broken chat must not stop the others
                    log.exception("Post check in chat %s failed", chat.id)
                    res.errors.append(f"{_title(chat)}: {type(e).__name__}")
            for r in res.removals:
                await self.notifier.post_removed(r)
            return res

    async def _check_chat(self, chat: Chat, pubs: list[Publication]) -> list[Removal]:
        ids = sorted({i for p in pubs for i in p.tg_message_ids})
        if not ids:
            return []
        try:
            peer = await resolve_chat(self.client, chat.tg_chat_id, chat.username)
            found = await self.client.get_messages(peer, ids=ids)
        except (errors.ChannelPrivateError, errors.UserBannedInChannelError, errors.ChatForbiddenError) as e:
            await self._lost_chat(chat, type(e).__name__)
            return []
        alive = {m.id for m in found if m is not None and not isinstance(m, types.MessageEmpty)}
        gone = [p for p in pubs if p.tg_message_ids and not alive.intersection(p.tg_message_ids)]
        if not gone:
            return []

        problem = await self._chat_problem(chat)
        out = []
        for pub in gone:
            clues = await self._clues(peer, pub)
            removal = await self._mark_removed(pub.id, clues, problem)
            if removal:
                out.append(removal)
        return out

    async def _mark_removed(self, pub_id: int, clues: list[str], problem: str | None) -> Removal | None:
        now = datetime.now(UTC)
        async with session_scope() as s:
            pub = await s.get(Publication, pub_id)
            if pub.status != PublicationStatus.POSTED:  # sold/withdrawn meanwhile: we deleted it ourselves
                return None
            pub.status, pub.deleted_at = PublicationStatus.REMOVED_BY_CHAT, now
            pub.error = ("; ".join(clues) or "удалён в чате, причина не найдена")[:1000]
            item = await s.get(Item, pub.item_id)
            chat = await s.get(Chat, pub.chat_id)
            await s.flush()
            removed = await s.scalar(select(func.count()).select_from(Publication).where(
                Publication.chat_id == chat.id, Publication.status == PublicationStatus.REMOVED_BY_CHAT,
                Publication.deleted_at >= now - timedelta(days=30)))
            return Removal(
                publication_id=pub.id, item_id=item.id, item_name=item.display_name, chat_id=chat.id,
                chat_title=_title(chat), posted_at=pub.posted_at, clues=clues, chat_problem=problem,
                removed_in_chat=removed or 1,
                can_repost=item.status in SELLABLE and item.quantity_sold < item.quantity_total and chat.can_post,
            )

    async def _chat_problem(self, chat: Chat) -> str | None:
        """Re-reads the chat's permissions: maybe we got muted or the photo permission changed."""
        try:
            snap = await inspect_chat(self.client, chat.tg_chat_id if chat.tg_chat_id else f"@{chat.username}")
        except (ChatCheckError, ValueError, errors.RPCError) as e:
            log.warning("Re-inspecting chat %s after a removal failed: %s", chat.id, e)
            return None
        async with session_scope() as s:
            c = await s.get(Chat, chat.id)
            chat_service.apply_snapshot(c, snap)
        return "; ".join(snap.problems) or None

    async def _clues(self, peer, pub: Publication) -> list[str]:
        """Messages posted after ours that reply to it or mention the selling account (moderator / anti-spam bot)."""
        if self._me is None:
            self._me = await self.client.get_me()
        me = self._me
        ours = set(pub.tg_message_ids)
        handle = f"@{me.username}".lower() if me.username else None
        clues: list[str] = []
        async for m in self.client.iter_messages(peer, min_id=min(ours), limit=CLUE_SCAN_LIMIT, reverse=True):
            if m.id in ours or m.sender_id == me.id:
                continue
            reply = getattr(m.reply_to, "reply_to_msg_id", None) if m.reply_to else None
            text = (m.message or "").strip()
            mentions_me = bool(handle and handle in text.lower()) or any(
                isinstance(e, types.MessageEntityMentionName) and e.user_id == me.id for e in (m.entities or []))
            if reply in ours or mentions_me:
                who = await _sender_name(m)
                clues.append(f"{who}: {text[:250]}" if text else f"{who}: (без текста)")
                if len(clues) >= CLUE_MAX:
                    break
        return clues

    async def _lost_chat(self, chat: Chat, reason: str) -> None:
        async with session_scope() as s:
            c = await s.get(Chat, chat.id)
            if not c.can_post and not c.is_active:
                return  # already reported
            c.can_post, c.is_active = False, False
            c.check_note = f"аккаунт для продаж больше не может читать чат ({reason}) — возможно, его исключили"
        await self.notifier.publisher_alert(
            f"🚫 Аккаунт для продаж потерял доступ к «{_title(chat)}» ({reason}) — похоже, его исключили или забанили. "
            "Чат выключен, посты там проверить не могу.")


async def repost(pub_id: int) -> tuple[bool, str]:
    """Queues the same version again (new cycle); the publisher posts it within the chat's limits."""
    async with session_scope() as s:
        old = await s.get(Publication, pub_id)
        item = await s.get(Item, old.item_id)
        chat = await s.get(Chat, old.chat_id)
        if item.status not in SELLABLE or item.quantity_sold >= item.quantity_total:
            return False, "вещь уже не продаётся"
        if not chat.can_post:
            return False, f"в этот чат сейчас нельзя писать: {chat.check_note or 'ограничение Telegram'}"
        pending = await s.scalar(select(Publication.id).where(
            Publication.item_id == item.id, Publication.chat_id == chat.id,
            Publication.status.in_([PublicationStatus.APPROVED, PublicationStatus.QUEUED, PublicationStatus.POSTED])))
        if pending:
            return False, "эта вещь уже стоит в очереди или опубликована в этом чате"
        cycle = await s.scalar(select(func.max(Publication.cycle_no)).where(
            Publication.item_id == item.id, Publication.chat_id == chat.id))
        s.add(Publication(item_id=item.id, chat_id=chat.id, text=old.text, hashtags=list(old.hashtags or []),
                          language=old.language, status=PublicationStatus.APPROVED, cycle_no=(cycle or 0) + 1))
        if not chat.is_active:
            return True, f"поставил в очередь, но чат выключен — включи его: /chat {chat.id}"
    return True, "поставил в очередь — уйдёт в рамках лимитов чата"


SHOWN = [PublicationStatus.POSTED, PublicationStatus.APPROVED, PublicationStatus.QUEUED, PublicationStatus.DRAFT,
         PublicationStatus.REMOVED_BY_CHAT, PublicationStatus.FAILED]


async def overview(item_id: int | None = None) -> list[tuple[Item, list[tuple[Publication, Chat]]]]:
    """Where each item is posted / queued / removed. Without item_id: items still for sale."""
    async with session_scope() as s:
        q = (select(Publication, Chat, Item).join(Chat, Chat.id == Publication.chat_id)
             .join(Item, Item.id == Publication.item_id).where(Publication.status.in_(SHOWN))
             .order_by(Item.id, Chat.id, Publication.cycle_no))
        q = q.where(Item.id == item_id) if item_id else q.where(Item.status.in_(SELLABLE))
        rows = (await s.execute(q)).all()
    out: dict[int, tuple[Item, list]] = {}
    for pub, chat, item in rows:
        out.setdefault(item.id, (item, []))[1].append((pub, chat))
    # A chat where the item was removed and then re-posted: show only the latest state.
    result = []
    for item, pubs in out.values():
        latest: dict[int, tuple[Publication, Chat]] = {}
        for pub, chat in pubs:
            latest[chat.id] = (pub, chat)
        result.append((item, list(latest.values())))
    return result


async def _sender_name(m) -> str:
    try:
        sender = await m.get_sender()
    except Exception:  # noqa: BLE001 - only used for a label
        sender = None
    if sender is None:
        return "аноним/админ"
    name = getattr(sender, "username", None)
    if name:
        return f"@{name}" + (" (бот)" if getattr(sender, "bot", False) else "")
    return getattr(sender, "first_name", None) or getattr(sender, "title", None) or str(m.sender_id)


def _title(chat: Chat) -> str:
    return chat.title or (f"@{chat.username}" if chat.username else f"#{chat.id}")
