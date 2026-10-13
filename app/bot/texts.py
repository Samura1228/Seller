"""All user-facing bot strings (Russian UI) and card rendering. HTML parse mode."""

from html import escape

from app.db.enums import Condition, ItemStatus, Language
from app.db.models import Item, PriceResearch
from app.services.copywriting import master_text
from app.services.listing import fmt_eur, render_post
from app.services.pricing import comps_summary

CONDITION_RU = {
    Condition.NEW_SEALED: "новое, запечатано",
    Condition.LIKE_NEW: "как новое",
    Condition.GOOD: "хорошее, есть следы использования",
    Condition.USED: "б/у, заметный износ",
}

STATUS_RU = {
    ItemStatus.NEW: "🆕 новая",
    ItemStatus.RECOGNIZED: "🧠 в обработке",
    ItemStatus.PENDING_APPROVAL: "🕓 ждёт подтверждения",
    ItemStatus.READY: "✅ готова",
    ItemStatus.PUBLISHED: "📢 опубликована",
    ItemStatus.HAS_INQUIRIES: "💬 есть отклики",
    ItemStatus.RESERVED: "🔒 бронь",
    ItemStatus.SOLD: "💰 продана",
    ItemStatus.WITHDRAWN: "🗑 снята",
}

HELP = (
    "<b>Как пользоваться</b>\n\n"
    "1. Сложи фото в папку: <i>одна подпапка = одна вещь</i>. Имя подпапки — подсказка (например «sony наушники»).\n"
    "2. Заархивируй папку в .zip и отправь сюда файлом.\n"
    "3. Я распознаю вещи, подберу цену и напишу тексты. Карточки на подтверждение приходят по одной.\n\n"
    "<b>Команды</b>\n"
    "/next — показать следующую карточку\n"
    "/items — список вещей\n"
    "/item 12 — открыть вещь №12\n"
    "/retry 12 — повторить обработку вещи после ошибки\n"
    "/cost — сколько потрачено на AI\n"
    "/cancel — отменить ввод"
)

ASK_PRICE = "💶 Отправь цену для <b>{name}</b>:\n<code>120</code> — только цена (минимум посчитаю сам)\n<code>120/100</code> — цена / минимум"
ASK_FIX = (
    "✏️ Напиши, что поправить в <b>{name}</b>.\n"
    "Например: «это WH-1000XM5, есть царапина на левой чашке, коробки нет».\n"
    "Цена и тексты будут пересчитаны."
)
ASK_EDIT = (
    "📝 Что изменить в тексте <b>{name}</b>?\n"
    "Например: «короче», «добавь, что работает с iPhone», «убери эмодзи». Правка применится к RU и EN."
)
ASK_QTY = "🔢 Сколько штук <b>{name}</b>?"
ASK_QTY_NUMBER = "🔢 Отправь количество числом."
BAD_PRICE = "Не понял цену. Пример: <code>120</code> или <code>120/100</code>"
BAD_QTY = "Нужно целое число от 1 до 999."
CANCELLED = "Ок, отменил."
NO_PENDING_INPUT = "Отправь .zip с фото или используй /help."
QUEUE_EMPTY = "Очередь подтверждений пуста ✅"
NOT_FOUND = "Вещь не найдена."
BUSY = "⏳ Уже обновляю эту вещь, подожди."
UPDATING = "⏳ Обновляю…"
NOT_ZIP = "Нужен архив .zip (одна подпапка = одна вещь)."


def _name(item: Item) -> str:
    return escape(item.display_name)


def review_card(item: Item, research: PriceResearch | None, seller: str, *, busy: bool = False) -> str:
    lines = [f"<b>#{item.id} · {_name(item)}</b>"]
    if item.folder_hint and item.folder_hint != item.title:
        lines.append(f"📁 {escape(item.folder_hint)}")
    if busy:
        lines.append(f"\n{UPDATING}")

    meta = []
    if item.category:
        meta.append(f"Категория: {escape(item.category)}")
    if item.condition:
        cond = CONDITION_RU[item.condition]
        if item.condition_notes:
            cond += f" — {escape(item.condition_notes)}"
        meta.append(f"Состояние: {cond}")
    if item.specs:
        meta.append("Характеристики: " + escape(", ".join(f"{k}: {v}" for k, v in item.specs.items())))
    if item.included:
        meta.append("В комплекте: " + escape(", ".join(item.included)))
    qty_q = any(q.get("field") == "quantity" for q in item.uncertain_fields)
    meta.append(f"Кол-во: {item.quantity_total}" + (" ❓" if qty_q else ""))
    lines.append("\n" + "\n".join(meta))

    questions = [q for q in item.uncertain_fields if q.get("field") != "quantity"]
    if questions:
        lines.append("\n❓ <b>Проверь:</b>\n" + "\n".join(f"• {escape(q['question_ru'])}" for q in questions))

    price_block = [f"💶 <b>Цена: {fmt_eur(item.price)}</b> · минимум {fmt_eur(item.min_price)}"]
    refs = []
    if research and research.new_price:
        refs.append(f"новая {fmt_eur(research.new_price)}")
    if (cs := comps_summary(research)):
        refs.append(f"Bazaraki {cs}")
    if refs:
        price_block.append(" · ".join(refs))
    if research and research.rationale:
        price_block.append(f"<i>{escape(research.rationale)}</i>")
    lines.append("\n" + "\n".join(price_block))

    for lang, flag in ((Language.RU, "🇷🇺"), (Language.EN, "🇬🇧")):
        t = master_text(item, lang)
        if t:
            post = escape(render_post(item, t, seller))
            lines.append(f"\n{flag} <b>{lang.value.upper()}</b>\n<blockquote>{post}</blockquote>")

    text = "\n".join(lines)
    return text if len(text) <= 4000 else text[:3990] + "…"


def resolved_card(item: Item, verdict: str) -> str:
    return f"<b>#{item.id} · {_name(item)}</b> — {fmt_eur(item.price)}\n{verdict}"


def item_line(item: Item) -> str:
    return f"#{item.id} {_name(item)} — {fmt_eur(item.price)} · {STATUS_RU[item.status]}"


def failed(item: Item, error: str) -> str:
    return (
        f"⚠️ <b>#{item.id} · {_name(item)}</b>: не удалось обработать.\n"
        f"<code>{escape(error[:300])}</code>\n\nМожно повторить или открыть /item {item.id}"
    )


def mb(n: int | None) -> str:
    return f"{(n or 0) / 1024 / 1024:.0f} МБ"


def batch_report(found: int, report: dict) -> str:
    parts = [f"📦 Найдено вещей: <b>{found}</b>"]
    if report.get("duplicates"):
        names = ", ".join(escape(d["folder"]) for d in report["duplicates"][:10])
        parts.append(f"↩️ Уже были загружены (пропущены): {len(report['duplicates'])} — {names}")
    if report.get("empty"):
        parts.append(f"⚠️ Папки без читаемых фото: {escape(', '.join(report['empty'][:10]))}")
    if report.get("loose_photos"):
        parts.append(f"⚠️ Фото в корне архива без папки пропущены: {report['loose_photos']}")
    if report.get("videos"):
        parts.append(f"🎞 Видео пропущены: {report['videos']}")
    if report.get("bad_photos"):
        parts.append(f"⚠️ Нечитаемых фото: {report['bad_photos']}")
    return "\n".join(parts)
