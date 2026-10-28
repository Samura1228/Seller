"""Text edits: the edit prompt (not the write-from-scratch one) is used, and earlier edits are remembered."""

from app.ai import prompts
from app.ai.schemas import MasterTexts
from app.db.enums import Language, TextPlatform
from app.db.models import Item, ItemText
from app.services import copywriting


def _item() -> Item:
    item = Item(id=1, title="Наушники Sony", included=["коробка"], text_rules=[])
    item.texts = [
        ItemText(platform=TextPlatform.TELEGRAM_MASTER, language=Language.RU, title="Sony", body="✅ Отличный звук\nКоробка есть", version=1),
        ItemText(platform=TextPlatform.TELEGRAM_MASTER, language=Language.EN, title="Sony", body="✅ Great sound\nBox included", version=1),
    ]
    return item


def _fake(calls, ru_body, en_body):
    async def structured(*, purpose, output, system, content, **_):
        assert output is MasterTexts
        calls.append({"purpose": purpose, "system": system, "content": content})
        return MasterTexts.model_validate({"ru": {"title": "Sony", "body": ru_body}, "en": {"title": "Sony", "body": en_body}})
    return structured


async def test_edits_use_edit_prompt_and_accumulate(monkeypatch):
    item, calls = _item(), []
    monkeypatch.setattr(copywriting, "structured", _fake(calls, "Отличный звук\nКоробка есть", "Great sound\nBox included"))

    await copywriting.write_master(None, item, "убери эмодзи")
    assert calls[0]["purpose"] == "listing_edit" and calls[0]["system"] == prompts.load("listing_edit")
    assert "2–4 per version" not in calls[0]["system"]  # the write-from-scratch style rules don't apply to edits
    assert item.text_rules == ["убери эмодзи"]

    monkeypatch.setattr(copywriting, "structured", _fake(calls, "Отличный звук", "Great sound"))
    await copywriting.write_master(None, item, "убери предложение про коробку")
    second = calls[1]["content"]
    assert "- убери эмодзи" in second and "New instruction from the owner:\nубери предложение про коробку" in second
    assert item.text_rules == ["убери эмодзи", "убери предложение про коробку"]
    assert copywriting.master_text(item, Language.RU).body == "Отличный звук"


async def test_full_rewrite_keeps_owner_requirements(monkeypatch):
    item, calls = _item(), []
    item.text_rules = ["убери эмодзи"]
    monkeypatch.setattr(copywriting, "structured", _fake(calls, "Новый текст", "New text"))
    await copywriting.write_master(None, item)  # «🔄 Новый текст» / rewrite after a fact correction
    assert calls[0]["purpose"] == "listing" and "- убери эмодзи" in calls[0]["content"]
    assert item.text_rules == ["убери эмодзи"]  # a rewrite is not an edit: nothing new to remember


def test_posts_never_carry_emoji():
    from decimal import Decimal

    from app.services.listing import EMOJI, render_post, render_publication, strip_emoji

    assert strip_emoji("✅ Отличный звук 🎧\n❗️Царапина\n👍🏽\n\n\n➡️ Коробка — есть, 5 € · №1 → ok") == \
        "Отличный звук\nЦарапина\n\nКоробка — есть, 5 € · №1 → ok"
    item = Item(id=1, title="Sony", price=Decimal(70), quantity_total=2, quantity_sold=0)
    text = ItemText(platform=TextPlatform.TELEGRAM_MASTER, language=Language.RU, title="🔥 Sony", body="✅ Звук")
    for post in (render_post(item, text, "seller"), render_publication(item, "🔥 Sony\n\n✅ Звук", ["#продам"], Language.EN, "s")):
        assert not EMOJI.search(post), post
    assert render_post(item, text, "seller").startswith("Sony\n\nЗвук\n\nЦена: 70 €")
