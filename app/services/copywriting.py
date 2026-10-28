from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import prompts
from app.ai.client import structured
from app.ai.schemas import MasterTexts
from app.db.enums import Language, TextPlatform, TextStatus
from app.db.models import Item, ItemText
from app.services.listing import strip_emoji
from app.services.recognition import item_facts

MAX_TEXT_RULES = 15


def master_text(item: Item, lang: Language) -> ItemText | None:
    return next(
        (t for t in item.texts if t.platform == TextPlatform.TELEGRAM_MASTER and t.language == lang), None
    )


def text_rules_block(item: Item) -> str:
    """The owner's earlier text edits: they must survive every later edit and rewrite."""
    if not item.text_rules:
        return ""
    return "Owner's requirements for the text (earlier edits, oldest first; a later one wins on conflict):\n" + \
        "\n".join(f"- {r}" for r in item.text_rules)


async def write_master(s: AsyncSession, item: Item, instruction: str | None = None) -> MasterTexts:
    """(Re)writes the RU+EN master texts. With `instruction`, edits the current texts and remembers the edit."""
    ru, en = master_text(item, Language.RU), master_text(item, Language.EN)
    rules = text_rules_block(item)
    editing = bool(instruction and ru and en)
    if editing:
        content = (
            f"Item card (reference only):\n{item_facts(item)}\n\n"
            + (f"{rules}\n\n" if rules else "")
            + f"Current texts:\n[RU]\n{ru.title}\n{ru.body}\n\n[EN]\n{en.title}\n{en.body}\n\n"
            f"New instruction from the owner:\n{instruction}"
        )
    else:
        content = f"Item card:\n{item_facts(item)}" + (f"\n\n{rules}" if rules else "")
    result = await structured(
        purpose="listing_edit" if editing else "listing",
        output=MasterTexts,
        system=prompts.load("listing_edit" if editing else "listing"),
        content=content,
        item_id=item.id,
        max_tokens=2500,
    )
    for lang, text in ((Language.RU, result.ru), (Language.EN, result.en)):
        text.title, text.body = strip_emoji(text.title), strip_emoji(text.body)
        existing = master_text(item, lang)
        if existing:
            existing.title, existing.body = text.title[:255], text.body
            existing.status = TextStatus.DRAFT
            existing.version += 1
        else:
            item.texts.append(ItemText(
                platform=TextPlatform.TELEGRAM_MASTER, language=lang, title=text.title[:255], body=text.body
            ))
    if editing:
        item.text_rules = [*(item.text_rules or []), instruction.strip()[:300]][-MAX_TEXT_RULES:]
    return result
