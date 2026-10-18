"""Post watcher against Postgres: notices posts deleted by chats, finds the moderator's explanation, reposts; /posts overview.

Run:  TEST_DATABASE_URL=postgresql://... pytest   (the database is wiped!)
"""

import os
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set")

from telethon import errors  # noqa: E402

from app.bot import keyboards, texts  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.enums import ItemStatus, Language, PublicationStatus  # noqa: E402
from app.db.models import Chat, Item, Publication  # noqa: E402
from app.db.session import _sessionmaker, session_scope  # noqa: E402
from app.services import post_watch  # noqa: E402
from app.userbot.chat_inspector import ChatSnapshot  # noqa: E402

ME = 42


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
        kipr = Chat(tg_chat_id=-1001, username="kipr6", title="Kipr", is_active=True)
        market = Chat(tg_chat_id=-1002, username="cyprus_market", title="Cyprus Market", is_active=True)
        gone_chat = Chat(tg_chat_id=-1003, username="lost", title="Lost", is_active=True)
        cases = Item(title="Чехлы", status=ItemStatus.PUBLISHED, price=Decimal(70), min_price=Decimal(60), quantity_total=2)
        lamp = Item(title="Лампа", status=ItemStatus.PUBLISHED, price=Decimal(20), min_price=Decimal(15), quantity_total=1)
        sold = Item(title="Кресло", status=ItemStatus.SOLD, price=Decimal(50), min_price=Decimal(40), quantity_total=1,
                    quantity_sold=1)
        s.add_all([kipr, market, gone_chat, cases, lamp, sold])
        await s.flush()
        now = datetime.now(UTC)

        def pub(item, chat, ids, status=PublicationStatus.POSTED):
            return Publication(item_id=item.id, chat_id=chat.id, text=f"{item.title}\n\nТекст", language=Language.RU,
                               status=status, tg_message_ids=ids, text_message_id=ids[0] if ids else None,
                               posted_at=now if ids else None)

        pubs = dict(
            cases_kipr=pub(cases, kipr, [101, 102]),     # one photo of the album deleted: still alive
            lamp_kipr=pub(lamp, kipr, [201]),            # deleted, a moderator explained why
            cases_market=pub(cases, market, [301]),      # deleted silently
            lamp_market=pub(lamp, market, [], PublicationStatus.APPROVED),
            sold_kipr=pub(sold, kipr, [401], PublicationStatus.DELETED),  # we deleted it: not watched
            cases_lost=pub(cases, gone_chat, [501]),
        )
        s.add_all(pubs.values())
        await s.flush()
        return SimpleNamespace(kipr=kipr.id, market=market.id, lost=gone_chat.id, cases=cases.id, lamp=lamp.id,
                               **{k: v.id for k, v in pubs.items()})


def msg(id_, text, reply_to=None, sender="mod_anna", sender_id=7):
    async def get_sender():
        return SimpleNamespace(username=sender, bot=sender.endswith("bot"))
    return SimpleNamespace(id=id_, message=text, reply_to=SimpleNamespace(reply_to_msg_id=reply_to) if reply_to else None,
                           sender_id=sender_id, entities=None, get_sender=get_sender)


class FakeUserbot:
    def __init__(self):
        self.alive = {"@kipr6": {101: False, 102: True, 201: False}, "@cyprus_market": {301: False}}
        self.history = {"@kipr6": [msg(205, "Привет всем"), msg(206, "Объявления только с ценой в рублях", reply_to=201),
                                   msg(207, "@ressell_1, прочитай правила", sender="guard_bot", sender_id=8),
                                   msg(208, "мой ответ", sender_id=ME)]}
        self.get_messages_calls = 0

    async def get_input_entity(self, ref):
        if ref == -1003 or ref == "@lost":
            raise ValueError("not in dialogs")  # forces the username path below
        raise ValueError("by username")

    async def get_messages(self, peer, ids):
        self.get_messages_calls += 1
        if peer == "@lost":
            raise errors.ChannelPrivateError(request=None)
        state = self.alive[peer]
        return [msg(i, "x") if state.get(i, True) else None for i in ids]

    async def iter_messages(self, peer, min_id=0, limit=None, reverse=False):
        for m in self.history.get(peer, []):
            if m.id > min_id:
                yield m

    async def get_me(self):
        return SimpleNamespace(id=ME, username="ressell_1")


class Notifier:
    def __init__(self):
        self.removed, self.alerts = [], []

    async def post_removed(self, r):
        self.removed.append(r)

    async def publisher_alert(self, t):
        self.alerts.append(t)


@pytest.fixture
def patched(monkeypatch):
    async def resolve(client, tg_chat_id, username):
        return f"@{username}"

    async def inspect(client, ref):
        return ChatSnapshot(tg_chat_id=-1001, username="kipr6", title="Kipr", kind="group", members_count=10, about="",
                            can_send_photos=False, problems=["Telegram: фото в этом чате запрещены для всех участников"])

    monkeypatch.setattr(post_watch, "resolve_chat", resolve)
    monkeypatch.setattr(post_watch, "inspect_chat", inspect)


async def test_detects_removed_posts_and_reasons(world, patched):
    n, client = Notifier(), FakeUserbot()
    watcher = post_watch.PostWatcher(get_settings(), client, n)
    res = await watcher.check_all()

    assert res.checked == 4  # the lost chat's post is not counted
    by_pub = {r.publication_id: r for r in n.removed}
    assert set(by_pub) == {world.lamp_kipr, world.cases_market}
    lamp = by_pub[world.lamp_kipr]
    assert lamp.clues == ["@mod_anna: Объявления только с ценой в рублях", "@guard_bot (бот): @ressell_1, прочитай правила"]
    assert "фото" in lamp.chat_problem and lamp.can_repost
    assert by_pub[world.cases_market].clues == []

    async with session_scope() as s:
        st = {p.id: p.status for p in await s.scalars(select(Publication))}
        assert st[world.cases_kipr] == PublicationStatus.POSTED  # album still partly there
        assert st[world.lamp_kipr] == st[world.cases_market] == PublicationStatus.REMOVED_BY_CHAT
        assert st[world.sold_kipr] == PublicationStatus.DELETED
        assert (await s.get(Chat, world.kipr)).can_send_photos is False  # permissions refreshed
        lost = await s.get(Chat, world.lost)
        assert not lost.is_active and not lost.can_post
    assert len(n.alerts) == 1 and "Lost" in n.alerts[0]

    # Rendering doesn't break and warns about repeated removals.
    body = texts.post_removed(lamp)
    assert "Возможная причина" in body and "ценой в рублях" in body
    assert [b.text for row in keyboards.post_removed(lamp) for b in row][0] == "🔁 Опубликовать снова"

    # Next round: nothing new, the lost chat is not re-reported.
    n2 = Notifier()
    await post_watch.PostWatcher(get_settings(), client, n2).check_all()
    assert n2.removed == [] and n2.alerts == []


async def test_repost_and_overview(world):
    ok, note = await post_watch.repost(world.lamp_kipr)
    assert ok and "очеред" in note
    ok, note = await post_watch.repost(world.lamp_kipr)
    assert not ok and "уже стоит" in note
    ok, _ = await post_watch.repost(world.cases_market)
    assert ok

    rows = await post_watch.overview()
    by_item = {item.id: {chat.id: pub.status for pub, chat in pubs} for item, pubs in rows}
    assert by_item[world.lamp] == {world.kipr: PublicationStatus.APPROVED, world.market: PublicationStatus.APPROVED}
    assert by_item[world.cases][world.kipr] == PublicationStatus.POSTED

    summary = texts.posts_summary(rows)
    assert "/posts_" in summary and "✅" in summary
    detail = texts.posts_detail(*(await post_watch.overview(world.cases))[0])
    assert 'href="https://t.me/kipr6/101"' in detail and "⏳" in detail
