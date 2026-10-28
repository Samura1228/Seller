"""Final post text = AI-written part + deterministic footer (price, quantity, logistics, contact).

Price and quantity live only in the footer, so changing them never requires rewriting the text.
"""

import re
from decimal import Decimal

from app.db.enums import Language
from app.db.models import Item, ItemText

# Emoji and pictographs (incl. ✅ ✔ ❗ ⭐ ➡ ▪ flags, keycaps, skin tones, ZWJ sequences). Posts never carry emoji.
EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\U0001FC00-\U0001FFFF\u2600-\u27BF\u2B00-\u2BFF\u231A\u231B\u2328\u23CF"
    "\u23E9-\u23FA\u24C2\u25AA\u25AB\u25B6\u25C0\u25FB-\u25FE\u2934\u2935\u3030\u303D\u3297\u3299"
    "\uFE0E\uFE0F\u200D\u20E3\U000E0020-\U000E007F]"
)


def strip_emoji(text: str) -> str:
    """Removes emoji and tidies the whitespace they leave behind; line structure is kept."""
    lines = [re.sub(r"[ \t]{2,}", " ", EMOJI.sub("", line)).strip() for line in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def fmt_eur(v: Decimal | None) -> str:
    if v is None:
        return "—"
    return f"€{int(v)}" if v == v.to_integral_value() else f"€{v:.2f}"


def footer(item: Item, lang: Language, seller_username: str) -> str:
    price = fmt_eur(item.price)
    qty = item.quantity_left
    if lang == Language.RU:
        lines = [f"Цена: {price.lstrip('€')} €, торг уместен"]
        if qty > 1:
            lines.append(f"В наличии: {qty} шт. (цена за штуку)")
        lines += ["Лимасол · самовывоз / встреча · без доставки", f"Пишите: @{seller_username}"]
    else:
        lines = [f"Price: {price}, negotiable"]
        if qty > 1:
            lines.append(f"Available: {qty} pcs (price per item)")
        lines += ["Limassol · pickup / meetup · no delivery", f"Message: @{seller_username}"]
    return "\n".join(lines)


def render_post(item: Item, text: ItemText, seller_username: str) -> str:
    return f"{strip_emoji(text.title)}\n\n{strip_emoji(text.body)}\n\n{footer(item, text.language, seller_username)}"


def render_publication(item: Item, text: str, hashtags: list[str], lang: Language, seller_username: str) -> str:
    """Final per-chat post: AI-written part + deterministic footer + chat hashtags."""
    post = f"{strip_emoji(text)}\n\n{footer(item, lang, seller_username)}"
    if hashtags:
        post += "\n\n" + " ".join(hashtags)
    return post
