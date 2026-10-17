"""Item processing pipeline: recognize -> price -> master texts -> approval card.

The pipeline knows nothing about Telegram; it reports through a Notifier.
"""

import asyncio
import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import Actor, ApprovalKind, ApprovalStatus, ItemStatus, PriceReason, TextStatus
from app.db.models import Approval, Item
from app.db.session import session_scope
from app.services import copywriting, pricing, recognition
from app.services.items import set_price, set_status

log = logging.getLogger(__name__)


class Notifier(Protocol):
    async def item_ready(self, item_id: int) -> None: ...
    async def item_updated(self, item_id: int) -> None: ...
    async def item_failed(self, item_id: int, error: str) -> None: ...


def _short_error(e: Exception) -> str:
    return f"{type(e).__name__}: {e}"[:500]


async def _fail(item_id: int, e: Exception, notifier: Notifier) -> None:
    log.exception("Pipeline failed for item %s", item_id)
    async with session_scope() as s:
        item = await s.get(Item, item_id)
        if item:
            item.pipeline_error = _short_error(e)
    await notifier.item_failed(item_id, _short_error(e))


async def ensure_review_approval(s: AsyncSession, item: Item) -> Approval:
    approval = await s.scalar(
        select(Approval).where(
            Approval.item_id == item.id,
            Approval.kind == ApprovalKind.ITEM_REVIEW,
            Approval.status.in_([ApprovalStatus.PENDING, ApprovalStatus.SKIPPED]),
        )
    )
    if approval is None:
        approval = Approval(item_id=item.id, kind=ApprovalKind.ITEM_REVIEW)
        s.add(approval)
        await s.flush()
    return approval


async def process_item(item_id: int, notifier: Notifier) -> bool:
    """Full pipeline for a freshly imported (or failed) item. Each step commits, so a retry resumes."""
    try:
        async with session_scope() as s:
            item = await recognition.load_item(s, item_id)
            if item.status not in (ItemStatus.NEW, ItemStatus.RECOGNIZED, ItemStatus.PENDING_APPROVAL):
                return True  # already approved / sold / withdrawn: nothing to do
            item.pipeline_error = None
            if item.status == ItemStatus.NEW:
                await recognition.recognize(s, item)
                await set_status(s, item, ItemStatus.RECOGNIZED, Actor.AI)

        async with session_scope() as s:
            item = await recognition.load_item(s, item_id)
            if item.price is None:
                await pricing.research_and_propose(s, item)

        async with session_scope() as s:
            item = await recognition.load_item(s, item_id)
            if not item.texts:
                await copywriting.write_master(s, item)
            await ensure_review_approval(s, item)
            if item.status == ItemStatus.RECOGNIZED:
                await set_status(s, item, ItemStatus.PENDING_APPROVAL, Actor.AI)
    except Exception as e:
        await _fail(item_id, e, notifier)
        return False
    await notifier.item_ready(item_id)
    return True


async def run_batch(item_ids: list[int], notifier: Notifier, concurrency: int, on_progress=None) -> tuple[int, int]:
    sem = asyncio.Semaphore(concurrency)
    done = failed = 0

    async def one(item_id: int) -> None:
        nonlocal done, failed
        async with sem:
            ok = await process_item(item_id, notifier)
        done += 1
        failed += 0 if ok else 1
        if on_progress:
            await on_progress(done, failed, len(item_ids))

    await asyncio.gather(*(one(i) for i in item_ids))
    return done, failed


# ---------- owner actions from the approval card ----------

async def correct(item_id: int, correction: str, notifier: Notifier) -> None:
    """Owner corrected facts: re-apply recognition, re-price, rewrite texts."""
    try:
        async with session_scope() as s:
            item = await recognition.load_item(s, item_id)
            await recognition.apply_correction(s, item, correction)
        async with session_scope() as s:
            item = await recognition.load_item(s, item_id)
            await pricing.research_and_propose(s, item)
        async with session_scope() as s:
            item = await recognition.load_item(s, item_id)
            await copywriting.write_master(s, item)
    except Exception as e:
        await _fail(item_id, e, notifier)
        return
    await notifier.item_updated(item_id)


async def reprice(item_id: int, notifier: Notifier) -> None:
    try:
        async with session_scope() as s:
            item = await recognition.load_item(s, item_id)
            await pricing.research_and_propose(s, item)
    except Exception as e:
        await _fail(item_id, e, notifier)
        return
    await notifier.item_updated(item_id)


async def rewrite(item_id: int, instruction: str | None, notifier: Notifier) -> None:
    try:
        async with session_scope() as s:
            item = await recognition.load_item(s, item_id)
            await copywriting.write_master(s, item, instruction)
    except Exception as e:
        await _fail(item_id, e, notifier)
        return
    await notifier.item_updated(item_id)


async def set_manual_price(item_id: int, price: Decimal, min_price: Decimal | None) -> None:
    async with session_scope() as s:
        item = await recognition.load_item(s, item_id)
        if min_price is None or min_price > price:
            # keep the AI's ratio between price and floor
            ratio = (item.min_price / item.price) if item.price and item.min_price else Decimal("0.85")
            min_price = (price * ratio).quantize(Decimal("1"))
        await set_price(s, item, price, min_price, PriceReason.MANUAL, Actor.ME)


async def set_quantity(item_id: int, qty: int) -> None:
    async with session_scope() as s:
        item = await recognition.load_item(s, item_id)
        item.quantity_total = qty
        item.uncertain_fields = [q for q in item.uncertain_fields if q.get("field") != "quantity"]
        # keep the AI card in sync so later corrections start from the confirmed quantity
        item.recognition_raw = {**(item.recognition_raw or {}), "quantity": qty, "quantity_confident": True,
                                "questions": [q for q in (item.recognition_raw or {}).get("questions", [])
                                              if q.get("field") != "quantity"]}


async def approve(item_id: int) -> None:
    async with session_scope() as s:
        item = await recognition.load_item(s, item_id)
        if item.price is None or len(item.texts) < 2:
            raise ValueError("Нет цены или текста")
        for t in item.texts:
            t.status = TextStatus.APPROVED
        item.uncertain_fields = []
        await set_status(s, item, ItemStatus.READY, Actor.ME, "approved price and master texts")
        approval = await ensure_review_approval(s, item)
        approval.status = ApprovalStatus.APPROVED
        approval.resolved_at = datetime.now(UTC)


async def withdraw(item_id: int) -> None:
    async with session_scope() as s:
        item = await recognition.load_item(s, item_id)
        await set_status(s, item, ItemStatus.WITHDRAWN, Actor.ME, "not selling")
        await ensure_review_approval(s, item)
        # close every open card of this item (review, chat previews); queued posts are skipped by status
        for a in await s.scalars(select(Approval).where(
            Approval.item_id == item_id, Approval.status.in_([ApprovalStatus.PENDING, ApprovalStatus.SKIPPED])
        )):
            a.status, a.resolved_at = ApprovalStatus.REJECTED, datetime.now(UTC)
