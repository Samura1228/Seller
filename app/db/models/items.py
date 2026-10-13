from datetime import datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, str_enum
from app.db.enums import (
    Actor,
    ApprovalKind,
    ApprovalStatus,
    BatchStatus,
    Condition,
    ItemStatus,
    Language,
    PriceReason,
    TextPlatform,
    TextStatus,
)


class IntakeBatch(TimestampMixin, Base):
    __tablename__ = "intake_batches"

    file_name: Mapped[str | None] = mapped_column(sa.String(255))
    size_bytes: Mapped[int | None] = mapped_column(sa.BigInteger)
    status: Mapped[BatchStatus] = mapped_column(str_enum(BatchStatus), default=BatchStatus.RECEIVING)
    items_found: Mapped[int] = mapped_column(default=0)
    report: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)  # skipped folders, warnings
    error: Mapped[str | None] = mapped_column(sa.Text)

    items: Mapped[list["Item"]] = relationship(back_populates="batch")


class Item(TimestampMixin, Base):
    """Master item card: the single source of truth."""

    __tablename__ = "items"

    batch_id: Mapped[int | None] = mapped_column(sa.ForeignKey("intake_batches.id", ondelete="SET NULL"), index=True)
    folder_hint: Mapped[str | None] = mapped_column(sa.String(255))

    title: Mapped[str | None] = mapped_column(sa.String(255))
    category: Mapped[str | None] = mapped_column(sa.String(100))
    brand: Mapped[str | None] = mapped_column(sa.String(100))
    model: Mapped[str | None] = mapped_column(sa.String(150))
    specs: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    condition: Mapped[Condition | None] = mapped_column(str_enum(Condition))
    condition_notes: Mapped[str | None] = mapped_column(sa.Text)  # defects, honestly
    included: Mapped[list[str]] = mapped_column(JSONB, default=list)  # box, charger, ...
    my_notes: Mapped[str | None] = mapped_column(sa.Text)  # corrections / facts I added

    quantity_total: Mapped[int] = mapped_column(default=1)
    quantity_sold: Mapped[int] = mapped_column(default=0)

    status: Mapped[ItemStatus] = mapped_column(str_enum(ItemStatus), default=ItemStatus.NEW, index=True)
    priority: Mapped[int] = mapped_column(default=0)

    price: Mapped[Decimal | None]
    min_price: Mapped[Decimal | None]  # negotiation floor, never shown to buyers
    new_price_ref: Mapped[Decimal | None]
    currency: Mapped[str] = mapped_column(sa.String(3), default="EUR")

    uncertain_fields: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    recognition_raw: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    pipeline_error: Mapped[str | None] = mapped_column(sa.Text)

    approved_at: Mapped[datetime | None]
    first_published_at: Mapped[datetime | None]
    sold_at: Mapped[datetime | None]
    withdrawn_at: Mapped[datetime | None]

    batch: Mapped[IntakeBatch | None] = relationship(back_populates="items")
    photos: Mapped[list["ItemPhoto"]] = relationship(
        back_populates="item", order_by="ItemPhoto.sort_order", cascade="all, delete-orphan"
    )
    texts: Mapped[list["ItemText"]] = relationship(back_populates="item", cascade="all, delete-orphan")

    @property
    def quantity_left(self) -> int:
        return max(self.quantity_total - self.quantity_sold, 0)

    @property
    def display_name(self) -> str:
        return self.title or self.folder_hint or f"Item #{self.id}"


class ItemPhoto(TimestampMixin, Base):
    __tablename__ = "item_photos"

    item_id: Mapped[int] = mapped_column(sa.ForeignKey("items.id", ondelete="CASCADE"), index=True)
    path: Mapped[str] = mapped_column(sa.String(500))  # relative to DATA_DIR
    thumb_path: Mapped[str] = mapped_column(sa.String(500))
    width: Mapped[int]
    height: Mapped[int]
    sort_order: Mapped[int] = mapped_column(default=0)
    is_cover: Mapped[bool] = mapped_column(default=False)
    sha256: Mapped[str] = mapped_column(sa.String(64), index=True)  # of the ORIGINAL file, for dedupe
    original_name: Mapped[str | None] = mapped_column(sa.String(255))
    tg_media_cache: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # stage 2: reuse uploaded media

    item: Mapped[Item] = relationship(back_populates="photos")


class ItemText(TimestampMixin, Base):
    __tablename__ = "item_texts"
    __table_args__ = (sa.UniqueConstraint("item_id", "platform", "language", name="uq_item_texts_item_platform_lang"),)

    item_id: Mapped[int] = mapped_column(sa.ForeignKey("items.id", ondelete="CASCADE"), index=True)
    platform: Mapped[TextPlatform] = mapped_column(str_enum(TextPlatform))
    language: Mapped[Language] = mapped_column(str_enum(Language))
    title: Mapped[str] = mapped_column(sa.String(255))
    body: Mapped[str] = mapped_column(sa.Text)
    status: Mapped[TextStatus] = mapped_column(str_enum(TextStatus), default=TextStatus.DRAFT)
    version: Mapped[int] = mapped_column(default=1)

    item: Mapped[Item] = relationship(back_populates="texts")


class PriceResearch(TimestampMixin, Base):
    __tablename__ = "price_research"

    item_id: Mapped[int] = mapped_column(sa.ForeignKey("items.id", ondelete="CASCADE"), index=True)
    new_price: Mapped[Decimal | None]
    new_price_source_url: Mapped[str | None] = mapped_column(sa.Text)
    comps: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    proposed_price: Mapped[Decimal | None]
    proposed_min: Mapped[Decimal | None]
    rationale: Mapped[str | None] = mapped_column(sa.Text)


class PriceHistory(TimestampMixin, Base):
    __tablename__ = "price_history"

    item_id: Mapped[int] = mapped_column(sa.ForeignKey("items.id", ondelete="CASCADE"), index=True)
    price: Mapped[Decimal]
    min_price: Mapped[Decimal | None]
    reason: Mapped[PriceReason] = mapped_column(str_enum(PriceReason))
    actor: Mapped[Actor] = mapped_column(str_enum(Actor))


class Approval(TimestampMixin, Base):
    """Approval queue shared by the bot and (later) the Mini App."""

    __tablename__ = "approvals"

    item_id: Mapped[int | None] = mapped_column(sa.ForeignKey("items.id", ondelete="CASCADE"), index=True)
    chat_id: Mapped[int | None] = mapped_column(sa.ForeignKey("chats.id", ondelete="CASCADE"), index=True)
    kind: Mapped[ApprovalKind] = mapped_column(str_enum(ApprovalKind))
    status: Mapped[ApprovalStatus] = mapped_column(
        str_enum(ApprovalStatus), default=ApprovalStatus.PENDING, index=True
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    bot_message_id: Mapped[int | None] = mapped_column(sa.BigInteger)
    resolved_at: Mapped[datetime | None]


class ItemEvent(TimestampMixin, Base):
    __tablename__ = "item_events"

    item_id: Mapped[int] = mapped_column(sa.ForeignKey("items.id", ondelete="CASCADE"), index=True)
    from_status: Mapped[ItemStatus | None] = mapped_column(str_enum(ItemStatus))
    to_status: Mapped[ItemStatus] = mapped_column(str_enum(ItemStatus))
    actor: Mapped[Actor] = mapped_column(str_enum(Actor))
    note: Mapped[str | None] = mapped_column(sa.Text)


class AiUsage(TimestampMixin, Base):
    __tablename__ = "ai_usage"

    purpose: Mapped[str] = mapped_column(sa.String(50))
    model: Mapped[str] = mapped_column(sa.String(64))
    item_id: Mapped[int | None] = mapped_column(sa.ForeignKey("items.id", ondelete="SET NULL"), index=True)
    input_tokens: Mapped[int] = mapped_column(default=0)
    output_tokens: Mapped[int] = mapped_column(default=0)
    web_searches: Mapped[int] = mapped_column(default=0)
    cost_usd: Mapped[Decimal] = mapped_column(sa.Numeric(10, 4), default=0)
