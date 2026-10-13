from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import prompts
from app.ai.client import structured
from app.ai.schemas import MasterTexts
from app.db.enums import Language, TextPlatform, TextStatus
from app.db.models import Item, ItemText
from app.services.recognition import item_facts


def master_text(item: Item, lang: Language) -> ItemText | None:
    return next(
        (t for t in item.texts if t.platform == TextPlatform.TELEGRAM_MASTER and t.language == lang), None
    )


async def write_master(s: AsyncSession, item: Item, instruction: str | None = None) -> MasterTexts:
    """(Re)writes the RU+EN master texts. With `instruction`, edits the current texts accordingly."""
    content = f"Item card:\n{item_facts(item)}"
    ru, en = master_text(item, Language.RU), master_text(item, Language.EN)
    if instruction and ru and en:
        content += (
            f"\n\nCurrent texts:\n[RU]\n{ru.title}\n{ru.body}\n\n[EN]\n{en.title}\n{en.body}\n\n"
            f"Owner's instruction for the edit (apply to BOTH versions, keep the rest):\n{instruction}"
        )
    result = await structured(
        purpose="listing_edit" if instruction else "listing",
        output=MasterTexts,
        system=prompts.load("listing"),
        content=content,
        item_id=item.id,
        max_tokens=2500,
    )
    for lang, text in ((Language.RU, result.ru), (Language.EN, result.en)):
        existing = master_text(item, lang)
        if existing:
            existing.title, existing.body = text.title[:255], text.body
            existing.status = TextStatus.DRAFT
            existing.version += 1
        else:
            item.texts.append(ItemText(
                platform=TextPlatform.TELEGRAM_MASTER, language=lang, title=text.title[:255], body=text.body
            ))
    return result
