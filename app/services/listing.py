"""Final post text = AI-written part + deterministic footer (price, quantity, logistics, contact).

Price and quantity live only in the footer, so changing them never requires rewriting the text.
"""

from decimal import Decimal

from app.db.enums import Language
from app.db.models import Item, ItemText


def fmt_eur(v: Decimal | None) -> str:
    if v is None:
        return "—"
    return f"€{int(v)}" if v == v.to_integral_value() else f"€{v:.2f}"


def footer(item: Item, lang: Language, seller_username: str) -> str:
    price = fmt_eur(item.price)
    qty = item.quantity_left
    if lang == Language.RU:
        lines = [f"💶 Цена: {price.lstrip('€')} €, торг уместен"]
        if qty > 1:
            lines.append(f"📦 В наличии: {qty} шт. (цена за штуку)")
        lines += ["📍 Лимасол · самовывоз / встреча · без доставки", f"✉️ Пишите: @{seller_username}"]
    else:
        lines = [f"💶 Price: {price}, negotiable"]
        if qty > 1:
            lines.append(f"📦 Available: {qty} pcs (price per item)")
        lines += ["📍 Limassol · pickup / meetup · no delivery", f"✉️ Message: @{seller_username}"]
    return "\n".join(lines)


def render_post(item: Item, text: ItemText, seller_username: str) -> str:
    return f"{text.title}\n\n{text.body.strip()}\n\n{footer(item, text.language, seller_username)}"


def render_publication(item: Item, text: str, hashtags: list[str], lang: Language, seller_username: str) -> str:
    """Final per-chat post: AI-written part + deterministic footer + chat hashtags."""
    post = f"{text.strip()}\n\n{footer(item, lang, seller_username)}"
    if hashtags:
        post += "\n\n" + " ".join(hashtags)
    return post
