"""Bot-panel core: Telethon client, approval queue display, pipeline notifier."""

import asyncio
import logging
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from telethon import TelegramClient
from telethon.errors import MessageNotModifiedError

from app.bot import keyboards, texts
from app.config import Settings
from app.db.enums import ApprovalKind, ApprovalStatus, BatchStatus, ItemStatus
from app.db.models import Approval, IntakeBatch, Item
from app.db.session import session_scope
from app.services import pipeline, pricing, recognition

log = logging.getLogger(__name__)


@dataclass
class PendingInput:
    kind: str  # price | fix | edit | qty
    item_id: int


class BotCore:
    def __init__(self, settings: Settings):
        self.settings = settings
        settings.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.client = TelegramClient(str(settings.sessions_dir / "bot"), settings.tg_api_id, settings.tg_api_hash)
        self.admin = settings.admin_tg_id
        self.pending: PendingInput | None = None
        self.busy: set[int] = set()        # item ids being re-processed
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

    async def _card_text(self, item_id: int) -> tuple[Item, str]:
        async with session_scope() as s:
            item = await recognition.load_item(s, item_id)
            research = await pricing.latest_research(s, item_id)
            text = texts.review_card(item, research, self.settings.seller_username, busy=item_id in self.busy)
        return item, text

    async def _pending_approval(self, item_id: int) -> Approval | None:
        async with session_scope() as s:
            return await s.scalar(select(Approval).where(
                Approval.item_id == item_id,
                Approval.kind == ApprovalKind.ITEM_REVIEW,
                Approval.status == ApprovalStatus.PENDING,
            ))

    # ---------- approval queue ----------

    async def show_card(self, item_id: int, with_photos: bool = True) -> None:
        """Sends (photos +) review card for an item at the bottom of the chat and remembers its message id."""
        item, text = await self._card_text(item_id)
        photos = [str(self.settings.data_dir / p.path) for p in item.photos[:10]]
        if with_photos and photos:
            await self.client.send_file(self.admin, photos)
        buttons = None if item_id in self.busy else keyboards.review(item_id)
        msg = await self.send(text, buttons=buttons)
        async with session_scope() as s:
            approval = await s.scalar(select(Approval).where(
                Approval.item_id == item_id,
                Approval.kind == ApprovalKind.ITEM_REVIEW,
                Approval.status.in_([ApprovalStatus.PENDING, ApprovalStatus.SKIPPED]),
            ))
            if approval:
                if approval.bot_message_id and approval.bot_message_id != msg.id:
                    old_id = approval.bot_message_id
                    self.spawn(self.edit(old_id, f"<b>#{item_id}</b> — карточка ниже ⬇️"))
                approval.status = ApprovalStatus.PENDING
                approval.bot_message_id = msg.id

    async def refresh_card(self, item_id: int) -> None:
        approval = await self._pending_approval(item_id)
        if not approval or not approval.bot_message_id:
            return
        _, text = await self._card_text(item_id)
        buttons = None if item_id in self.busy else keyboards.review(item_id)
        await self.edit(approval.bot_message_id, text, buttons=buttons)

    async def show_next(self, force: bool = False) -> None:
        """Shows one card at a time. `force` (from /next) re-sends the current card or reports an empty queue."""
        async with self._queue_lock:
            async with session_scope() as s:
                base = select(Approval).join(Item, Item.id == Approval.item_id).where(
                    Approval.kind == ApprovalKind.ITEM_REVIEW, Item.status == ItemStatus.PENDING_APPROVAL
                )
                displayed = await s.scalar(base.where(
                    Approval.status == ApprovalStatus.PENDING, Approval.bot_message_id.is_not(None)
                ).order_by(Approval.id).limit(1))
                if displayed and not force:
                    return
                target = displayed or await s.scalar(base.where(
                    Approval.status == ApprovalStatus.PENDING, Approval.bot_message_id.is_(None)
                ).order_by(Approval.id).limit(1)) or await s.scalar(base.where(
                    Approval.status == ApprovalStatus.SKIPPED
                ).order_by(Approval.updated_at).limit(1))
                target_item = target.item_id if target else None

            if target_item is None:
                if force:
                    await self.send(texts.QUEUE_EMPTY)
                return
            await self.show_card(target_item)

    async def queue_size(self) -> int:
        async with session_scope() as s:
            rows = await s.scalars(select(Approval.id).join(Item, Item.id == Approval.item_id).where(
                Approval.kind == ApprovalKind.ITEM_REVIEW,
                Approval.status.in_([ApprovalStatus.PENDING, ApprovalStatus.SKIPPED]),
                Item.status == ItemStatus.PENDING_APPROVAL,
            ))
            return len(rows.all())

    # ---------- re-processing an item from its card ----------

    def run_on_item(self, item_id: int, coro_factory, move_down: bool = False) -> bool:
        """Marks the card busy, runs the AI step in background, then re-renders the card.

        move_down: re-send the card at the bottom of the chat (used after the owner typed something).
        """
        if item_id in self.busy:
            return False
        self.busy.add(item_id)

        async def runner() -> None:
            try:
                await self.refresh_card(item_id)
                await coro_factory()
            finally:
                self.busy.discard(item_id)
                if move_down:
                    await self.show_card(item_id, with_photos=False)
                else:
                    await self.refresh_card(item_id)

        self.spawn(runner())
        return True

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
