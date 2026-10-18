"""Bot-panel core: Telethon client, one-at-a-time approval queue, notifiers for the pipeline and the publisher."""

import asyncio
import logging
from collections.abc import Coroutine, Hashable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import selectinload
from telethon import TelegramClient
from telethon.errors import MessageNotModifiedError

from app.bot import keyboards, texts
from app.config import Settings
from app.db.enums import ApprovalKind, ApprovalStatus, BatchStatus, ItemStatus
from app.db.models import Approval, Buyer, Chat, Inquiry, IntakeBatch, Item, Publication
from app.db.session import session_scope
from app.services import inquiries, pipeline, pricing, recognition, versions

log = logging.getLogger(__name__)

QUEUE_KINDS = (ApprovalKind.ITEM_REVIEW, ApprovalKind.CHAT_RULES, ApprovalKind.CHAT_PREVIEWS)
KIND_ORDER = case({k: i for i, k in enumerate(QUEUE_KINDS)}, value=Approval.kind)
OPEN = (ApprovalStatus.PENDING, ApprovalStatus.SKIPPED)


@dataclass
class PendingInput:
    kind: str  # price | fix | edit | qty | chat_rules | sold_price
    item_id: int  # item id, or chat id for chat_rules
    extra: dict = field(default_factory=dict)


@dataclass
class Card:
    text: str
    buttons: Any
    photos: list[str] = field(default_factory=list)
    before: list[str] = field(default_factory=list)  # extra messages sent above the card (long previews)


class BotCore:
    def __init__(self, settings: Settings):
        self.settings = settings
        settings.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.client = TelegramClient(str(settings.sessions_dir / "bot"), settings.tg_api_id, settings.tg_api_hash)
        self.admin = settings.admin_tg_id
        self.pending: PendingInput | None = None
        # Busy keys: int item id (review card), ("chat", id), ("pv", item id)
        self.busy: set[Hashable] = set()
        self.userbot: TelegramClient | None = None
        self.publisher = None  # app.services.publisher.Publisher, set in main when the userbot is configured
        self.post_watcher = None  # app.services.post_watch.PostWatcher, same
        self.scan: dict | None = None  # /scanchats selection state
        self._queue_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()

    # ---------- helpers ----------

    def spawn(self, coro: Coroutine[Any, Any, Any]) -> None:
        """Fire-and-forget with a strong reference and error logging."""
        task = asyncio.create_task(coro)
        self._tasks.add(task)

        def _done(t: asyncio.Task) -> None:
            self._tasks.discard(t)
            if not t.cancelled() and t.exception():
                log.error("Background task failed", exc_info=t.exception())

        task.add_done_callback(_done)

    async def send(self, text: str, **kw) -> Any:
        return await self.client.send_message(self.admin, text, parse_mode="html", link_preview=False, **kw)

    async def edit(self, msg_id: int, text: str, **kw) -> None:
        try:
            await self.client.edit_message(self.admin, msg_id, text, parse_mode="html", link_preview=False, **kw)
        except MessageNotModifiedError:
            pass

    # ---------- rendering ----------

    @staticmethod
    def busy_key(approval: Approval) -> Hashable:
        if approval.kind == ApprovalKind.ITEM_REVIEW:
            return approval.item_id
        if approval.kind == ApprovalKind.CHAT_RULES:
            return ("chat", approval.chat_id)
        return ("pv", approval.item_id)

    async def render(self, approval: Approval) -> Card | None:
        busy = self.busy_key(approval) in self.busy
        seller = self.settings.seller_username
        async with session_scope() as s:
            if approval.kind == ApprovalKind.ITEM_REVIEW:
                item = await recognition.load_item(s, approval.item_id)
                research = await pricing.latest_research(s, item.id)
                return Card(
                    text=texts.review_card(item, research, seller, busy=busy),
                    buttons=None if busy else keyboards.review(item.id),
                    photos=[str(self.settings.data_dir / p.path) for p in item.photos[:10]],
                )
            if approval.kind == ApprovalKind.CHAT_RULES:
                chat = await s.get(Chat, approval.chat_id)
                if chat is None:
                    return None
                return Card(text=texts.chat_card(chat, busy=busy), buttons=None if busy else keyboards.chat(chat))
            item = await s.get(Item, approval.item_id, options=[selectinload(Item.photos)])
            rows = await versions.drafts(s, approval.item_id)
            if item is None or not rows:
                return None
            before, text = texts.previews_card(item, rows, seller, busy=busy)
            return Card(text=text, buttons=None if busy else keyboards.previews(item.id), before=before)

    # ---------- approval queue ----------

    async def show_approval(self, approval_id: int, with_photos: bool = True) -> None:
        """Sends a card at the bottom of the chat and remembers its message id."""
        async with session_scope() as s:
            approval = await s.get(Approval, approval_id)
        card = await self.render(approval) if approval else None
        if card is None:
            async with session_scope() as s:  # nothing to show any more (e.g. drafts gone): close it
                a = await s.get(Approval, approval_id)
                if a:
                    a.status = ApprovalStatus.REJECTED
            return
        if with_photos and card.photos:
            await self.client.send_file(self.admin, card.photos)
        for extra in card.before:
            await self.send(extra)
        msg = await self.send(card.text, buttons=card.buttons)
        async with session_scope() as s:
            a = await s.get(Approval, approval_id)
            if a.bot_message_id and a.bot_message_id != msg.id:
                self.spawn(self.edit(a.bot_message_id, "Карточка обновлена ниже ⬇️"))
            a.status = ApprovalStatus.PENDING
            a.bot_message_id = msg.id

    async def refresh_approval(self, approval_id: int) -> None:
        async with session_scope() as s:
            approval = await s.get(Approval, approval_id)
        if not approval or approval.status != ApprovalStatus.PENDING or not approval.bot_message_id:
            return
        card = await self.render(approval)
        if card is None:
            return
        await self.edit(approval.bot_message_id, card.text, buttons=card.buttons)

    async def _approval_id(self, kind: ApprovalKind, item_id: int | None = None, chat_id: int | None = None) -> int | None:
        async with session_scope() as s:
            q = select(Approval.id).where(Approval.kind == kind, Approval.status.in_(OPEN))
            q = q.where(Approval.chat_id == chat_id) if chat_id is not None else q.where(Approval.item_id == item_id)
            return await s.scalar(q.order_by(Approval.id.desc()).limit(1))

    # item-review wrappers (used by review handlers)
    async def show_card(self, item_id: int, with_photos: bool = True) -> None:
        if aid := await self._approval_id(ApprovalKind.ITEM_REVIEW, item_id=item_id):
            await self.show_approval(aid, with_photos)

    async def refresh_card(self, item_id: int) -> None:
        if aid := await self._approval_id(ApprovalKind.ITEM_REVIEW, item_id=item_id):
            await self.refresh_approval(aid)

    def _queue_query(self):
        return (
            select(Approval).outerjoin(Item, Item.id == Approval.item_id)
            .where(Approval.kind.in_(QUEUE_KINDS),
                   or_(Approval.kind != ApprovalKind.ITEM_REVIEW, Item.status == ItemStatus.PENDING_APPROVAL))
        )

    async def show_next(self, force: bool = False) -> None:
        """One card at a time. `force` (/next) re-sends the current card or reports an empty queue."""
        async with self._queue_lock:
            async with session_scope() as s:
                base = self._queue_query()
                displayed = await s.scalar(base.where(
                    Approval.status == ApprovalStatus.PENDING, Approval.bot_message_id.is_not(None)
                ).order_by(KIND_ORDER, Approval.id).limit(1))
                if displayed and not force:
                    return
                target = displayed or await s.scalar(base.where(
                    Approval.status == ApprovalStatus.PENDING, Approval.bot_message_id.is_(None)
                ).order_by(KIND_ORDER, Approval.id).limit(1)) or await s.scalar(base.where(
                    Approval.status == ApprovalStatus.SKIPPED
                ).order_by(Approval.updated_at).limit(1))
                target_id = target.id if target else None
            if target_id is None:
                if force:
                    await self.send(texts.QUEUE_EMPTY)
                return
            await self.show_approval(target_id)

    async def skip(self, approval_id: int) -> None:
        async with session_scope() as s:
            a = await s.get(Approval, approval_id)
            if a and a.status == ApprovalStatus.PENDING:
                a.status, a.bot_message_id = ApprovalStatus.SKIPPED, None

    async def queue_size(self) -> int:
        async with session_scope() as s:
            rows = await s.scalars(self._queue_query().with_only_columns(Approval.id).where(Approval.status.in_(OPEN)))
            return len(rows.all())

    # ---------- background work on a card ----------

    def run_on(self, key: Hashable, find_approval, coro_factory, move_down: bool = False) -> bool:
        """Marks the card busy, runs an AI step in background, then re-renders (or re-sends) the card.

        find_approval: async callable returning the approval id whose card shows this work (or None).
        """
        if key in self.busy:
            return False
        self.busy.add(key)

        async def runner() -> None:
            approval_id = await find_approval()
            try:
                if approval_id:
                    await self.refresh_approval(approval_id)
                await coro_factory()
            finally:
                self.busy.discard(key)
                if approval_id:
                    if move_down:
                        await self.show_approval(approval_id, with_photos=False)
                    else:
                        await self.refresh_approval(approval_id)

        self.spawn(runner())
        return True

    def run_on_item(self, item_id: int, coro_factory, move_down: bool = False) -> bool:
        return self.run_on(item_id, lambda: self._approval_id(ApprovalKind.ITEM_REVIEW, item_id=item_id),
                           coro_factory, move_down)

    def make_versions(self, item_id: int | None = None, chat_id: int | None = None) -> None:
        """Generates per-chat versions in background, then shows the next card."""
        async def go() -> None:
            try:
                n = await (versions.generate_for_chat(chat_id) if chat_id else versions.generate_for_item(item_id))
            except Exception as e:  # noqa: BLE001 - reported to the owner
                log.exception("Version generation failed")
                await self.send(f"⚠️ Не удалось подготовить версии постов: <code>{texts.escape(str(e)[:300])}</code>")
                return
            if n:
                await self.show_next()

        self.spawn(go())

    # ---------- pipeline.Notifier ----------

    async def item_ready(self, item_id: int) -> None:
        await self.show_next()

    async def item_updated(self, item_id: int) -> None:
        if item_id not in self.busy:
            await self.refresh_card(item_id)

    async def item_failed(self, item_id: int, error: str) -> None:
        async with session_scope() as s:
            item = await s.get(Item, item_id)
        if item:
            await self.send(texts.failed(item, error), buttons=keyboards.failed(item_id))

    # ---------- listener.InquiryNotifier ----------

    async def render_inquiry(self, inquiry_id: int) -> tuple[str, Any]:
        async with session_scope() as s:
            inq = await s.get(Inquiry, inquiry_id)
            item = await s.get(Item, inq.item_id) if inq.item_id else None
            buyer = await s.get(Buyer, inq.buyer_id) if inq.buyer_id else None
            pub = await s.get(Publication, inq.publication_id) if inq.publication_id else None
            chat = await s.get(Chat, pub.chat_id) if pub else None
            count = await s.scalar(select(func.count()).select_from(Inquiry).where(Inquiry.item_id == inq.item_id)) \
                if inq.item_id else 0
        candidates = [] if item else await inquiries.candidate_items()
        text = texts.inquiry_card(inq, item, buyer, chat, count, self.settings.seller_username)
        return text, keyboards.inquiry(inq, item, buyer, chat, candidates)

    async def inquiry_new(self, inquiry_id: int, is_new: bool) -> None:
        text, buttons = await self.render_inquiry(inquiry_id)
        msg = await self.send(text, buttons=buttons)
        async with session_scope() as s:
            inq = await s.get(Inquiry, inquiry_id)
            old = inq.notify_message_id
            inq.notify_message_id = msg.id
        if old and not is_new:
            self.spawn(self.edit(old, "Обновлено ниже ⬇️"))

    async def refresh_inquiry(self, inquiry_id: int) -> None:
        async with session_scope() as s:
            msg_id = (await s.get(Inquiry, inquiry_id)).notify_message_id
        if msg_id:
            text, buttons = await self.render_inquiry(inquiry_id)
            await self.edit(msg_id, text, buttons=buttons)

    # ---------- publisher.PublisherNotifier ----------

    async def publication_posted(self, item_id: int, item_name: str, chat_title: str, link: str | None) -> None:
        await self.send(texts.posted(item_id, item_name, chat_title, link))

    async def publisher_alert(self, text: str) -> None:
        await self.send(text)

    # ---------- post_watch.WatchNotifier ----------

    async def post_removed(self, removal) -> None:
        await self.send(texts.post_removed(removal), buttons=keyboards.post_removed(removal))


async def recover_interrupted(core: BotCore) -> None:
    """After a restart: finish items whose processing was interrupted (no error recorded)."""
    async with session_scope() as s:
        stuck = list(await s.scalars(select(IntakeBatch).where(
            IntakeBatch.status.in_([BatchStatus.RECEIVING, BatchStatus.UNPACKING])
        )))
        for b in stuck:
            b.status, b.error = BatchStatus.FAILED, "interrupted by restart"
    if stuck:
        await core.send("⚠️ Загрузка архива прервалась перезапуском. Отправь архив ещё раз — "
                        "уже загруженные вещи я пропущу.")
    async with session_scope() as s:
        ids = list(await s.scalars(select(Item.id).where(
            Item.status.in_([ItemStatus.NEW, ItemStatus.RECOGNIZED]), Item.pipeline_error.is_(None)
        ).order_by(Item.id)))
    if ids:
        log.info("Resuming %d interrupted items", len(ids))
        await core.send(f"🔄 Перезапуск: продолжаю обработку {len(ids)} вещей.")
        await pipeline.run_batch(ids, core, core.settings.pipeline_concurrency)
    await core.show_next()
