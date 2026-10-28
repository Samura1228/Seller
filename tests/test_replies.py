"""Suggested replies to buyers: the conversation and the minimum price reach the AI; the owner gets a ready text.

Run:  TEST_DATABASE_URL=postgresql://... pytest   (the database is wiped!)
"""

import os
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set")

from app.ai.schemas import ReplySuggestion  # noqa: E402
from app.bot import keyboards, texts  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.enums import InquirySource, ItemStatus, Language, TextPlatform  # noqa: E402
from app.db.models import Buyer, Inquiry, Item, ItemText  # noqa: E402
from app.db.session import _sessionmaker, session_scope  # noqa: E402
from app.services import replies  # noqa: E402


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
        item = Item(title="Колонка B&O Beosound Explore", status=ItemStatus.PUBLISHED, price=Decimal(185),
                    min_price=Decimal(160), new_price_ref=Decimal(249), quantity_total=1)
        item.texts = [ItemText(platform=TextPlatform.TELEGRAM_MASTER, language=Language.RU,
                               title="Колонка B&O, новая", body="В заводской плёнке")]
        buyer = Buyer(tg_user_id=555, username="artur_p", display_name="Артур")
        s.add_all([item, buyer])
        await s.flush()
        inq = Inquiry(item_id=item.id, buyer_id=buyer.id, source=InquirySource.DM, tg_chat_id=555, tg_message_id=1,
                      text="Здравствуйте предложу 150 за колонку?", price_offer=Decimal(150))
        unknown = Inquiry(buyer_id=buyer.id, source=InquirySource.DM, tg_chat_id=555, tg_message_id=3, text="Актуально?")
        s.add_all([inq, unknown])
        await s.flush()
        return SimpleNamespace(item=item.id, inq=inq.id, unknown=unknown.id)


class Userbot:
    async def get_messages(self, peer, limit=None):
        assert peer == 555
        return [SimpleNamespace(out=True, message="Здравствуйте! Готов предложить 175"),  # newest first
                SimpleNamespace(out=False, message="Здравствуйте предложу 150 за колонку?")]


async def test_suggestion_sees_conversation_and_minimum(world, monkeypatch):
    seen = []

    async def structured(*, purpose, output, content, **_):
        assert output is ReplySuggestion and purpose == "reply_suggest"
        seen.append(content)
        price = 155 if len(seen) == 2 else 168
        return ReplySuggestion(reply=f"Добрый день! Колонка новая, в плёнке. Могу отдать за {price}. Когда удобно встретиться?",
                               tip_ru="Небольшая уступка с причиной и вопрос о встрече", price_named_eur=price)

    monkeypatch.setattr(replies, "structured", structured)
    sug = await replies.suggest(world.inq, Userbot())
    content = seen[0]
    assert content.index("Buyer: Здравствуйте предложу 150") < content.index("Seller: Здравствуйте! Готов предложить 175")
    assert "MINIMUM (secret): €160" in content and "new retail price: €249" in content
    assert "В заводской плёнке" in content and not sug.below_min

    again = await replies.suggest(world.inq, Userbot(), previous=sug.text)
    assert "[Earlier suggestion — write a different one]\n" + sug.text in seen[1]
    assert again.below_min  # 155 < 160: the owner is warned

    async with session_scope() as s:
        item, buyer = await s.get(Item, world.item), await s.get(Buyer, 1)
    card = texts.reply_suggestion(item, buyer, again)
    assert "<code>Добрый день!" in card and "ниже твоего минимума" in card
    assert keyboards.reply_suggestion(world.inq, buyer)[1][0].type.url == "https://t.me/artur_p"

    # Without the userbot (or for an unidentified item) it still works from the inquiry itself.
    await replies.suggest(world.unknown, None)
    assert "Buyer: Актуально?" in seen[-1] and "ask the buyer which item" in seen[-1]


async def test_inquiry_card_has_reply_button(world):
    async with session_scope() as s:
        inq, item = await s.get(Inquiry, world.inq), await s.get(Item, world.item)
        buyer = await s.get(Buyer, inq.buyer_id)
        unknown = await s.get(Inquiry, world.unknown)
    labels = [b.text for row in keyboards.inquiry(inq, item, buyer, None, []) for b in row]
    assert labels[0] == "💬 Предложить ответ"
    assert "💬 Предложить ответ" in [b.text for row in keyboards.inquiry(unknown, None, buyer, None, [item]) for b in row]
