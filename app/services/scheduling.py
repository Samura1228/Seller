"""Pure scheduling rules (no I/O) so they are easy to test: which chat may receive a post now, and which post goes next."""

import random
from dataclasses import dataclass
from datetime import datetime, timedelta

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


@dataclass(frozen=True)
class ChatLimits:
    per_day: int
    min_interval: timedelta
    allowed_weekdays: frozenset[int]  # 0 = Monday; empty = any day


def chat_limits(rules: dict | None, slowmode_seconds: int | None, default_per_day: int) -> ChatLimits:
    rules = rules or {}
    per_day = rules.get("max_posts_per_day") or default_per_day
    hours = rules.get("min_interval_hours") or 0
    interval = max(timedelta(hours=hours), timedelta(seconds=slowmode_seconds or 0))
    days = frozenset(WEEKDAYS.index(d) for d in (rules.get("allowed_days") or []) if d in WEEKDAYS)
    return ChatLimits(per_day=max(1, int(per_day)), min_interval=interval, allowed_weekdays=days)


@dataclass(frozen=True)
class ChatState:
    chat_id: int
    limits: ChatLimits
    posts_last_24h: int
    last_post_at: datetime | None
    blocked_until: datetime | None = None


def chat_ready(state: ChatState, now_local: datetime) -> bool:
    """now_local must be timezone-aware (Cyprus time) so weekday rules match the chat's audience."""
    if state.blocked_until and now_local < state.blocked_until:
        return False
    if state.limits.allowed_weekdays and now_local.weekday() not in state.limits.allowed_weekdays:
        return False
    if state.posts_last_24h >= state.limits.per_day:
        return False
    return not (state.last_post_at and now_local - state.last_post_at < state.limits.min_interval)


@dataclass(frozen=True)
class Candidate:
    publication_id: int
    item_id: int
    chat_id: int
    price: float
    priority: int
    has_inquiries: bool


def pick(candidates: list[Candidate], ready_chats: set[int], last_item_id: int | None) -> Candidate | None:
    """Priority: manual boost, then items without inquiries, then more expensive items, then oldest version.
    Avoids posting the same item twice in a row (looks like spam) when there is an alternative."""
    eligible = [c for c in candidates if c.chat_id in ready_chats]
    if not eligible:
        return None
    eligible.sort(key=lambda c: (-c.priority, c.has_inquiries, -c.price, c.publication_id))
    for c in eligible:
        if c.item_id != last_item_id:
            return c
    return eligible[0]


@dataclass(frozen=True)
class GlobalState:
    enabled: bool
    paused_until: datetime | None
    next_at: datetime | None
    posts_last_hour: int
    posts_last_24h: int


def global_block_reason(g: GlobalState, now_local: datetime, window: tuple[int, int],
                        max_per_hour: int, max_per_day: int) -> str | None:
    """None if a post may go out now, otherwise a short Russian reason (shown in /publisher)."""
    if not g.enabled:
        return "выключен"
    if g.paused_until and now_local < g.paused_until:
        return f"пауза до {g.paused_until.astimezone(now_local.tzinfo):%d.%m %H:%M}"
    start, end = window
    if not start <= now_local.hour < end:
        return f"вне окна публикаций ({start}:00–{end}:00)"
    if g.posts_last_hour >= max_per_hour:
        return f"лимит {max_per_hour} постов в час"
    if g.posts_last_24h >= max_per_day:
        return f"лимит {max_per_day} постов в сутки"
    if g.next_at and now_local < g.next_at:
        return f"пауза между постами до {g.next_at.astimezone(now_local.tzinfo):%H:%M}"
    return None


def next_gap(min_minutes: int, max_minutes: int, rng: random.Random | None = None) -> timedelta:
    rng = rng or random.Random()
    return timedelta(seconds=rng.randint(min_minutes * 60, max(min_minutes, max_minutes) * 60))
