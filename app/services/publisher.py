"""Publishing scheduler: posts approved per-chat versions one at a time, within anti-ban limits.

All state lives in the DB (publications, chats, settings), so restarts are safe. One post per tick at most.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload
from telethon import TelegramClient, errors

from app.config import Settings
from app.db.enums import Actor, ItemStatus, PublicationStatus
from app.db.models import Chat, Item, Publication
from app.db.session import session_scope
from app.services import settings_store
from app.services.items import set_status
from app.services.listing import render_publication
from app.services.scheduling import (
    Candidate,
    ChatState,
    GlobalState,
    chat_limits,
    chat_ready,
    global_block_reason,
    next_gap,
    pick,
)
from app.services.versions import SELLABLE
from app.userbot.sender import message_link, send_post

log = logging.getLogger(__name__)

K_ENABLED = "publisher_enabled"
K_PAUSED = "publisher_paused_until"
K_NEXT = "publisher_next_at"
K_LAST_ITEM = "publisher_last_item"
MAX_ATTEMPTS = 3

CHAT_FATAL = (
    errors.ChatWriteForbiddenError, errors.UserBannedInChannelError, errors.ChatRestrictedError,
    errors.ChannelPrivateError, errors.ChatAdminRequiredError, errors.ChatGuestSendForbiddenError,
)
PHOTOS_FORBIDDEN = (errors.ChatSendMediaForbiddenError, errors.ChatSendPhotosForbiddenError)


class PublisherNotifier(Protocol):
    async def publication_posted(self, item_id: int, item_name: str, chat_title: str, link: str | None) -> None: ...
    async def publisher_alert(self, text: str) -> None: ...


@dataclass
class Status:
    enabled: bool
    block_reason: str | None
    approved: int
    drafts: int
    posted_24h: int
    ready_chats: int
    active_chats: int
    next_title: str | None


def chat_block_key(chat_id: int) -> str:
    return f"chat_block:{chat_id}"


class Publisher:
    def __init__(self, settings: Settings, client: TelegramClient, notifier: PublisherNotifier, sender=send_post):
        self.settings = settings
        self.client = client
        self.notifier = notifier
        self.sender = sender
        self.tz = ZoneInfo(settings.timezone)
        self._lock = asyncio.Lock()

    def now(self) -> datetime:
        return datetime.now(UTC).astimezone(self.tz)

    # ---------- state ----------

    async def _global_state(self, now: datetime) -> GlobalState:
        async with session_scope() as s:
            last_hour = await s.scalar(select(func.count()).select_from(Publication).where(
                Publication.posted_at >= now - timedelta(hours=1)))
            last_day = await s.scalar(select(func.count()).select_from(Publication).where(
                Publication.posted_at >= now - timedelta(hours=24)))
        return GlobalState(
            enabled=bool(await settings_store.get(K_ENABLED, False)),
            paused_until=await settings_store.get_time(K_PAUSED),
            next_at=await settings_store.get_time(K_NEXT),
            posts_last_hour=last_hour or 0,
            posts_last_24h=last_day or 0,
        )

    async def _load(self, now: datetime) -> tuple[list[Candidate], list[ChatState], dict[int, str]]:
        async with session_scope() as s:
            chats = list(await s.scalars(select(Chat).where(Chat.is_active.is_(True), Chat.can_post.is_(True))))
            counts = dict((await s.execute(
                select(Publication.chat_id, func.count()).where(Publication.posted_at >= now - timedelta(hours=24))
                .group_by(Publication.chat_id))).all())
            last = dict((await s.execute(
                select(Publication.chat_id, func.max(Publication.posted_at)).group_by(Publication.chat_id))).all())
            rows = (await s.execute(
                select(Publication, Item).join(Item, Item.id == Publication.item_id)
                .where(
                    Publication.status == PublicationStatus.APPROVED,
                    Publication.chat_id.in_([c.id for c in chats] or [-1]),
                    Item.status.in_(SELLABLE),
                    Item.quantity_sold < Item.quantity_total,
                    Item.price.is_not(None),
                ))).all()
        states = []
        for c in chats:
            last_at = max(filter(None, [last.get(c.id), c.last_post_at]), default=None)
            states.append(ChatState(
                chat_id=c.id,
                limits=chat_limits(c.rules, c.slowmode_seconds, self.settings.chat_default_posts_per_day),
                posts_last_24h=counts.get(c.id, 0),
                last_post_at=last_at,
                blocked_until=await settings_store.get_time(chat_block_key(c.id)),
            ))
        candidates = [
            Candidate(publication_id=p.id, item_id=i.id, chat_id=p.chat_id, price=float(i.price), priority=i.priority,
                      has_inquiries=i.status == ItemStatus.HAS_INQUIRIES)
            for p, i in rows
        ]
        titles = {p.id: f"#{i.id} {i.display_name}" for p, i in rows}
        return candidates, states, titles

    async def status(self) -> Status:
        now = self.now()
        g = await self._global_state(now)
        candidates, states, titles = await self._load(now)
        ready = {cs.chat_id for cs in states if chat_ready(cs, now)}
        nxt = pick(candidates, ready, await settings_store.get(K_LAST_ITEM))
        async with session_scope() as s:
            drafts = await s.scalar(select(func.count()).select_from(Publication).where(
                Publication.status == PublicationStatus.DRAFT))
        return Status(
            enabled=g.enabled,
            block_reason=global_block_reason(g, now, (self.settings.publish_window_start, self.settings.publish_window_end),
                                             self.settings.publish_max_per_hour, self.settings.publish_max_per_day),
            approved=len(candidates), drafts=drafts or 0, posted_24h=g.posts_last_24h,
            ready_chats=len(ready), active_chats=len(states),
            next_title=titles.get(nxt.publication_id) if nxt else None,
        )

    async def set_enabled(self, on: bool) -> None:
        await settings_store.put(K_ENABLED, on)
        if on:
            await settings_store.put_time(K_PAUSED, None)

    # ---------- loop ----------

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Publisher tick failed")
            await asyncio.sleep(self.settings.publish_tick_seconds)

    async def tick(self, force: bool = False) -> str:
        """force=True ('post now' button): ignores the on/off switch, window, gap and global caps,
        but never per-chat limits or a FloodWait/spam pause."""
        async with self._lock:
            now = self.now()
            g = await self._global_state(now)
            if force:
                if g.paused_until and now < g.paused_until:
                    return f"пауза после ограничения Telegram до {g.paused_until.astimezone(self.tz):%d.%m %H:%M}"
            else:
                reason = global_block_reason(
                    g, now, (self.settings.publish_window_start, self.settings.publish_window_end),
                    self.settings.publish_max_per_hour, self.settings.publish_max_per_day)
                if reason:
                    return reason
            candidates, states, _ = await self._load(now)
            ready = {cs.chat_id for cs in states if chat_ready(cs, now)}
            chosen = pick(candidates, ready, await settings_store.get(K_LAST_ITEM))
            if chosen is None:
                return "нет постов, которые можно опубликовать сейчас (лимиты чатов или пустая очередь)"
            return await self._post(chosen.publication_id, now)

    async def _post(self, pub_id: int, now: datetime) -> str:
        async with session_scope() as s:
            pub = await s.get(Publication, pub_id)
            item = await s.get(Item, pub.item_id, options=[selectinload(Item.photos)])
            chat = await s.get(Chat, pub.chat_id)
            text = render_publication(item, pub.text, pub.hashtags or [], pub.language, self.settings.seller_username)
            max_photos = int((chat.rules or {}).get("max_photos") or 10)
            photos = [self.settings.data_dir / p.path for p in item.photos][:max(1, max_photos)]
            target = (chat.tg_chat_id, chat.username, chat.can_send_photos)
            item_name, chat_title = item.display_name, chat.title or chat.username or str(chat.id)

        try:
            ids, text_id = await self.sender(self.client, target[0], target[1], text, photos, target[2])
        except errors.FloodWaitError as e:
            until = now + timedelta(seconds=e.seconds + 60)
            await settings_store.put_time(K_PAUSED, until)
            await self.notifier.publisher_alert(
                f"⏸ Telegram попросил подождать {e.seconds // 60} мин (FloodWait). Публикации на паузе до {until:%H:%M}.")
            return "FloodWait — пауза"
        except errors.PeerFloodError:
            until = now + timedelta(hours=24)
            await settings_store.put_time(K_PAUSED, until)
            await self.notifier.publisher_alert(
                "🛑 Telegram ограничил аккаунт для продаж за спам (PeerFlood). Публикации на паузе 24 часа. "
                "Проверь статус аккаунта в @SpamBot с этого аккаунта.")
            return "PeerFlood — пауза 24 ч"
        except errors.SlowModeWaitError as e:
            await settings_store.put_time(chat_block_key(pub.chat_id), now + timedelta(seconds=e.seconds + 30))
            return f"slow mode в «{chat_title}», повторю позже"
        except PHOTOS_FORBIDDEN:
            async with session_scope() as s:
                (await s.get(Chat, pub.chat_id)).can_send_photos = False
            return f"в «{chat_title}» нельзя фото — следующая попытка будет текстом"
        except CHAT_FATAL as e:
            async with session_scope() as s:
                chat = await s.get(Chat, pub.chat_id)
                chat.is_active, chat.can_post = False, False
                chat.check_note = f"публикация запрещена: {type(e).__name__}"
                p = await s.get(Publication, pub_id)
                p.status, p.error = PublicationStatus.FAILED, type(e).__name__
            await self.notifier.publisher_alert(
                f"🚫 Не могу публиковать в «{chat_title}» ({type(e).__name__}). Чат выключен, проверь его через /chats.")
            return "чат выключен"
        except Exception as e:  # network errors, unexpected RPC errors
            log.exception("Posting publication %s failed", pub_id)
            async with session_scope() as s:
                p = await s.get(Publication, pub_id)
                p.attempts += 1
                p.error = f"{type(e).__name__}: {e}"[:500]
                failed = p.attempts >= MAX_ATTEMPTS
                if failed:
                    p.status = PublicationStatus.FAILED
            await settings_store.put_time(K_NEXT, now + timedelta(minutes=5))
            if failed:
                await self.notifier.publisher_alert(
                    f"⚠️ Пост «{item_name}» в «{chat_title}» не отправился {MAX_ATTEMPTS} раза: {type(e).__name__}. Пропускаю его.")
            return f"ошибка: {type(e).__name__}"

        async with session_scope() as s:
            p = await s.get(Publication, pub_id)
            p.status, p.posted_at, p.tg_message_ids, p.error = PublicationStatus.POSTED, now, ids, None
            p.text_message_id = text_id
            p.attempts += 1
            chat = await s.get(Chat, p.chat_id)
            chat.last_post_at = now
            item = await s.get(Item, p.item_id)
            if item.first_published_at is None:
                item.first_published_at = now
            if item.status == ItemStatus.READY:
                await set_status(s, item, ItemStatus.PUBLISHED, Actor.SYSTEM, f"first post in {chat_title}")
            link = message_link(chat.tg_chat_id, chat.username, ids[0])
            item_id = item.id
        await settings_store.put_time(K_NEXT, now + next_gap(self.settings.publish_gap_min_minutes,
                                                             self.settings.publish_gap_max_minutes))
        await settings_store.put(K_LAST_ITEM, item_id)
        await self.notifier.publication_posted(item_id, item_name, chat_title, link)
        return f"опубликовано: {item_name} → {chat_title}"
