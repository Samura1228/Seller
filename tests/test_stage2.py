"""Stage 2 end-to-end against Postgres: add chat → confirm → versions → approve → publisher (Telegram & AI mocked).

Run:  TEST_DATABASE_URL=postgresql://... pytest   (the database is wiped!)
"""

import io
import os
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from PIL import Image
from sqlalchemy import select, text

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set")

from telethon import errors  # noqa: E402

from app.ai.schemas import ChatRules, ChatVersion  # noqa: E402
from app.bot import texts  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.enums import (  # noqa: E402
    ApprovalKind,
    ApprovalStatus,
    Condition,
    ItemStatus,
    Language,
    PublicationStatus,
    RulesStatus,
    TextPlatform,
    TextStatus,
)
from app.db.models import Approval, Chat, Item, ItemPhoto, ItemText, Publication  # noqa: E402
from app.db.session import _sessionmaker, session_scope  # noqa: E402
from app.services import chats as chat_service  # noqa: E402
from app.services import publisher as publisher_mod  # noqa: E402
from app.services import settings_store, versions  # noqa: E402
from app.services.publisher import Publisher  # noqa: E402
from app.userbot.chat_inspector import ChatSnapshot  # noqa: E402

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=ZoneInfo("Asia/Nicosia"))


@pytest.fixture(scope="module", autouse=True)
async def fresh_db():
    engine = _sessionmaker().kw["bind"]
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
        await conn.run_sync(Base.metadata.create_all)
    yield


def rules(**kw) -> ChatRules:
    base = dict(rules_found=True, posting_allowed=True, max_posts_per_day=None, min_interval_hours=None,
                same_item_repeat_hours=None, requires_price=True, required_hashtags=["#продам"], allowed_hashtags=["#электроника"],
                max_photos=3, max_text_length=None, allowed_days=[], format_notes_ru="", forbidden_ru="",
                summary_ru="1 пост в сутки", confidence=0.9, questions_ru=[])
    base.update(kw)
    return ChatRules(**base)


def snapshot(i: int, username: str, recent: list[str]) -> ChatSnapshot:
    return ChatSnapshot(tg_chat_id=-1000000000000 - i, username=username, title=f"Chat {i}", kind="group",
                        members_count=1000 * i, about="Правила: 1 объявление в сутки. #продам обязателен",
                        recent_texts=recent)


async def make_item(title: str, price: int, ru: str, en: str) -> int:
    settings = get_settings()
    async with session_scope() as s:
        item = Item(title=title, folder_hint=title, status=ItemStatus.READY, condition=Condition.LIKE_NEW,
                    price=Decimal(price), min_price=Decimal(price - 10), quantity_total=1)
        s.add(item)
        await s.flush()
        rel = f"photos/{item.id}/0.jpg"
        (settings.data_dir / rel).parent.mkdir(parents=True, exist_ok=True)
        buf = io.BytesIO()
        Image.new("RGB", (32, 32), (9, 9, 9)).save(buf, "JPEG")
        (settings.data_dir / rel).write_bytes(buf.getvalue())
        s.add(ItemPhoto(item_id=item.id, path=rel, thumb_path=rel, width=32, height=32, sha256=f"h{item.id}", is_cover=True))
        for lang, body in ((Language.RU, ru), (Language.EN, en)):
            s.add(ItemText(item_id=item.id, platform=TextPlatform.TELEGRAM_MASTER, language=lang,
                           title=f"{title} {lang.value}", body=body, status=TextStatus.APPROVED))
        return item.id


class FakeNotifier:
    def __init__(self):
        self.posted, self.alerts = [], []

    async def publication_posted(self, item_id, item_name, chat_title, link):
        self.posted.append((item_id, chat_title, link))

    async def publisher_alert(self, text):
        self.alerts.append(text)


class FakeSender:
    def __init__(self):
        self.calls, self.raise_next = [], None
        self._id = 100

    async def __call__(self, client, tg_chat_id, username, text, photos, with_photos, topic_id=None):
        if self.raise_next:
            e, self.raise_next = self.raise_next, None
            raise e
        self.calls.append((tg_chat_id, text, [p.name for p in photos], with_photos))
        self._id += 1
        return [self._id], self._id


@pytest.fixture
def mock_ai(monkeypatch):
    seen = []

    async def structured(*, purpose, output, content=None, **_):
        seen.append((purpose, content))
        if output is ChatRules:
            return rules(confidence=0.5 if "chat_b" in content else 0.9)
        if output is ChatVersion:
            lang = "ru" if "Language: Russian" in content else "en"
            return ChatVersion(title=f"Версия {lang}", body=f"текст {len(seen)}", hashtags=["#электроника", "продам"])
        raise AssertionError(output)

    monkeypatch.setattr(chat_service, "structured", structured)
    monkeypatch.setattr(versions, "structured", structured)
    return seen


async def test_stage2_flow(monkeypatch, mock_ai):
    settings = get_settings()
    snaps = {
        "@chat_a": snapshot(1, "chat_a", ["Продам стол, Лимасол, 50 евро"] * 10),
        "@chat_b": snapshot(2, "chat_b", ["Selling a lamp in Limassol, 20 euro"] * 10),
    }

    async def fake_inspect(client, ref, recent_limit=80):
        return snaps[ref]

    monkeypatch.setattr(chat_service, "inspect_chat", fake_inspect)

    # --- add chats: language detected, rules parsed, cards queued ---
    a = await chat_service.add_chat(None, "@chat_a")
    b = await chat_service.add_chat(None, "@chat_b")
    async with session_scope() as s:
        ca, cb = await s.get(Chat, a.chat_id), await s.get(Chat, b.chat_id)
        assert (ca.language.value, cb.language.value) == ("ru", "en")
        assert ca.rules_status == RulesStatus.AUTO and cb.rules_status == RulesStatus.NEEDS_ME
        assert not ca.is_active
        card = texts.chat_card(ca)
        assert "Лимит: 1 пост(а) в сутки (по умолчанию)" in card and "#продам" in card
    # re-adding the same chat updates it instead of duplicating
    await chat_service.add_chat(None, "@chat_a")
    async with session_scope() as s:
        assert len(list(await s.scalars(select(Chat)))) == 2
        assert len(list(await s.scalars(select(Approval).where(Approval.kind == ApprovalKind.CHAT_RULES)))) == 2

    # --- items exist before chats are confirmed: versions appear when a chat is activated ---
    cheap = await make_item("Лампа", 40, "Хорошая лампа", "Nice lamp")
    pricey = await make_item("Наушники", 150, "Отличные наушники", "Great headphones")
    await chat_service.confirm(a.chat_id)
    await chat_service.confirm(b.chat_id)
    assert await versions.generate_for_chat(a.chat_id) == 2
    assert await versions.generate_for_chat(b.chat_id) == 2
    assert await versions.generate_for_item(cheap) == 0  # nothing missing

    async with session_scope() as s:
        pubs = list(await s.scalars(select(Publication).order_by(Publication.id)))
        assert len(pubs) == 4 and all(p.status == PublicationStatus.DRAFT for p in pubs)
        by_chat = {p.chat_id: p.language for p in pubs}
        assert by_chat == {a.chat_id: Language.RU, b.chat_id: Language.EN}
        assert pubs[0].hashtags == ["#продам", "#электроника"]  # required first, deduped, '#' added
        previews = list(await s.scalars(select(Approval).where(Approval.kind == ApprovalKind.CHAT_PREVIEWS)))
        assert len(previews) == 2
        item = await s.get(Item, pricey)
        rows = await versions.drafts(s, pricey)
        before, card = texts.previews_card(item, rows, settings.seller_username)
        assert before == [] and "Пишите: @limassol_seller" in card and "Message: @limassol_seller" in card
    # the second version of an item is told to differ from the first
    assert any("[Version for another chat" in (c or "") for p, c in mock_ai if p == "chat_version")

    assert await versions.approve(cheap) == 2
    assert await versions.approve(pricey) == 2

    # --- publisher ---
    sender, notifier = FakeSender(), FakeNotifier()
    pub = Publisher(settings, client=None, notifier=notifier, sender=sender)
    monkeypatch.setattr(pub, "now", lambda: NOW)

    assert await pub.tick() == "выключен"
    await pub.set_enabled(True)
    result = await pub.tick()
    assert result.startswith("опубликовано: Наушники")  # pricier item first
    tg_chat, text_sent, photos, with_photos = sender.calls[0]
    assert "\nЦена: 150 €" in text_sent and "@limassol_seller" in text_sent and text_sent.endswith("#продам #электроника")
    assert photos == ["0.jpg"] and with_photos
    assert notifier.posted[0][2] in ("https://t.me/chat_a/101", "https://t.me/chat_b/101")

    async with session_scope() as s:
        item = await s.get(Item, pricey)
        assert item.status == ItemStatus.PUBLISHED and item.first_published_at is not None
    assert "между постами" in await pub.tick()  # random gap after a post

    # forced posts: per-chat limit (1/day) still applies, and the same item isn't posted twice in a row
    r2 = await pub.tick(force=True)
    assert r2.startswith("опубликовано: Лампа")
    async with session_scope() as s:
        posted = list(await s.scalars(select(Publication).where(Publication.status == PublicationStatus.POSTED)))
        assert {p.chat_id for p in posted} == {a.chat_id, b.chat_id}
    assert (await pub.tick(force=True)).startswith("нет постов")
    st = await pub.status()
    assert st.approved == 2 and st.ready_chats == 0 and st.posted_24h == 2

    # --- errors: FloodWait pauses everything; a banned chat is switched off ---
    await settings_store.put(publisher_mod.K_ENABLED, True)
    later = NOW.replace(day=20, hour=13)
    monkeypatch.setattr(pub, "now", lambda: later)
    sender.raise_next = errors.FloodWaitError(request=None, capture=600)
    assert "FloodWait" in await pub.tick()
    assert "пауза до" in await pub.tick() and "FloodWait" in notifier.alerts[-1]

    await settings_store.put_time(publisher_mod.K_PAUSED, None)
    sender.raise_next = errors.ChatWriteForbiddenError(request=None)
    assert await pub.tick(force=True) == "чат выключен"
    async with session_scope() as s:
        failed = await s.scalar(select(Publication).where(Publication.status == PublicationStatus.FAILED))
        chat = await s.get(Chat, failed.chat_id)
        assert not chat.is_active and not chat.can_post and "ChatWriteForbidden" in chat.check_note

    # --- withdrawn items are never posted and their cards are closed ---
    from app.services import pipeline
    await pipeline.withdraw(cheap)
    async with session_scope() as s:
        open_cards = await s.scalar(select(Approval).where(
            Approval.item_id == cheap, Approval.status.in_([ApprovalStatus.PENDING, ApprovalStatus.SKIPPED])))
        assert open_cards is None
    candidates, _, _ = await pub._load(later)
    assert all(c.item_id != cheap for c in candidates)


async def test_rules_edit_and_language(mock_ai):
    async with session_scope() as s:
        chat = await s.scalar(select(Chat).where(Chat.username == "chat_b"))
    lang = await chat_service.cycle_language(chat.id)
    assert lang.value == "mixed"  # en -> mixed (manual override); mixed chats get Russian posts
    await chat_service.edit_rules(chat.id, "можно 2 поста в день")
    async with session_scope() as s:
        chat = await s.get(Chat, chat.id)
        assert chat.rules_status == RulesStatus.CONFIRMED and "2 поста" in chat.notes
        assert chat_service.post_language(chat) == "ru"


async def test_queue_shows_chat_card(monkeypatch, mock_ai):
    from app.bot.core import BotCore
    from tests.test_pipeline import FakeTg

    async def fake_inspect(client, ref, recent_limit=80):
        return snapshot(3, "chat_c", ["Продам шкаф"] * 5)

    monkeypatch.setattr(chat_service, "inspect_chat", fake_inspect)
    res = await chat_service.add_chat(None, "@chat_c")
    core = BotCore(get_settings())
    core.client = FakeTg()
    await core.show_next()
    cards = [m for m in core.client.sent if m[2]]
    assert len(cards) == 1 and f"Чат #{res.chat_id}" in cards[0][1]
    datas = [b.type.data for row in cards[0][2] for b in row]
    assert f"ch:ok:{res.chat_id}".encode() in datas
    await core.show_next()
    assert len([m for m in core.client.sent if m[2]]) == 1  # still one card at a time


async def test_runner_lock_is_exclusive():
    import asyncio

    from app.db.lock import acquire_runner_lock

    url = get_settings().database_url
    first = await acquire_runner_lock(url)
    second = asyncio.create_task(acquire_runner_lock(url, poll_seconds=0.1))
    await asyncio.sleep(0.5)
    assert not second.done()
    await first.close()
    conn = await asyncio.wait_for(second, timeout=5)
    await conn.close()
