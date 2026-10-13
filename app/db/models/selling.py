"""Tables for stages 2-6. Created now so relations don't need rework later."""

from datetime import datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, str_enum
from app.db.enums import (
    ChatLanguage,
    ExternalPlatform,
    ExternalStatus,
    InquirySource,
    InquiryStatus,
    Language,
    MatchMethod,
    PublicationStatus,
    ReservationStatus,
    RulesStatus,
)


class Chat(TimestampMixin, Base):
    __tablename__ = "chats"

    tg_chat_id: Mapped[int | None] = mapped_column(sa.BigInteger, unique=True)
    username: Mapped[str | None] = mapped_column(sa.String(100))
    invite_link: Mapped[str | None] = mapped_column(sa.String(255))
    title: Mapped[str | None] = mapped_column(sa.String(255))
    language: Mapped[ChatLanguage | None] = mapped_column(str_enum(ChatLanguage))
    language_override: Mapped[ChatLanguage | None] = mapped_column(str_enum(ChatLanguage))
    rules_raw: Mapped[str | None] = mapped_column(sa.Text)
    # {allowed, max_posts_per_day, min_interval_hours, per_user, requires_price,
    #  hashtags, max_photos, format_notes, allowed_days}
    rules: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    rules_status: Mapped[RulesStatus] = mapped_column(str_enum(RulesStatus), default=RulesStatus.AUTO)
    is_active: Mapped[bool] = mapped_column(default=False)
    priority: Mapped[int] = mapped_column(default=0)
    last_post_at: Mapped[datetime | None]
    notes: Mapped[str | None] = mapped_column(sa.Text)


class Publication(TimestampMixin, Base):
    """One post of one item in one chat. Drafts are the per-chat previews."""

    __tablename__ = "publications"

    item_id: Mapped[int] = mapped_column(sa.ForeignKey("items.id", ondelete="CASCADE"), index=True)
    chat_id: Mapped[int] = mapped_column(sa.ForeignKey("chats.id", ondelete="CASCADE"), index=True)
    text: Mapped[str] = mapped_column(sa.Text)
    language: Mapped[Language] = mapped_column(str_enum(Language))
    status: Mapped[PublicationStatus] = mapped_column(
        str_enum(PublicationStatus), default=PublicationStatus.DRAFT, index=True
    )
    scheduled_at: Mapped[datetime | None] = mapped_column(index=True)
    posted_at: Mapped[datetime | None]
    tg_message_ids: Mapped[list[int]] = mapped_column(ARRAY(sa.BigInteger), default=list)  # album => several
    cycle_no: Mapped[int] = mapped_column(default=1)
    deleted_at: Mapped[datetime | None]
    error: Mapped[str | None] = mapped_column(sa.Text)
    attempts: Mapped[int] = mapped_column(default=0)


class Buyer(TimestampMixin, Base):
    __tablename__ = "buyers"

    tg_user_id: Mapped[int] = mapped_column(sa.BigInteger, unique=True)
    username: Mapped[str | None] = mapped_column(sa.String(100))
    display_name: Mapped[str | None] = mapped_column(sa.String(255))
    first_seen_at: Mapped[datetime] = mapped_column(server_default=sa.func.now())


class Inquiry(TimestampMixin, Base):
    __tablename__ = "inquiries"

    item_id: Mapped[int | None] = mapped_column(sa.ForeignKey("items.id", ondelete="SET NULL"), index=True)
    publication_id: Mapped[int | None] = mapped_column(sa.ForeignKey("publications.id", ondelete="SET NULL"))
    buyer_id: Mapped[int | None] = mapped_column(sa.ForeignKey("buyers.id", ondelete="SET NULL"), index=True)
    source: Mapped[InquirySource] = mapped_column(str_enum(InquirySource))
    tg_chat_id: Mapped[int | None] = mapped_column(sa.BigInteger)
    tg_message_id: Mapped[int | None] = mapped_column(sa.BigInteger)
    text: Mapped[str | None] = mapped_column(sa.Text)
    match_method: Mapped[MatchMethod | None] = mapped_column(str_enum(MatchMethod))
    match_confidence: Mapped[float | None]
    status: Mapped[InquiryStatus] = mapped_column(str_enum(InquiryStatus), default=InquiryStatus.NEW, index=True)
    notify_message_id: Mapped[int | None] = mapped_column(sa.BigInteger)


class Reservation(TimestampMixin, Base):
    __tablename__ = "reservations"

    item_id: Mapped[int] = mapped_column(sa.ForeignKey("items.id", ondelete="CASCADE"), index=True)
    buyer_id: Mapped[int | None] = mapped_column(sa.ForeignKey("buyers.id", ondelete="SET NULL"))
    quantity: Mapped[int] = mapped_column(default=1)
    until: Mapped[datetime | None]
    status: Mapped[ReservationStatus] = mapped_column(
        str_enum(ReservationStatus), default=ReservationStatus.ACTIVE
    )


class Sale(TimestampMixin, Base):
    __tablename__ = "sales"

    item_id: Mapped[int] = mapped_column(sa.ForeignKey("items.id", ondelete="CASCADE"), index=True)
    buyer_id: Mapped[int | None] = mapped_column(sa.ForeignKey("buyers.id", ondelete="SET NULL"))
    quantity: Mapped[int] = mapped_column(default=1)
    final_price: Mapped[Decimal]
    sold_at: Mapped[datetime] = mapped_column(server_default=sa.func.now())


class ExternalListing(TimestampMixin, Base):
    """Bazaraki / Kyma / Facebook listing of an item."""

    __tablename__ = "external_listings"
    __table_args__ = (sa.UniqueConstraint("item_id", "platform", name="uq_external_listings_item_platform"),)

    item_id: Mapped[int] = mapped_column(sa.ForeignKey("items.id", ondelete="CASCADE"), index=True)
    platform: Mapped[ExternalPlatform] = mapped_column(str_enum(ExternalPlatform))
    status: Mapped[ExternalStatus] = mapped_column(str_enum(ExternalStatus), default=ExternalStatus.WAITING)
    url: Mapped[str | None] = mapped_column(sa.Text)
    external_id: Mapped[str | None] = mapped_column(sa.String(100))
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    filled_at: Mapped[datetime | None]
    published_at: Mapped[datetime | None]
    removed_at: Mapped[datetime | None]
    error: Mapped[str | None] = mapped_column(sa.Text)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(sa.String(100), primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(server_default=sa.func.now(), onupdate=sa.func.now())
