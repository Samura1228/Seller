"""/agent: tool loop over the system's data, photos in the request, proposals applied only via apply().

Run:  TEST_DATABASE_URL=postgresql://... pytest   (the database is wiped!)
"""

import io
import json
import os
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from PIL import Image
from sqlalchemy import text

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set")

from anthropic.types import TextBlock, ToolUseBlock  # noqa: E402

from app.db.base import Base  # noqa: E402
from app.db.enums import ItemStatus  # noqa: E402
from app.db.models import Buyer, Inquiry, Item  # noqa: E402
from app.db.session import _sessionmaker, session_scope  # noqa: E402
from app.services import agent as agent_mod  # noqa: E402
from app.services import agent_tools  # noqa: E402


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
        speaker = Item(title="Колонка B&O Beosound Explore", status=ItemStatus.PUBLISHED, price=Decimal(185),
                       min_price=Decimal(160), quantity_total=1, first_published_at=datetime.now(UTC))
        buyer = Buyer(tg_user_id=555, username="Arturpod305", display_name="Артур")
        s.add_all([speaker, buyer])
        await s.flush()
        s.add(Inquiry(item_id=speaker.id, buyer_id=buyer.id, source="dm", text="предложу 150", price_offer=Decimal(150)))
        await s.flush()
        return SimpleNamespace(speaker=speaker.id)


class FakeClaude:
    """Scripted replies; records every request."""

    def __init__(self, script):
        self.script, self.requests = list(script), []
        self.messages = self

    async def create(self, **kw):
        self.requests.append(json.loads(json.dumps(kw, default=str)))
        blocks, stop = self.script.pop(0)
        return SimpleNamespace(content=blocks, stop_reason=stop,
                               usage=SimpleNamespace(input_tokens=100, output_tokens=20, server_tool_use=None))


def _jpeg() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (3000, 2000), (200, 30, 30)).save(out, "JPEG")
    return out.getvalue()


async def test_agent_reads_data_and_proposes(world, monkeypatch):
    fake = FakeClaude([
        ([TextBlock(type="text", text="Смотрю вещи…"),
          ToolUseBlock(type="tool_use", id="t1", name="list_items", input={"query": "B&O"})], "tool_use"),
        ([ToolUseBlock(type="tool_use", id="t2", name="list_inquiries", input={"item_id": world.speaker}),
          ToolUseBlock(type="tool_use", id="t3", name="propose_change",
                       input={"action": "set_price", "item_id": world.speaker, "price": 175, "min_price": 160,
                              "summary_ru": "Снизить до €175: 2 отклика, оба торгуются"})], "tool_use"),
        ([TextBlock(type="text", text="Предлагаю €175 — подтверди кнопкой ниже.")], "end_turn"),
    ])
    monkeypatch.setattr(agent_mod, "_client", lambda: fake)
    session = agent_mod.AgentSession(userbot=None)
    reply = await session.ask("Почему колонку не берут? Вот фото", [_jpeg()])

    assert reply.text == "Предлагаю €175 — подтверди кнопкой ниже."
    assert [p.action for p in reply.proposals] == ["set_price"] and reply.proposals[0].price == Decimal(175)
    first = fake.requests[0]
    assert {t["name"] for t in first["tools"]} >= {"list_items", "propose_change", "web_search"}
    user = first["messages"][0]["content"]
    assert user[0]["type"] == "image" and user[-1]["text"].startswith("Почему колонку")
    tool_results = fake.requests[1]["messages"][-1]["content"]
    assert tool_results[0]["tool_use_id"] == "t1" and "Beosound" in tool_results[0]["content"]
    last = fake.requests[2]["messages"][-1]["content"]
    assert "предложу 150" in last[0]["content"] and "NOT applied" in last[1]["content"]
    async with session_scope() as s:  # nothing changed by the agent itself
        assert (await s.get(Item, world.speaker)).price == Decimal(185)

    # Old images are dropped from later requests to keep them small.
    for _ in range(4):
        fake.script.append(([TextBlock(type="text", text="ок")], "end_turn"))
        await session.ask("ещё фото", [_jpeg()])
    kinds = [b["type"] for m in fake.requests[-1]["messages"] if isinstance(m["content"], list) for b in m["content"]]
    assert kinds.count("image") == agent_mod.KEEP_IMAGES


async def test_photos_tool_and_errors(world):
    out = await agent_tools.run_tool("get_item_photos", {"item_id": world.speaker}, agent_tools.ToolContext())
    assert out == "no photos on disk"
    assert (await agent_tools.run_tool("get_item", {"item_id": 999}, agent_tools.ToolContext())) == "no such item"
    assert "error:" in await agent_tools.run_tool("get_item", {}, agent_tools.ToolContext())
    stats = json.loads(await agent_tools.run_tool("stats", {}, agent_tools.ToolContext()))
    assert stats["inquiries"] == 1 and stats["items_by_status"] == {"published": 1}


async def test_apply_proposals(world):
    from app.bot.handlers.agent import apply, proposal_text

    core = SimpleNamespace(userbot=None)
    p = agent_tools.Proposal(action="set_price", item_id=world.speaker, summary_ru="дешевле", price=Decimal(175),
                             min_price=Decimal(160))
    async with session_scope() as s:
        item = await s.get(Item, world.speaker)
    assert "€185 → <b>€175</b>, минимум €160" in proposal_text(p, item)
    assert (await apply(core, p)).startswith("✅ Цена €175, минимум €160")
    assert await apply(core, agent_tools.Proposal(action="reserve", item_id=world.speaker, summary_ru="")) == "✅ Забронировано"
    async with session_scope() as s:
        item = await s.get(Item, world.speaker)
        assert item.price == Decimal(175) and item.status == ItemStatus.RESERVED
    assert await apply(core, agent_tools.Proposal(action="unreserve", item_id=world.speaker, summary_ru="")) == "✅ Бронь снята"
