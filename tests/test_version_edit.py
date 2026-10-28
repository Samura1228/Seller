"""Editing per-chat versions before approval: one version, all versions (+ master, remembered), reply routing.

Run:  TEST_DATABASE_URL=postgresql://... pytest   (the database is wiped!)
"""

import os
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import selectinload

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set")

from app.ai.schemas import EditedPost, MasterTexts  # noqa: E402
from app.bot import texts  # noqa: E402
from app.bot.handlers.review import ALL_VERSIONS  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.enums import ItemStatus, Language, PublicationStatus, TextPlatform, TextStatus  # noqa: E402
from app.db.models import Chat, Item, ItemText, Publication  # noqa: E402
from app.db.session import _sessionmaker, session_scope  # noqa: E402
from app.services import copywriting, versions  # noqa: E402

NOTE = "Мелкие замечания: на коробке следы от наклейки."


@pytest.fixture(scope="module", autouse=True)
async def fresh_db():
    engine = _sessionmaker().kw["bind"]
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
        await conn.run_sync(Base.metadata.create_all)
    yield


@pytest.fixture(scope="module")
async def world():
    async with session_scope() as s:
        item = Item(title="Колонка B&O", status=ItemStatus.READY, price=Decimal(185), min_price=Decimal(160), quantity_total=1)
        item.texts = [
            ItemText(platform=TextPlatform.TELEGRAM_MASTER, language=Language.RU, title="Колонка B&O",
                     body=f"Новая, запечатана.\n{NOTE}", status=TextStatus.APPROVED),
            ItemText(platform=TextPlatform.TELEGRAM_MASTER, language=Language.EN, title="B&O speaker",
                     body="New, sealed.\nMinor notes: sticker marks on the box.", status=TextStatus.APPROVED),
        ]
        a = Chat(tg_chat_id=-1001, username="kipr6", title="Kipr", is_active=True)
        b = Chat(tg_chat_id=-1002, username="ads_cy", title="Объявления | Кипр", is_active=True)
        s.add_all([item, a, b])
        await s.flush()
        pubs = [Publication(item_id=item.id, chat_id=c.id, text=f"Колонка B&O\n\nНовая. {NOTE}", language=Language.RU,
                            status=PublicationStatus.DRAFT, preview_message_id=900 + n) for n, c in enumerate([a, b])]
        s.add_all(pubs)
        await s.flush()
        return SimpleNamespace(item=item.id, p1=pubs[0].id, p2=pubs[1].id)


@pytest.fixture
def ai(monkeypatch):
    calls = []

    async def version_ai(*, purpose, output, content, **_):
        assert output is EditedPost and purpose == "version_edit"
        calls.append(content)
        post = content.split("[Post]\n", 1)[1].split("\n\n[Owner's instruction]", 1)[0]
        return EditedPost(text="✅ " + post.replace(f" {NOTE}", ""))  # stray emoji must be stripped

    async def master_ai(*, purpose, output, content, **_):
        assert output is MasterTexts and purpose == "listing_edit"
        calls.append(content)
        return MasterTexts.model_validate({"ru": {"title": "Колонка B&O", "body": "Новая, запечатана."},
                                           "en": {"title": "B&O speaker", "body": "New, sealed."}})

    monkeypatch.setattr(versions, "structured", version_ai)
    monkeypatch.setattr(copywriting, "structured", master_ai)
    return calls


async def _pubs():
    async with session_scope() as s:
        return {p.id: p.text for p in await s.scalars(select(Publication))}


async def test_edit_one_version(world, ai):
    assert (await versions.find_draft_by_preview(901)).id == world.p2
    assert await versions.edit_drafts(world.item, "убери про замечания", world.p2) == 1
    got = await _pubs()
    assert got[world.p2] == "Колонка B&O\n\nНовая." and NOTE in got[world.p1]
    async with session_scope() as s:
        assert (await s.get(Item, world.item)).text_rules == []  # a one-chat tweak isn't a standing rule


async def test_edit_all_versions_updates_master_and_is_remembered(world, ai):
    assert await versions.edit_drafts(world.item, "убери во всех упоминание о мелких замечаниях") == 2
    got = await _pubs()
    assert all(NOTE not in t and "✅" not in t for t in got.values())
    async with session_scope() as s:
        item = await s.get(Item, world.item, options=[selectinload(Item.texts)])
        assert item.text_rules == ["убери во всех упоминание о мелких замечаниях"]
        ru = copywriting.master_text(item, Language.RU)
        assert ru.body == "Новая, запечатана." and ru.status == TextStatus.APPROVED and item.status == ItemStatus.READY
        rows = await versions.drafts(s, world.item)
        before, card = texts.previews_card(item, rows, "ressell_1")
    assert "ответь на эту карточку" in card


def test_all_versions_wording():
    assert ALL_VERSIONS.search("Убери во всех объявлениях упоминание о мелких замечаниях")
    assert not ALL_VERSIONS.search("убери последнее предложение")


async def test_long_previews_remember_messages_and_reply_routes(world, monkeypatch):
    """Versions too long for one card go as separate messages; a reply to one edits only it, unless it says 'во всех'."""
    import asyncio

    from app.bot.core import BotCore
    from app.bot.handlers import review
    from app.config import get_settings
    from app.db.enums import ApprovalKind
    from app.db.models import Approval
    from tests.test_pipeline import FakeTg

    long_text = "Колонка B&O\n\n" + "Отличный звук. " * 120  # ~1800 chars each → the card is split
    async with session_scope() as s:
        for p in await s.scalars(select(Publication)):
            p.text, p.preview_message_id = long_text, None
        s.add(Approval(item_id=world.item, kind=ApprovalKind.CHAT_PREVIEWS))
        await s.flush()
        aid = (await s.scalar(select(Approval).where(Approval.kind == ApprovalKind.CHAT_PREVIEWS))).id

    core = BotCore(get_settings())
    core.client = FakeTg()
    await core.show_approval(aid, with_photos=False)
    version_msgs = [m for m in core.client.sent if "📍" in m[1]]
    assert len(version_msgs) == 2
    async with session_scope() as s:
        ids = {p.id: p.preview_message_id for p in await s.scalars(select(Publication))}
    assert sorted(ids.values()) == sorted(m[0] for m in version_msgs)

    started = []
    monkeypatch.setattr("app.bot.handlers.chats.apply_versions_edit",
                        lambda core, item_id, text, pub_id=None: started.append((item_id, text, pub_id)) or True)

    class Event:
        def __init__(self, reply_to, raw):
            self.reply_to_msg_id, self.raw_text, self.replies = reply_to, raw, []

        async def reply(self, t, **_):
            self.replies.append(t)

    one = Event(ids[world.p1], "убери последнее предложение")
    assert await review._reply_to_versions(core, one)
    every = Event(ids[world.p1], "Убери во всех объявлениях упоминание о мелких замечаниях")
    assert await review._reply_to_versions(core, every)
    async with session_scope() as s:
        card_id = (await s.get(Approval, aid)).bot_message_id
    assert await review._reply_to_versions(core, Event(card_id, "короче"))
    assert not await review._reply_to_versions(core, Event(12345, "привет"))  # unrelated reply: normal handling
    assert started == [(world.item, "убери последнее предложение", world.p1),
                       (world.item, "Убери во всех объявлениях упоминание о мелких замечаниях", None),
                       (world.item, "короче", None)]

    # Re-sending the card marks the old version messages as outdated.
    await core.show_approval(aid, with_photos=False)
    await asyncio.sleep(0.05)
    assert sum(1 for _, t, _ in core.client.edits if t == "Версия обновлена ниже ⬇️") == 2
