from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

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
from app.userbot.chat_inspector import detect_language, normalize_ref
from app.userbot.sender import message_link

TZ = ZoneInfo("Asia/Nicosia")
NOW = datetime(2026, 10, 19, 12, 0, tzinfo=TZ)  # Monday noon


def test_chat_limits_from_rules_and_slowmode():
    lim = chat_limits({"max_posts_per_day": 2, "min_interval_hours": 3, "allowed_days": ["sat", "sun"]}, 600, 1)
    assert lim.per_day == 2 and lim.min_interval == timedelta(hours=3) and lim.allowed_weekdays == {5, 6}
    lim = chat_limits({}, 7200, 1)  # slow mode stricter than rules
    assert lim.per_day == 1 and lim.min_interval == timedelta(hours=2) and not lim.allowed_weekdays


def test_chat_ready():
    lim = chat_limits({"max_posts_per_day": 2, "min_interval_hours": 4}, None, 1)
    assert chat_ready(ChatState(1, lim, 0, None), NOW)
    assert not chat_ready(ChatState(1, lim, 2, None), NOW)                                  # daily cap
    assert not chat_ready(ChatState(1, lim, 1, NOW - timedelta(hours=3)), NOW)              # interval
    assert chat_ready(ChatState(1, lim, 1, NOW - timedelta(hours=5)), NOW)
    assert not chat_ready(ChatState(1, lim, 0, None, blocked_until=NOW + timedelta(minutes=5)), NOW)
    weekend = chat_limits({"allowed_days": ["sat"]}, None, 1)
    assert not chat_ready(ChatState(1, weekend, 0, None), NOW)


def test_pick_priority_and_no_repeat():
    cands = [
        Candidate(1, item_id=10, chat_id=1, price=50, priority=0, has_inquiries=False),
        Candidate(2, item_id=11, chat_id=1, price=200, priority=0, has_inquiries=True),
        Candidate(3, item_id=12, chat_id=2, price=120, priority=0, has_inquiries=False),
        Candidate(4, item_id=13, chat_id=1, price=20, priority=5, has_inquiries=False),
    ]
    assert pick(cands, {1, 2}, None).publication_id == 4          # manual boost first
    assert pick(cands[:3], {1, 2}, None).publication_id == 3      # no inquiries + pricier beats inquiries
    assert pick(cands[:3], {1, 2}, 12).publication_id == 1        # don't repeat the last item
    assert pick(cands[:3], {1}, None).publication_id == 1         # only ready chats
    assert pick(cands, set(), None) is None
    only_same = [Candidate(5, 10, 1, 50, 0, False)]
    assert pick(only_same, {1}, 10).publication_id == 5           # repeat only if nothing else


def test_global_block_reason():
    ok = GlobalState(enabled=True, paused_until=None, next_at=None, posts_last_hour=0, posts_last_24h=0)
    args = ((9, 21), 3, 15)
    assert global_block_reason(ok, NOW, *args) is None
    assert global_block_reason(GlobalState(False, None, None, 0, 0), NOW, *args) == "выключен"
    assert "пауза до" in global_block_reason(GlobalState(True, NOW + timedelta(hours=1), None, 0, 0), NOW, *args)
    assert "вне окна" in global_block_reason(ok, NOW.replace(hour=22), *args)
    assert "в час" in global_block_reason(GlobalState(True, None, None, 3, 3), NOW, *args)
    assert "в сутки" in global_block_reason(GlobalState(True, None, None, 0, 15), NOW, *args)
    assert "между постами" in global_block_reason(GlobalState(True, None, NOW + timedelta(minutes=9), 0, 0), NOW, *args)
    gap = next_gap(12, 35)
    assert timedelta(minutes=12) <= gap <= timedelta(minutes=35)


def test_detect_language():
    ru = ["Продам велосипед, состояние отличное, Лимасол"] * 8 + ["iPhone 15 for sale, Limassol"] * 2
    en = ["Selling a sofa in great condition, pickup only"] * 9 + ["Продам диван"]
    assert detect_language(ru) == "ru"
    assert detect_language(en) == "en"
    assert detect_language(ru[:5] + en[:5]) == "mixed"
    assert detect_language(["ok", "👍"]) == "mixed"


def test_refs_and_links():
    assert normalize_ref("https://t.me/kipr6") == "@kipr6"
    assert normalize_ref("@cyprus_market") == "@cyprus_market"
    assert normalize_ref("sellbuycy") == "@sellbuycy"
    assert normalize_ref("https://t.me/+AbCdEf") == "https://t.me/+AbCdEf"
    assert message_link(-1001234567890, "kipr6", 55) == "https://t.me/kipr6/55"
    assert message_link(-1001234567890, None, 55) == "https://t.me/c/1234567890/55"
