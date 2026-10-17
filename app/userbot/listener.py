"""Listens to the selling account in real time: private messages and replies to our posts in chats."""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Protocol

from telethon import TelegramClient, events, types

from app.services import inquiries
from app.services.inquiries import BuyerInfo, Incoming, Outcome

log = logging.getLogger(__name__)

IGNORED_USERS = {777000, 42777}  # Telegram service notifications (login codes etc.)


class InquiryNotifier(Protocol):
    async def inquiry_new(self, inquiry_id: int, is_new: bool) -> None: ...


@dataclass
class _Pending:
    buyer: BuyerInfo
    messages: list[Incoming] = field(default_factory=list)
    task: asyncio.Task | None = None
    started: float = 0.0


def message_text(msg) -> str:
    text = (msg.message or "").strip()
    if msg.media and not text:
        return "[фото]" if isinstance(msg.media, types.MessageMediaPhoto) else "[вложение]"
    if msg.fwd_from and text:
        return f"[переслано] {text}"
    return text


def buyer_info(user: types.User) -> BuyerInfo:
    name = " ".join(filter(None, [user.first_name, user.last_name])) or None
    return BuyerInfo(tg_user_id=user.id, username=user.username, display_name=name)


def is_person(user) -> bool:
    return isinstance(user, types.User) and not user.bot and not user.is_self and user.id not in IGNORED_USERS


class InquiryListener:
    """DMs are debounced: people often send «Здравствуйте» and the actual question a few seconds later."""

    def __init__(self, client: TelegramClient, notifier: InquiryNotifier, debounce: float = 20.0, max_wait: float = 60.0):
        self.client = client
        self.notifier = notifier
        self.debounce = debounce
        self.max_wait = max_wait
        self._pending: dict[int, _Pending] = {}

    def register(self) -> None:
        self.client.add_event_handler(self.on_message, events.NewMessage(incoming=True))

    async def on_message(self, event: events.NewMessage.Event) -> None:
        try:
            if event.is_private:
                await self._on_private(event)
            elif event.is_group and event.message.reply_to and event.message.reply_to.reply_to_msg_id:
                await self._on_group_reply(event)
        except Exception:
            log.exception("Failed to handle incoming message")

    async def _on_private(self, event) -> None:
        sender = await event.get_sender()
        if not is_person(sender):
            return
        text = message_text(event.message)
        if not text:
            return
        self.add_private(buyer_info(sender), Incoming(event.message.id, text))

    def add_private(self, buyer: BuyerInfo, msg: Incoming) -> None:
        loop = asyncio.get_running_loop()
        p = self._pending.get(buyer.tg_user_id)
        if p is None:
            p = self._pending[buyer.tg_user_id] = _Pending(buyer=buyer, started=loop.time())
        p.messages.append(msg)
        if p.task:
            p.task.cancel()
        delay = max(0.0, min(self.debounce, p.started + self.max_wait - loop.time()))
        p.task = asyncio.create_task(self._flush_later(buyer.tg_user_id, delay))

    async def _flush_later(self, user_id: int, delay: float) -> None:
        await asyncio.sleep(delay)
        p = self._pending.pop(user_id, None)
        if p is None:
            return
        try:
            outcome = await inquiries.handle_dm(p.buyer, p.messages)
        except Exception:
            log.exception("Inquiry processing failed for user %s", user_id)
            return
        await self._notify(outcome)

    async def _on_group_reply(self, event) -> None:
        pub_id = await inquiries.find_publication(event.chat_id, event.message.reply_to.reply_to_msg_id)
        if pub_id is None:
            return
        sender = await event.get_sender()
        if not is_person(sender):
            return
        outcome = await inquiries.handle_reply(
            buyer_info(sender), event.chat_id, Incoming(event.message.id, message_text(event.message)), pub_id)
        await self._notify(outcome)

    async def _notify(self, outcome: Outcome | None) -> None:
        if outcome is not None:
            await self.notifier.inquiry_new(outcome.inquiry_id, outcome.is_new)

    async def flush_all(self) -> None:
        """Process everything buffered right now (tests, shutdown)."""
        for user_id in list(self._pending):
            p = self._pending.get(user_id)
            if p and p.task:
                p.task.cancel()
            await self._flush_later(user_id, 0)

    async def scan_unread(self, limit_dialogs: int = 200) -> int:
        """After a restart: unread private chats may contain messages that arrived while we were down."""
        found = 0
        async for dialog in self.client.iter_dialogs(limit=limit_dialogs):
            if not dialog.is_user or dialog.unread_count <= 0 or not is_person(dialog.entity):
                continue
            msgs = await self.client.get_messages(dialog.entity, limit=min(dialog.unread_count, 5))
            batch = [Incoming(m.id, message_text(m)) for m in reversed(msgs) if not m.out and message_text(m)]
            if not batch:
                continue
            outcome = await inquiries.handle_dm(buyer_info(dialog.entity), batch)
            await self._notify(outcome)
            found += 1
        return found
