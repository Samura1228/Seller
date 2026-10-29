"""End-to-end stage-1 flow against a real Postgres with the AI and Apify mocked.

Run:  TEST_DATABASE_URL=postgresql://... pytest   (the database is wiped!)
"""

import io
import os
import zipfile
from decimal import Decimal
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import func, select, text

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set")

from app.ai.schemas import ListingText, MasterTexts, PriceProposal, Question, Recognition, Spec  # noqa: E402
from app.bot import texts  # noqa: E402
from app.bot.handlers.review import parse_price  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.enums import ApprovalStatus, Condition, ItemStatus, TextStatus  # noqa: E402
from app.db.models import Approval, Item, ItemEvent, ItemPhoto, PriceHistory  # noqa: E402
from app.db.session import _sessionmaker, session_scope  # noqa: E402
from app.services import copywriting, intake, pipeline, pricing, recognition  # noqa: E402


class FakeNotifier:
    def __init__(self):
        self.ready, self.updated, self.failed = [], [], []

    async def item_ready(self, item_id):
        self.ready.append(item_id)

    async def item_updated(self, item_id):
        self.updated.append(item_id)

    async def item_failed(self, item_id, error):
        self.failed.append((item_id, error))


def _jpeg(color) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), color).save(buf, "JPEG")
    return buf.getvalue()


def make_zip(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("stuff/sony headphones/1.jpg", _jpeg((10, 10, 10)))
        zf.writestr("stuff/sony headphones/2.jpg", _jpeg((20, 20, 20)))
        zf.writestr("stuff/кресло/1.jpg", _jpeg((200, 100, 50)))
    return path


@pytest.fixture(scope="module", autouse=True)
async def fresh_db():
    engine = _sessionmaker().kw["bind"]
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
        await conn.run_sync(Base.metadata.create_all)
    yield


@pytest.fixture(autouse=True)
def mock_ai(monkeypatch):
    calls = {"fail_recognize": False}

    async def structured(*, purpose, output, item_id=None, content=None, **_):
        if output is Recognition:
            if calls["fail_recognize"]:
                raise RuntimeError("boom")
            corrected = purpose == "correct"
            return Recognition(
                title_ru="Наушники Sony WH-1000XM5" if corrected else "Наушники Sony WH-1000XM4",
                search_query="sony wh-1000xm4", category="electronics/headphones", brand="Sony",
                model="WH-1000XM5" if corrected else "WH-1000XM4", specs=[Spec(name="цвет", value="чёрный")],
                condition=Condition.LIKE_NEW, condition_notes_ru="", included=["кейс", "кабель"],
                quantity=2 if corrected else 1, quantity_confident=corrected, confidence=0.7,
                questions=[] if corrected else [Question(field="model", question_ru="XM4 или XM5?")],
            )
        if output is PriceProposal:
            return PriceProposal(new_price_eur=349, new_price_source_url="https://sony.com", relevant_comp_indexes=[0, 1, 9],
                                 comps_low_eur=150, comps_high_eur=180, price_eur=170, min_price_eur=150,
                                 rationale_ru="Новая: €349, на Bazaraki: €150–180 → €170, минимум €150")
        if output is MasterTexts:
            return MasterTexts(ru=ListingText(title="Sony WH-1000XM4 — как новые", body="✅ Шумодав\n✅ 30 ч"),
                               en=ListingText(title="Sony WH-1000XM4 — like new", body="✅ ANC\n✅ 30 h"))
        raise AssertionError(output)

    async def web_research(**_):
        return "Sony WH-1000XM4 new: €349 https://sony.com"

    async def comps(query):
        return [{"source": "bazaraki", "title": "Sony XM4", "price": 150.0, "url": "u1"},
                {"source": "bazaraki", "title": "Sony WH1000XM4 black", "price": 180.0, "url": "u2"}]

    monkeypatch.setattr(recognition, "structured", structured)
    monkeypatch.setattr(pricing, "structured", structured)
    monkeypatch.setattr(pricing, "web_research", web_research)
    monkeypatch.setattr(copywriting, "structured", structured)
    monkeypatch.setattr(pricing.apify_bazaraki, "search", comps)
    return calls


async def test_full_stage1_flow(tmp_path, mock_ai):
    settings = get_settings()
    zip_path = make_zip(tmp_path / "items.zip")

    async with session_scope() as s:
        from app.db.models import IntakeBatch
        batch = IntakeBatch(file_name="items.zip")
        s.add(batch)
        await s.flush()
        batch_id = batch.id

    ids, report = await intake.import_zip(batch_id, zip_path, settings)
    assert len(ids) == 2 and report["duplicates"] == []

    # one item fails recognition, then succeeds on retry
    notifier = FakeNotifier()
    mock_ai["fail_recognize"] = True
    done, failed = await pipeline.run_batch(ids[:1], notifier, 2)
    assert failed == 1 and notifier.failed[0][0] == ids[0]
    mock_ai["fail_recognize"] = False
    done, failed = await pipeline.run_batch(ids, notifier, 2)
    assert (done, failed) == (2, 0)
    assert sorted(notifier.ready) == sorted(ids)

    hp = ids[0]
    async with session_scope() as s:
        item = await recognition.load_item(s, hp)
        assert item.status == ItemStatus.PENDING_APPROVAL and item.pipeline_error is None
        assert item.price == Decimal(170) and item.min_price == Decimal(150)
        assert {q["field"] for q in item.uncertain_fields} == {"model", "quantity"}
        assert len(item.texts) == 2 and len(item.photos) == 2
        research = await pricing.latest_research(s, hp)
        assert len(research.comps) == 2  # out-of-range index 9 ignored
        card = texts.review_card(item, research, settings.seller_username)
        assert "€170" in card and "Bazaraki €150–180 (2 объявл.)" in card
        assert "Пишите: @limassol_seller" in card and "Message: @limassol_seller" in card
        assert len(card) < 4000

    # owner actions
    await pipeline.set_manual_price(hp, Decimal(200), None)
    await pipeline.set_quantity(hp, 2)
    async with session_scope() as s:
        item = await recognition.load_item(s, hp)
        assert item.min_price == Decimal(176)  # keeps the 150/170 ratio
        assert all(q["field"] != "quantity" for q in item.uncertain_fields)
        assert "\nВ наличии: 2 шт." in texts.review_card(item, None, settings.seller_username)

    await pipeline.correct(hp, "это XM5", notifier)
    async with session_scope() as s:
        item = await recognition.load_item(s, hp)
        assert item.model == "WH-1000XM5" and item.my_notes == "это XM5"
        assert item.quantity_total == 2 and item.uncertain_fields == []

    await pipeline.approve(hp)
    await pipeline.withdraw(ids[1])
    async with session_scope() as s:
        item = await recognition.load_item(s, hp)
        assert item.status == ItemStatus.READY and item.approved_at is not None
        assert all(t.status == TextStatus.APPROVED for t in item.texts)
        statuses = list(await s.scalars(select(Approval.status).order_by(Approval.item_id)))
        assert statuses == [ApprovalStatus.APPROVED, ApprovalStatus.REJECTED]
        events = list(await s.scalars(select(ItemEvent.to_status).where(ItemEvent.item_id == hp).order_by(ItemEvent.id)))
        assert events == [ItemStatus.NEW, ItemStatus.RECOGNIZED, ItemStatus.PENDING_APPROVAL, ItemStatus.READY]
        assert await s.scalar(select(func.count()).select_from(PriceHistory).where(PriceHistory.item_id == hp)) >= 3

    # re-sending the same archive skips the active item, but re-imports the withdrawn one
    ids2, report2 = await intake.import_zip(batch_id, zip_path, settings)
    assert len(ids2) == 1 and [d["folder"] for d in report2["duplicates"]] == ["sony headphones"]
    async with session_scope() as s:
        assert await s.scalar(select(func.count()).select_from(ItemPhoto)) == 4
        assert await s.scalar(select(func.count()).select_from(Item)) == 3

    # process_item on an approved item is a no-op (no new approval)
    assert await pipeline.process_item(hp, notifier)
    async with session_scope() as s:
        assert await s.scalar(select(func.count()).select_from(Approval).where(Approval.item_id == hp)) == 1


async def test_failed_price_sources_are_shown_on_card(monkeypatch, mock_ai):
    """Web search / Bazaraki failures don't block pricing, but the card says the price was made without them."""
    settings = get_settings()

    async def broken_web(**_):
        raise RuntimeError("Country code CY is not supported.")

    async def broken_comps(query):
        raise pricing.apify_bazaraki.ComparablesError("APIFY_TOKEN is not set")

    monkeypatch.setattr(pricing, "web_research", broken_web)
    monkeypatch.setattr(pricing.apify_bazaraki, "search", broken_comps)
    async with session_scope() as s:
        item_id = await s.scalar(select(Item.id).where(Item.status == ItemStatus.READY).limit(1))
    await pipeline.reprice(item_id, FakeNotifier())
    async with session_scope() as s:
        item = await recognition.load_item(s, item_id)
        research = await pricing.latest_research(s, item_id)
        assert research.warnings == ["аналоги с Bazaraki (APIFY_TOKEN is not set)",
                                     "поиск в вебе (RuntimeError: Country code CY is not supported.)"]
        card = texts.review_card(item, research, settings.seller_username)
    assert "⚠️ Цена посчитана без: аналоги с Bazaraki" in card and "Пересчитать цену" in card


def test_parse_price():
    assert parse_price("120") == (Decimal(120), None)
    assert parse_price("120/100") == (Decimal(120), Decimal(100))
    assert parse_price("€120 / €95") == (Decimal(120), Decimal(95))
    assert parse_price("12,5") == (Decimal("12.5"), None)
    assert parse_price("120 100") == (Decimal(120), Decimal(100))
    assert parse_price("abc") is None
    assert parse_price("0") is None


class FakeTg:
    """Records what the bot would send to Telegram."""

    def __init__(self):
        self.sent, self.edits, self.albums = [], [], []
        self._id = 100

    async def send_message(self, entity, text, **kw):
        self._id += 1
        self.sent.append((self._id, text, kw.get("buttons")))
        return type("Msg", (), {"id": self._id})()

    async def send_file(self, entity, files, **kw):
        self.albums.append(files)

    async def edit_message(self, entity, msg_id, text=None, **kw):
        self.edits.append((msg_id, text, kw.get("buttons")))


async def test_bot_queue_shows_one_card_at_a_time(tmp_path):
    from app.bot.core import BotCore

    settings = get_settings()
    zip_path = tmp_path / "more.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("lamp/1.jpg", _jpeg((1, 2, 3)))
        zf.writestr("chair/1.jpg", _jpeg((4, 5, 6)))
    ids, _ = await intake.import_zip(None, zip_path, settings)
    assert len(ids) == 2

    core = BotCore(settings)
    core.client = FakeTg()
    await pipeline.run_batch(ids, core, 1)

    cards = [m for m in core.client.sent if m[2]]
    assert len(cards) == 1, "only one card is shown while it is unresolved"
    first_item = int(cards[0][1].split("#")[1].split(" ")[0])
    assert len(core.client.albums) == 1

    await core.show_next()  # still one displayed -> nothing new
    assert len([m for m in core.client.sent if m[2]]) == 1

    await pipeline.approve(first_item)
    await core.show_next()
    cards = [m for m in core.client.sent if m[2]]
    assert len(cards) == 2 and f"#{first_item} " not in cards[1][1]

    # skip the second -> queue has only a skipped one, which comes back on /next
    second_item = [i for i in ids if i != first_item][0]
    async with session_scope() as s:
        a = await s.scalar(select(Approval).where(Approval.item_id == second_item))
        a.status, a.bot_message_id = ApprovalStatus.SKIPPED, None
    await core.show_next(force=True)
    cards = [m for m in core.client.sent if m[2]]
    assert len(cards) == 3 and f"#{second_item} " in cards[2][1]
    assert await core.queue_size() == 1

    await pipeline.approve(second_item)
    await core.show_next(force=True)
    assert core.client.sent[-1][1] == texts.QUEUE_EMPTY


async def test_new_price_never_from_memory(monkeypatch, mock_ai):
    """A 'new price' the web search didn't return (or a failed search) is dropped, whatever the model says."""
    from app.ai.schemas import PriceProposal

    async def proposal(**_):
        return PriceProposal(new_price_eur=229, new_price_source_url="https://www.bang-olufsen.com/de/de/x",
                             relevant_comp_indexes=[], comps_low_eur=None, comps_high_eur=None, price_eur=185,
                             min_price_eur=160, rationale_ru="Новая: €229 → €185")

    async def notes_without_it(**_):
        return "Found only: Amazon.de €239 https://amazon.de/dp/1"

    async def notes_with_it(**_):
        return "Official: €249 https://bang-olufsen.com/de/de/speakers/beosound-explore"

    monkeypatch.setattr(pricing, "structured", proposal)
    async with session_scope() as s:
        item_id = await s.scalar(select(Item.id).limit(1))
    for notes, expected in ((notes_without_it, None), (notes_with_it, Decimal(229))):
        monkeypatch.setattr(pricing, "web_research", notes)
        await pipeline.reprice(item_id, FakeNotifier())
        async with session_scope() as s:
            assert (await pricing.latest_research(s, item_id)).new_price == expected
