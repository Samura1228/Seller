"""/scanchats: discovery of buy/sell chats among the selling account's dialogs, bulk add and bulk enable."""

import asyncio
import os
import re
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text
from telethon import types

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set")

from app.ai.schemas import ChatRules, MarketplaceList, MarketplaceVerdict  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.models import Chat  # noqa: E402
from app.db.session import _sessionmaker, session_scope  # noqa: E402
from app.services import chat_discovery  # noqa: E402
from app.services import chats as chat_service  # noqa: E402
from app.userbot.chat_inspector import ChatSnapshot  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
async def fresh_db():
    engine = _sessionmaker().kw["bind"]
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
        await conn.run_sync(Base.metadata.create_all)
    yield


def channel(id_, title, username=None, broadcast=False, members=100):
    return types.Channel(id=id_, title=title, photo=types.ChatPhotoEmpty(), date=datetime(2020, 1, 1), username=username,
                         megagroup=not broadcast, broadcast=broadcast, access_hash=1, participants_count=members)


class DialogsClient:
    def __init__(self, entities):
        self.entities = entities

    async def iter_dialogs(self, limit=None):
        for e in self.entities:
            yield SimpleNamespace(entity=e)


def rules(**kw):
    base = dict(rules_found=True, posting_allowed=True, max_posts_per_day=1, min_interval_hours=None, same_item_repeat_hours=None,
                requires_price=True, required_hashtags=[], allowed_hashtags=[], max_photos=None, max_text_length=None,
                allowed_days=[], format_notes_ru="", forbidden_ru="", summary_ru="ok", confidence=0.9, questions_ru=[])
    base.update(kw)
    return ChatRules(**base)


async def test_list_and_classify(monkeypatch):
    ents = [
        channel(1, "Барахолка Лимасол", "baraholka_lim", members=9000),
        channel(2, "Cyprus News", "cy_news", broadcast=True, members=50000),
        types.Chat(id=3, title="Отдам даром Кипр", photo=types.ChatPhotoEmpty(), participants_count=300,
                   date=datetime(2020, 1, 1), version=1),
        types.User(id=4, first_name="Friend"),
        channel(5, "Already added", "kipr6"),
    ]
    known = {-1000000000005}
    found = await chat_discovery.list_groups(DialogsClient(ents), known)
    assert [f.title for f in found] == ["Барахолка Лимасол", "Cyprus News", "Отдам даром Кипр"]
    assert [f.kind for f in found] == ["group", "channel", "group"]
    assert found[2].tg_chat_id == -3

    async def structured(*, purpose, output, content=None, **_):
        assert output is MarketplaceList and "[1] Cyprus News | @cy_news | channel" in content
        return MarketplaceList(verdicts=[MarketplaceVerdict(id=0, is_marketplace=True, reason_ru="барахолка"),
                                         MarketplaceVerdict(id=1, is_marketplace=False, reason_ru="новости"),
                                         MarketplaceVerdict(id=2, is_marketplace=True, reason_ru="отдам даром"),
                                         MarketplaceVerdict(id=99, is_marketplace=True, reason_ru="мусор")])

    monkeypatch.setattr(chat_discovery, "structured", structured)
    ranked = await chat_discovery.classify(found)
    assert [(f.title, f.is_marketplace) for f in ranked] == [
        ("Барахолка Лимасол", True), ("Отдам даром Кипр", True), ("Cyprus News", False)]


async def test_channel_replaced_by_discussion_group(monkeypatch):
    chan = ChatSnapshot(tg_chat_id=-1000000000010, username="cy_market", title="Cyprus Market", kind="channel",
                        members_count=20000, about="", can_post=False, problems=["это канал"], linked_chat_id=-1000000000011)
    chat = ChatSnapshot(tg_chat_id=-1000000000011, username=None, title="Cyprus Market Chat", kind="group",
                        members_count=3000, about="Продажа вещей", recent_texts=["Продам стул"] * 5)

    async def inspect(client, ref, recent_limit=80):
        return chan if ref == "@cy_market" else chat

    async def structured(**_):
        return rules()

    monkeypatch.setattr(chat_service, "inspect_chat", inspect)
    monkeypatch.setattr(chat_service, "structured", structured)
    res = await chat_service.add_chat(None, "@cy_market")
    assert res.error is None and res.auto_rules and "Cyprus Market Chat" in res.note
    async with session_scope() as s:
        c = await s.get(Chat, res.chat_id)
        assert c.tg_chat_id == -1000000000011 and c.can_post

    chan.linked_chat_id = None  # a channel without a discussion group stays as an inactive, non-postable chat
    res = await chat_service.add_chat(None, "@cy_market")
    assert not res.auto_rules


async def test_scan_buttons_flow(monkeypatch):
    from app.bot.core import BotCore
    from app.bot.handlers import chats as chat_handlers
    from tests.test_pipeline import FakeTg

    monkeypatch.setattr(chat_handlers, "ADD_PAUSE_SECONDS", 0)
    core = BotCore(get_settings())
    chat_handlers.register(core)
    handlers = {cb.__name__: cb for cb, _ in core.client.list_event_handlers()}
    core.client = FakeTg()
    core.userbot = object()

    found = [chat_discovery.Found(-1000000000020, "Барахолка А", "bar_a", "group", 500, True, "барахолка"),
             chat_discovery.Found(-1000000000021, "Барахолка Б", None, "group", 400, True, "барахолка"),
             chat_discovery.Found(-1000000000022, "Новости", "news", "channel", 9000, False, "новости")]
    core.scan = {"found": found, "selected": {0, 1}, "auto": []}

    added = []

    async def add_chat(client, ref):
        added.append(ref)
        async with session_scope() as s:
            c = Chat(tg_chat_id=-1000000000020 - len(added) * 100, title=str(ref), can_post=True, rules={"max_posts_per_day": 1})
            s.add(c)
            await s.flush()
            return chat_service.AddResult(ref=ref, chat_id=c.id, auto_rules=ref == "@bar_a")

    monkeypatch.setattr(chat_service, "add_chat", add_chat)
    made = []
    monkeypatch.setattr(core, "make_versions", lambda item_id=None, chat_id=None: made.append(chat_id))

    class Ev:
        message_id = 777

        def __init__(self, data):
            self.data_match = re.match(rb"^sc:(\w+):(\d+)$", data)
            self.edits = []

        async def answer(self, *a, **k):
            pass

        async def edit(self, *a, **k):
            self.edits.append((a, k))

    ev = Ev(b"sc:t:1")
    await handlers["on_scan_button"](ev)  # untick Б
    assert core.scan["selected"] == {0}
    await handlers["on_scan_button"](Ev(b"sc:t:1"))  # tick it back
    await handlers["on_scan_button"](Ev(b"sc:add:0"))
    for _ in range(50):
        await asyncio.sleep(0.02)
        if core.scan.get("auto"):
            break
    assert added == ["@bar_a", -1000000000021]  # by @username when there is one, else by id
    summary = core.client.edits[-1][1]
    assert "✅ Барахолка А" in summary and "❓ Барахолка Б" in summary
    assert core.client.edits[-1][2][0][0].type.data == b"sc:auto:0"

    await handlers["on_scan_button"](Ev(b"sc:auto:0"))
    async with session_scope() as s:
        active = list(await s.scalars(select(Chat).where(Chat.is_active.is_(True))))
    assert [c.title for c in active] == ["@bar_a"] and made == [active[0].id]


async def test_folder_listing():
    from telethon import functions

    ents = [channel(31, "Лимасол | Кипр - Барахолка", "lim_bar"), channel(32, "Бізнес куточок на Кіпрі", "biz_ua"),
            channel(33, "Никосия Барахолка", "nic_bar"), channel(34, "Excluded", "ex")]

    def peer(i):
        return types.InputPeerChannel(channel_id=i, access_hash=1)

    limassol = types.DialogFilter(id=2, title=types.TextWithEntities(text="Лимассол", entities=[]), pinned_peers=[peer(31)],
                                  include_peers=[peer(32), peer(34)], exclude_peers=[peer(34)])
    nicosia = types.DialogFilter(id=3, title=types.TextWithEntities(text="Никосия", entities=[]), pinned_peers=[],
                                 include_peers=[peer(33)], exclude_peers=[])

    class Client(DialogsClient):
        async def __call__(self, request):
            assert isinstance(request, functions.messages.GetDialogFiltersRequest)
            return types.messages.DialogFilters(filters=[types.DialogFilterDefault(), limassol, nicosia])

    c = Client(ents)
    found = await chat_discovery.list_folder(c, "лимассол", set())
    assert [f.title for f in found] == ["Лимасол | Кипр - Барахолка", "Бізнес куточок на Кіпрі"]
    assert await chat_discovery.list_folder(c, "Лимассол", {-1000000000031}) is not None
    assert [f.title for f in await chat_discovery.list_folder(c, "Лимассол", {-1000000000031})] == ["Бізнес куточок на Кіпрі"]
    assert await chat_discovery.list_folder(c, "Пафос", set()) is None
    assert await chat_discovery.folder_names(c) == ["Лимассол", "Никосия"]


async def test_scanchats_folder_command(monkeypatch):

    from app.bot.core import BotCore
    from app.bot.handlers import chats as chat_handlers
    from tests.test_pipeline import FakeTg

    ents = [channel(41, "Лимасол Барахолка", "lim2"), channel(42, "Бізнес куточок", "biz2")]
    folder = types.DialogFilter(id=2, title=types.TextWithEntities(text="Лимассол", entities=[]), pinned_peers=[],
                                include_peers=[types.InputPeerChannel(channel_id=41, access_hash=1),
                                               types.InputPeerChannel(channel_id=42, access_hash=1)], exclude_peers=[])

    class Client(DialogsClient):
        async def __call__(self, request):
            return types.messages.DialogFilters(filters=[folder])

    async def structured(*, content=None, **_):
        return MarketplaceList(verdicts=[MarketplaceVerdict(id=0, is_marketplace=True, reason_ru="барахолка"),
                                         MarketplaceVerdict(id=1, is_marketplace=False, reason_ru="бизнес-услуги")])

    monkeypatch.setattr(chat_discovery, "structured", structured)
    core = BotCore(get_settings())
    chat_handlers.register(core)
    handler = next((cb, ev) for cb, ev in core.client.list_event_handlers() if cb.__name__ == "on_scanchats")
    core.client = FakeTg()
    core.userbot = Client(ents)

    m = re.match(r"^/scanchats(?:\s+(.+))?$", "/scanchats Лимассол")
    await handler[0](SimpleNamespace(pattern_match=m))
    text, buttons = core.client.edits[-1][1], core.client.edits[-1][2]
    assert "В папке «Лимассол» новых чатов: <b>2</b>" in text and "🤔" in text
    labels = [b.text for row in buttons for b in row]
    assert labels[0].startswith("✅ Лимасол Барахолка") and labels[1].startswith("✅ Бізнес куточок") and labels[1].endswith("🤔")
    assert core.scan["selected"] == {0, 1}

    m = re.match(r"^/scanchats(?:\s+(.+))?$", "/scanchats Пафос")
    await handler[0](SimpleNamespace(pattern_match=m))
    assert "не нашёл" in core.client.edits[-1][1] and "«Лимассол»" in core.client.edits[-1][1]
