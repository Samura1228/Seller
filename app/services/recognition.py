import asyncio
import json
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.ai import prompts
from app.ai.client import image_block, structured
from app.ai.schemas import Question, Recognition
from app.config import get_settings
from app.db.models import Item
from app.services.photos import ai_jpeg_bytes


def pick_photos(paths: list[Path], limit: int) -> list[Path]:
    """Evenly spread selection so the AI sees the whole set (first photo is usually the overview)."""
    if len(paths) <= limit:
        return paths
    step = (len(paths) - 1) / (limit - 1)
    return [paths[round(i * step)] for i in range(limit)]


async def load_item(s: AsyncSession, item_id: int) -> Item:
    item = await s.get(Item, item_id, options=[selectinload(Item.photos), selectinload(Item.texts)])
    if item is None:
        raise LookupError(f"Item {item_id} not found")
    return item


def apply_recognition(item: Item, r: Recognition) -> None:
    item.title = r.title_ru[:255]
    item.category = r.category[:100]
    item.brand = (r.brand or None) and r.brand[:100]
    item.model = (r.model or None) and r.model[:150]
    item.specs = {sp.name: sp.value for sp in r.specs if sp.name and sp.value}
    item.condition = r.condition
    item.condition_notes = r.condition_notes_ru or None
    item.included = r.included
    item.quantity_total = max(1, min(r.quantity, 999))
    questions = [q.model_dump() for q in r.questions]
    if not r.quantity_confident and not any(q["field"] == "quantity" for q in questions):
        questions.append(Question(field="quantity", question_ru=f"Сколько штук? Я вижу {r.quantity}.").model_dump())
    item.uncertain_fields = questions
    item.recognition_raw = r.model_dump(mode="json")


async def recognize(s: AsyncSession, item: Item) -> Recognition:
    settings = get_settings()
    paths = [settings.data_dir / p.path for p in item.photos]
    chosen = pick_photos(paths, settings.ai_max_photos_per_item)
    images = await asyncio.gather(*(asyncio.to_thread(ai_jpeg_bytes, p, settings.photo_ai_max_side) for p in chosen))

    content = [
        {"type": "text", "text": f"Folder name (hint from the owner): {item.folder_hint or '-'}\n"
                                 f"Photos: {len(chosen)} of {len(paths)}."},
        *(image_block(b) for b in images),
    ]
    result = await structured(
        purpose="recognize", output=Recognition, system=prompts.load("recognize"), content=content, item_id=item.id
    )
    apply_recognition(item, result)
    return result


async def apply_correction(s: AsyncSession, item: Item, correction: str) -> Recognition:
    settings = get_settings()
    current = item.recognition_raw or {}
    content = (
        f"Current card (JSON):\n{json.dumps(current, ensure_ascii=False, indent=1)}\n\n"
        f"Owner's correction:\n{correction}"
    )
    result = await structured(
        purpose="correct", output=Recognition, system=prompts.load("correct"), content=content,
        item_id=item.id, model=settings.ai_model_fast,
    )
    apply_recognition(item, result)
    item.my_notes = "\n".join(filter(None, [item.my_notes, correction.strip()]))
    return result


def item_facts(item: Item) -> str:
    """Compact JSON card used as input for pricing and copywriting."""
    facts = {
        "name": item.title,
        "brand": item.brand,
        "model": item.model,
        "category": item.category,
        "specs": item.specs,
        "condition": item.condition.value if item.condition else None,
        "condition_notes": item.condition_notes,
        "included": item.included,
        "quantity": item.quantity_total,
        "owner_notes": item.my_notes,
    }
    return json.dumps({k: v for k, v in facts.items() if v not in (None, "", [], {})}, ensure_ascii=False, indent=1)
