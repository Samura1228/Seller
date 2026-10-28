"""Google Sheet export: rows for «Отклики» and «Покупатели», pushed to the Apps Script web app (HTTP mocked).

Run:  TEST_DATABASE_URL=postgresql://... pytest   (the database is wiped!)
"""

import json
import os
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set")

import httpx  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.enums import InquirySource, ItemStatus  # noqa: E402
from app.db.models import Buyer, Inquiry, Item, Sale  # noqa: E402
from app.db.session import _sessionmaker, session_scope  # noqa: E402
from app.services import sheets  # noqa: E402


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
        speaker = Item(title="Колонка B&O", status=ItemStatus.PUBLISHED, price=Decimal(185), min_price=Decimal(160),
                       quantity_total=1)
        cases = Item(title="Чехлы Apple", status=ItemStatus.SOLD, price=Decimal(70), min_price=Decimal(60),
                     quantity_total=1, quantity_sold=1)
        artur = Buyer(tg_user_id=8541015082, username="Arturpod305", display_name="Артур")
        s.add_all([speaker, cases, artur])
        await s.flush()
        i1 = Inquiry(item_id=speaker.id, buyer_id=artur.id, source=InquirySource.DM, text="предложу 150",
                     intent="price_offer", summary="торгуется", price_offer=Decimal(150))
        i2 = Inquiry(item_id=cases.id, buyer_id=artur.id, source=InquirySource.DM, text="чехлы есть?", intent="question")
        s.add_all([i1, i2, Sale(item_id=cases.id, buyer_id=artur.id, final_price=Decimal(65))])
        await s.flush()
        return SimpleNamespace(i1=i1.id, artur=artur.id, speaker=speaker.id, cases=cases.id)


@pytest.fixture
def google(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "sheets_webhook_url", "https://script.google.com/macros/s/abc/exec")
    monkeypatch.setattr(settings, "sheets_secret", "s3cret")
    posts = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":  # Apps Script runs the script, then redirects to the result
            posts.append(json.loads(request.content))
            return httpx.Response(302, headers={"Location": "https://script.googleusercontent.com/echo?x=1"})
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(sheets, "_http", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                                                   follow_redirects=True))
    return posts


async def test_push_inquiry_rows(world, google):
    await sheets.push_inquiry(world.i1)
    sent = google[-1]
    assert sent["secret"] == "s3cret"
    row = sent["inquiries"][0]
    assert row["ID отклика"] == world.i1 and row["Вещь"] == f"#{world.speaker} Колонка B&O"
    assert row["Предложил"] == "€150" and row["Минимум"] == "€160" and row["Намерение"] == "торг"
    assert row["Username"] == "@Arturpod305" and row["Telegram ID"] == "'8541015082" and row["Где"] == "личка"
    buyer = sent["buyers"][0]
    assert buyer["Откликов"] == 2 and buyer["Ссылка на чат"] == "https://t.me/Arturpod305"
    assert buyer["Интересовался"] == f"#{world.speaker} Колонка B&O; #{world.cases} Чехлы Apple"
    assert buyer["Купил"] == f"#{world.cases} Чехлы Apple за €65"


async def test_sync_all_and_failures(world, google, monkeypatch):
    assert await sheets.sync_all() == (2, 1)

    def broken(request):
        return httpx.Response(200, json={"ok": False, "error": "bad secret"})

    monkeypatch.setattr(sheets, "_http", lambda: httpx.AsyncClient(transport=httpx.MockTransport(broken)))
    with pytest.raises(RuntimeError, match="bad secret"):
        await sheets.sync_all()
    await sheets.push_inquiry(world.i1)  # never raises: inquiries keep flowing even if the sheet is broken


async def test_disabled_does_nothing(world, monkeypatch):
    monkeypatch.setattr(get_settings(), "sheets_webhook_url", None)
    assert not sheets.enabled()
    await sheets.push_inquiry(world.i1)
