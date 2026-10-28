"""Marketplace queue and the helper's API against Postgres (AI mocked).

Run:  TEST_DATABASE_URL=postgresql://... pytest   (the database is wiped!)
"""

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set")

import httpx  # noqa: E402

from app.ai.schemas import FormPlan, MarketplaceText  # noqa: E402
from app.bot import keyboards, texts  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.enums import ExternalPlatform, ExternalStatus, ItemStatus, Language, TextPlatform, TextStatus  # noqa: E402
from app.db.models import ExternalListing, Item, ItemPhoto, ItemText  # noqa: E402
from app.db.session import _sessionmaker, session_scope  # noqa: E402
from app.services import marketplaces, settings_store  # noqa: E402

NOON = datetime(2026, 10, 28, 10, 0, tzinfo=UTC)  # 12:00 in Cyprus: inside the posting window


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
        speaker = Item(title="Колонка B&O", brand="Bang & Olufsen", model="Beosound Explore", status=ItemStatus.READY,
                       price=Decimal(185), min_price=Decimal(160), quantity_total=1)
        speaker.texts = [ItemText(platform=TextPlatform.TELEGRAM_MASTER, language=lang, title="B&O", body="Новая",
                                  status=TextStatus.APPROVED) for lang in (Language.RU, Language.EN)]
        speaker.photos = [ItemPhoto(path="photos/1.jpg", thumb_path="t.jpg", width=10, height=10, sha256="a" * 64)]
        lamp = Item(title="Лампа", status=ItemStatus.READY, price=Decimal(20), min_price=Decimal(15), quantity_total=1)
        draft = Item(title="Черновик", status=ItemStatus.PENDING_APPROVAL, price=Decimal(5), quantity_total=1)
        s.add_all([speaker, lamp, draft])
        await s.flush()
        return SimpleNamespace(speaker=speaker.id, lamp=lamp.id, draft=draft.id)


@pytest.fixture
def ai(monkeypatch):
    calls = []

    async def structured(*, purpose, output, content, **_):
        calls.append((purpose, content))
        if output is MarketplaceText:
            return MarketplaceText(title="✅ B&O Beosound Explore, sealed", body="Portable speaker, IP67.",
                                   category_path=["Electronics, appliances", "Audio, stereo"], condition="new")
        assert output is FormPlan
        return FormPlan(state="ready", actions=[], publish_element=5, ad_url=None, note_ru="готово")

    monkeypatch.setattr(marketplaces, "structured", structured)
    return calls


@pytest.fixture
def daytime(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "helper_token", "secret")
    return settings


async def _status(listing_id):
    async with session_scope() as s:
        return (await s.get(ExternalListing, listing_id)).status


async def test_queue_publish_flow(world, ai, daytime):
    assert await marketplaces.enqueue_all() == 6  # 2 approved items x 3 platforms; the draft isn't queued
    assert await marketplaces.enqueue(world.speaker) == 0

    assert await marketplaces.claim(["bazaraki"], datetime(2026, 10, 28, 20, 0, tzinfo=UTC)) is None  # 22:00 Cyprus
    task = await marketplaces.claim(["bazaraki"], NOON)
    assert task.kind == "publish" and task.item_id == world.speaker  # the more expensive item first
    assert task.title == "B&O Beosound Explore, sealed" and task.price == 185 and task.language == "en"
    assert task.body.endswith("Message me on Telegram: @limassol_seller") and len(task.photo_ids) == 1
    assert task.category_path == ["Electronics, appliances", "Audio, stereo"]
    assert await marketplaces.claim(["bazaraki"], NOON) is None  # one ad in flight per platform

    await marketplaces.filled(task.listing_id, None)
    assert await marketplaces.decision(task.listing_id) == "pending"
    await marketplaces.decide(task.listing_id, publish=True)
    assert await marketplaces.decision(task.listing_id) == "publish"
    await marketplaces.published(task.listing_id, "https://www.bazaraki.com/adv/1_bo/", NOON)
    assert await _status(task.listing_id) == ExternalStatus.PUBLISHED
    assert await marketplaces.claim(["bazaraki"], NOON + timedelta(minutes=10)) is None  # 30–90 min gap
    later = await marketplaces.claim(["bazaraki"], NOON + timedelta(minutes=95))
    assert later.item_id == world.lamp

    # The owner didn't answer: the form is closed and retried in 2 h.
    await marketplaces.filled(later.listing_id, None)
    await marketplaces.no_answer(later.listing_id, NOON + timedelta(minutes=100))
    await settings_store.put_time(marketplaces.k_next(ExternalPlatform.BAZARAKI), None)
    assert await marketplaces.claim(["bazaraki"], NOON + timedelta(minutes=110)) is None
    again = await marketplaces.claim(["bazaraki"], NOON + timedelta(hours=4))
    assert again.listing_id == later.listing_id

    # Failures: retried, then given up after 3 attempts.
    for _ in range(3):
        ext = await marketplaces.failed(again.listing_id, "selector boom", NOON)
    assert ext.status == ExternalStatus.FAILED
    await marketplaces.retry(again.listing_id)
    assert await _status(again.listing_id) == ExternalStatus.WAITING

    # Disabled platform gives nothing; the Kyma text is Russian with a Russian footer.
    await settings_store.put(marketplaces.k_enabled(ExternalPlatform.FACEBOOK), False)
    assert await marketplaces.claim(["facebook"], NOON) is None
    kyma = await marketplaces.claim(["kyma"], NOON)
    assert kyma.language == "ru" and "Пишите в Telegram: @limassol_seller" in kyma.body

    # A task the helper took and never reported back returns to the queue after an hour.
    await marketplaces._release_stale(NOON + timedelta(hours=2))
    assert await _status(kyma.listing_id) == ExternalStatus.WAITING


async def test_sold_item_is_removed_by_helper(world, ai, daytime):
    from app.services import sales

    async with session_scope() as s:
        bz = await s.scalar(select(ExternalListing).where(ExternalListing.item_id == world.speaker,
                                                         ExternalListing.platform == ExternalPlatform.BAZARAKI))
        bz_id = bz.id
    res = await sales.remove_posts(None, world.speaker)
    assert res.bazaraki_to_remove == ["Bazaraki"]
    assert await _status(bz_id) == ExternalStatus.REMOVAL_NEEDED
    async with session_scope() as s:  # unpublished ads of the sold item are cancelled
        others = list(await s.scalars(select(ExternalListing.status).where(
            ExternalListing.item_id == world.speaker, ExternalListing.id != bz_id)))
    assert set(others) == {ExternalStatus.CANCELLED}

    night = datetime(2026, 10, 28, 21, 30, tzinfo=UTC)  # removals don't wait for the posting window
    task = await marketplaces.claim(["bazaraki"], night)
    assert task.kind == "remove" and task.url == "https://www.bazaraki.com/adv/1_bo/"
    await marketplaces.removed(task.listing_id)
    assert await _status(bz_id) == ExternalStatus.REMOVED


async def test_helper_api(world, ai, daytime, monkeypatch):
    from app.api import helper as helper_api
    from app.api.main import api

    events = []

    class Notifier:
        async def marketplace_filled(self, listing_id, path, note):
            events.append(("filled", listing_id, bool(path), note))

        async def marketplace_published(self, listing_id):
            events.append(("published", listing_id))

        async def marketplace_failed(self, listing_id, error, final):
            events.append(("failed", listing_id, final))

        async def marketplace_removed(self, listing_id):
            events.append(("removed", listing_id))

    helper_api.set_notifier(Notifier())
    monkeypatch.setattr(marketplaces, "datetime", _FrozenDatetime)
    transport = httpx.ASGITransport(app=api)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        assert (await c.post("/api/helper/next", json={"platforms": []})).status_code == 401
        auth = {"Authorization": "Bearer secret"}
        r = await c.post("/api/helper/next", json={"platforms": ["bazaraki"]}, headers=auth)
        task = r.json()["task"]
        assert task["kind"] == "publish" and task["item_id"] == world.lamp
        lid = task["listing_id"]
        plan = (await c.post(f"/api/helper/tasks/{lid}/plan", headers=auth,
                             json={"goal": "publish", "snapshot": {"url": "x", "elements": []}, "history": []})).json()
        assert plan["state"] == "ready" and plan["publish_element"] == 5
        assert '"price_eur": 20' in ai[-1][1] and "Goal: publish" in ai[-1][1]
        r = await c.post(f"/api/helper/tasks/{lid}/filled", headers=auth | {"Content-Type": "image/png"},
                         content=b"\x89PNGfake", params={"note": "всё заполнено"})
        assert r.status_code == 200 and events[-1] == ("filled", lid, True, "всё заполнено")
        assert (await c.get(f"/api/helper/tasks/{lid}/decision", headers=auth)).json() == {"decision": "pending"}
        await marketplaces.decide(lid, publish=True)
        assert (await c.get(f"/api/helper/tasks/{lid}/decision", headers=auth)).json() == {"decision": "publish"}
        await c.post(f"/api/helper/tasks/{lid}/published", headers=auth, json={"url": "https://bz/adv/2"})
        assert events[-1] == ("published", lid) and await _status(lid) == ExternalStatus.PUBLISHED

    rows, seen = await marketplaces.status()
    assert seen is not None
    body = texts.platforms_status(rows, seen, get_settings())
    assert "помощник на связи" in body and "Bazaraki" in body
    assert keyboards.platforms(rows)[-1][0].text.startswith("📥")


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOON + timedelta(hours=6)  # 18:00 in Cyprus, past every gap set above
