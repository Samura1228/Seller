"""Stage 3 against Postgres: inquiries (replies, DMs with debounce, unidentified → clarified), reserve, sell, posts cleanup.

Run:  TEST_DATABASE_URL=postgresql://... pytest   (the database is wiped!)
"""

import asyncio
import os
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set")

from telethon import types  # noqa: E402

from app.ai.schemas import InquiryMatch  # noqa: E402
from app.bot import keyboards, texts  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.enums import (  # noqa: E402
    InquirySource,
    InquiryStatus,
    ItemStatus,
    Language,
    PublicationStatus,
    ReservationStatus,
)
from app.db.models import Chat, Inquiry, Item, Publication, Reservation, Sale  # noqa: E402
from app.db.session import _sessionmaker, session_scope  # noqa: E402
from app.services import inquiries, sales  # noqa: E402
from app.services.inquiries import BuyerInfo, Incoming  # noqa: E402
from app.userbot.listener import InquiryListener  # noqa: E402

CHAT_TG = -1000000000777


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
    """Two published items (cases x2, lamp) with posts in one chat, plus a queued post."""
    async with session_scope() as s:
        chat = Chat(tg_chat_id=CHAT_TG, username="kipr6", title="Kipr", is_active=True)
        cases = Item(title="Чехлы Apple TechWoven", brand="Apple", model="TechWoven iPhone 17 Pro", status=ItemStatus.PUBLISHED,
                     price=Decimal(70), min_price=Decimal(60), quantity_total=2, first_published_at=datetime.now(UTC))
        lamp = Item(title="Лампа IKEA", status=ItemStatus.PUBLISHED, price=Decimal(20), min_price=Decimal(15),
                    quantity_total=1, first_published_at=datetime.now(UTC))
        s.add_all([chat, cases, lamp])
        await s.flush()
        p1 = Publication(item_id=cases.id, chat_id=chat.id, text="Чехлы\n\nНовые", language=Language.RU,
                         status=PublicationStatus.POSTED, tg_message_ids=[101, 102], text_message_id=101)
        p2 = Publication(item_id=lamp.id, chat_id=chat.id, text="Лампа\n\nТёплый свет", language=Language.RU,
                         status=PublicationStatus.POSTED, tg_message_ids=[201], text_message_id=201)
        s.add_all([p1, p2])
        await s.flush()
        return SimpleNamespace(chat_id=chat.id, cases=cases.id, lamp=lamp.id, p1=p1.id, p2=p2.id)


@pytest.fixture
def ai(monkeypatch, world):
    calls = []

    async def structured(*, purpose, output, content=None, **_):
        assert output is InquiryMatch
        calls.append(content)
        msg = content.split("Buyer's message(s):\n", 1)[1].lower()
        if "крипто" in msg:
            return InquiryMatch(sales_related=False, item_id=None, confidence=0.9, intent="other", price_offer_eur=None, summary_ru="спам")
        if "чехл" in msg:
            offer = 60 if "60" in msg else None
            return InquiryMatch(sales_related=True, item_id=world.cases, confidence=0.95,
                                intent="price_offer" if offer else "question", price_offer_eur=offer, summary_ru="спрашивает про чехлы")
        if "ламп" in msg:
            return InquiryMatch(sales_related=True, item_id=world.lamp, confidence=0.9, intent="question", price_offer_eur=None, summary_ru="лампа")
        if "earlier in this conversation" in content.lower():
            return InquiryMatch(sales_related=True, item_id=None, confidence=0.2, intent="other", price_offer_eur=None, summary_ru="")
        return InquiryMatch(sales_related=True, item_id=None, confidence=0.2, intent="question", price_offer_eur=None, summary_ru="непонятно")

    monkeypatch.setattr(inquiries, "structured", structured)
    return calls


class Notifier:
    def __init__(self):
        self.events = []

    async def inquiry_new(self, inquiry_id, is_new):
        self.events.append((inquiry_id, is_new))


class FakeUserbot:
    def __init__(self):
        self.edits, self.deletes = [], []

    async def get_input_entity(self, ref):
        return f"peer:{ref}"

    async def edit_message(self, peer, msg_id, text, parse_mode=None, link_preview=False):
        self.edits.append((peer, msg_id, text))

    async def delete_messages(self, peer, ids, revoke=True):
        self.deletes.append((peer, list(ids)))


BUYER = BuyerInfo(tg_user_id=555, username="anna_cy", display_name="Anna")
BUYER2 = BuyerInfo(tg_user_id=556, username=None, display_name="Petros")


async def test_reply_to_post(world, ai):
    pub = await inquiries.find_publication(CHAT_TG, 102)  # reply to the 2nd photo of the album
    assert pub == world.p1
    assert await inquiries.find_publication(CHAT_TG, 999) is None
    out = await inquiries.handle_reply(BUYER, CHAT_TG, Incoming(5001, "Сколько стоит?"), pub)
    assert out.is_new
    assert await inquiries.handle_reply(BUYER, CHAT_TG, Incoming(5001, "Сколько стоит?"), pub) is None  # dedupe
    async with session_scope() as s:
        inq = await s.get(Inquiry, out.inquiry_id)
        assert inq.item_id == world.cases and inq.source == InquirySource.REPLY
        assert (await s.get(Item, world.cases)).status == ItemStatus.HAS_INQUIRIES
    assert ai == []  # replies need no AI


async def test_dm_debounce_and_conversation(world, ai):
    n = Notifier()
    lst = InquiryListener(client=None, notifier=n, debounce=0.05, max_wait=0.2)
    lst.add_private(BUYER2, Incoming(10, "Здравствуйте"))
    lst.add_private(BUYER2, Incoming(11, "Лампа ещё продаётся?"))
    await asyncio.sleep(0.15)
    assert len(n.events) == 1 and n.events[0][1] is True
    assert "Здравствуйте\nЛампа ещё продаётся?" in ai[-1]  # one AI call for both messages
    lamp_inq = n.events[0][0]

    # same conversation, same item → no new notification
    assert await inquiries.handle_dm(BUYER2, [Incoming(12, "А можно завтра?")]) is None
    # same conversation, another item → new inquiry
    out = await inquiries.handle_dm(BUYER2, [Incoming(13, "И чехлы за 60 отдадите?")])
    assert out.is_new and out.inquiry_id != lamp_inq
    async with session_scope() as s:
        inq = await s.get(Inquiry, out.inquiry_id)
        assert inq.item_id == world.cases and inq.price_offer == Decimal(60) and inq.intent == "price_offer"
    # already processed message ids are skipped (restart scan)
    assert await inquiries.handle_dm(BUYER2, [Incoming(13, "И чехлы за 60 отдадите?")]) is None


async def test_unidentified_then_clarified_and_spam(world, ai):
    buyer = BuyerInfo(tg_user_id=777, username="mike", display_name="Mike")
    first = await inquiries.handle_dm(buyer, [Incoming(1, "Привет, а что по цене?")])
    async with session_scope() as s:
        inq = await s.get(Inquiry, first.inquiry_id)
        assert inq.item_id is None and inq.status == InquiryStatus.UNIDENTIFIED
    assert await inquiries.handle_dm(buyer, [Incoming(2, "ну то самое")]) is None  # still unclear, no spam to owner
    second = await inquiries.handle_dm(buyer, [Incoming(3, "я про лампу")])
    assert second.inquiry_id == first.inquiry_id and second.is_new is False
    async with session_scope() as s:
        inq = await s.get(Inquiry, first.inquiry_id)
        assert inq.item_id == world.lamp and inq.status == InquiryStatus.NEW

    spammer = BuyerInfo(tg_user_id=888, username="x", display_name="X")
    assert await inquiries.handle_dm(spammer, [Incoming(1, "Лучшие крипто сигналы")]) is None


async def test_cards_render(world):
    async with session_scope() as s:
        inq = await s.scalar(select(Inquiry).where(Inquiry.item_id == world.cases).order_by(Inquiry.id))
        item = await s.get(Item, world.cases)
        chat = await s.get(Chat, world.chat_id)
        from app.db.models import Buyer
        buyer = await s.get(Buyer, inq.buyer_id)
    card = texts.inquiry_card(inq, item, buyer, chat, 2, "ressell_1")
    assert "Отклик · #" in card and "минимум <b>€60</b>" in card and "https://t.me/kipr6/5001" in card
    assert "осталось 2 из 2" in card
    kb = keyboards.inquiry(inq, item, buyer, chat, [])
    datas = [getattr(b.type, "data", None) or getattr(b, "url", None) for row in kb for b in row]
    assert f"iq:res:{inq.id}".encode() in datas
    unknown = keyboards.inquiry(SimpleNamespace(id=9, status=InquiryStatus.UNIDENTIFIED, tg_message_id=1), None, None, None,
                                [SimpleNamespace(id=world.lamp, display_name="Лампа IKEA")])
    assert unknown[0][0].type.data == f"iq:set:9:{world.lamp}".encode()


async def test_reserve_sell_and_posts(world):
    bot = FakeUserbot()
    async with session_scope() as s:  # a queued (approved, not yet posted) version that must disappear on sell-out
        s.add(Publication(item_id=world.cases, chat_id=world.chat_id, text="x", language=Language.RU,
                          status=PublicationStatus.APPROVED, cycle_no=2))

    await sales.reserve(world.cases)
    async with session_scope() as s:
        assert (await s.get(Item, world.cases)).status == ItemStatus.RESERVED
    with pytest.raises(sales.SaleError):
        await sales.reserve(world.cases)
    assert await sales.unreserve(world.cases) == ItemStatus.HAS_INQUIRIES

    # one of two sold: back on sale, posts show the new quantity
    await sales.reserve(world.cases)
    res = await sales.sell(world.cases, Decimal(65))
    assert not res.sold_out and res.left == 1
    posts = await sales.refresh_posts(bot, world.cases)
    assert posts.done == 1 and bot.edits[0][1] == 101
    assert "В наличии" not in bot.edits[0][2] and "70 €" in bot.edits[0][2]  # 1 left → no quantity line
    async with session_scope() as s:
        item = await s.get(Item, world.cases)
        assert item.status == ItemStatus.HAS_INQUIRIES and item.quantity_sold == 1
        r = await s.scalar(select(Reservation).where(Reservation.item_id == world.cases).order_by(Reservation.id.desc()))
        assert r.status == ReservationStatus.CONVERTED

    # last unit: SOLD, posts deleted everywhere, queued versions dropped, inquiries closed
    res = await sales.sell(world.cases)
    assert res.sold_out and res.final_price == Decimal(70)
    posts = await sales.remove_posts(bot, world.cases)
    assert posts.done == 1 and bot.deletes == [(f"peer:{CHAT_TG}", [101, 102])]
    async with session_scope() as s:
        item = await s.get(Item, world.cases)
        assert item.status == ItemStatus.SOLD and item.sold_at is not None
        pubs = list(await s.scalars(select(Publication).where(Publication.item_id == world.cases)))
        assert [p.status for p in pubs] == [PublicationStatus.DELETED]
        assert all(i.status == InquiryStatus.HANDLED for i in await s.scalars(select(Inquiry).where(Inquiry.item_id == world.cases)))
        assert len(list(await s.scalars(select(Sale).where(Sale.item_id == world.cases)))) == 2
    with pytest.raises(sales.SaleError):
        await sales.sell(world.cases)

    # without a userbot nothing crashes, the owner gets told
    async with session_scope() as s:
        lamp = await s.get(Item, world.lamp)
    res = await sales.remove_posts(None, lamp.id)
    assert res.done == 0 and "нет сессии" in res.failed[0]


async def test_scan_unread(world, ai):
    me_msgs = [SimpleNamespace(id=31, message="Лампа ещё есть?", media=None, fwd_from=None, out=False),
               SimpleNamespace(id=30, message="Добрый день", media=None, fwd_from=None, out=False)]
    user = types.User(id=999, first_name="Olga", username="olga")
    bot_user = types.User(id=1000, first_name="Bot", bot=True)

    class Client:
        async def iter_dialogs(self, limit=None):
            yield SimpleNamespace(is_user=True, unread_count=2, entity=user)
            yield SimpleNamespace(is_user=True, unread_count=1, entity=bot_user)
            yield SimpleNamespace(is_user=False, unread_count=5, entity=None)

        async def get_messages(self, entity, limit=None):
            return me_msgs[:limit]

    n = Notifier()
    assert await InquiryListener(Client(), n).scan_unread() == 1
    assert len(n.events) == 1
    assert "Добрый день\nЛампа ещё есть?" in ai[-1]  # oldest first
    assert await InquiryListener(Client(), n).scan_unread() == 1 and len(n.events) == 1  # processed once only


async def test_bot_inquiry_notification(world):
    from app.bot.core import BotCore
    from tests.test_pipeline import FakeTg

    core = BotCore(get_settings())
    core.client = FakeTg()
    async with session_scope() as s:
        inq_id = await s.scalar(select(Inquiry.id).where(Inquiry.item_id == world.lamp).order_by(Inquiry.id))
    await core.inquiry_new(inq_id, True)
    msg_id, card, buttons = core.client.sent[-1]
    assert "Лампа IKEA" in card and buttons
    async with session_scope() as s:
        assert (await s.get(Inquiry, inq_id)).notify_message_id == msg_id
    await core.refresh_inquiry(inq_id)
    assert core.client.edits[-1][0] == msg_id


async def test_listener_events(world, ai):
    n = Notifier()
    lst = InquiryListener(client=None, notifier=n, debounce=0.01, max_wait=0.05)
    person = types.User(id=4242, first_name="Nick", username="nick")

    def ev(private, msg, chat_id=None, sender=person):
        async def get_sender():
            return sender
        return SimpleNamespace(is_private=private, is_group=not private, chat_id=chat_id, message=msg, get_sender=get_sender)

    reply = SimpleNamespace(id=7001, message="Свет тёплый?", media=None, fwd_from=None,
                            reply_to=SimpleNamespace(reply_to_msg_id=201))
    await lst.on_message(ev(False, reply, CHAT_TG))
    assert len(n.events) == 1
    other_reply = SimpleNamespace(id=7002, message="+", media=None, fwd_from=None, reply_to=SimpleNamespace(reply_to_msg_id=55))
    await lst.on_message(ev(False, other_reply, CHAT_TG))  # reply to someone else's message: ignored
    service = types.User(id=777000, first_name="Telegram")
    await lst.on_message(ev(True, SimpleNamespace(id=1, message="Login code: 12345", media=None, fwd_from=None, reply_to=None),
                            sender=service))
    photo_only = SimpleNamespace(id=2, message="", media=types.MessageMediaPhoto(), fwd_from=None, reply_to=None)
    await lst.on_message(ev(True, photo_only))
    await asyncio.sleep(0.1)
    assert "[фото]" in ai[-1]  # a photo-only DM still reaches matching, login codes never do
    assert all("Login code" not in c for c in ai)
