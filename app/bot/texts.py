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
    "/cancel — отменить ввод\n\n"
    "<b>Публикация в чаты</b>\n"
    "/addchat @chat1 @chat2 — добавить чаты (аккаунт для продаж уже должен в них состоять)\n"
    "/chats — список чатов · /chat 3 — карточка чата\n"
    "/publisher — включить/выключить публикации, статус очереди\n"
    "/versions 12 — заново подготовить версии постов для вещи"
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


# ---------- stage 2: chats, previews, publisher ----------

LANG_RU = {"ru": "🇷🇺 RU", "en": "🇬🇧 EN", "mixed": "🌐 смешанный"}
RULES_STATUS_RU = {"auto": "распознаны автоматически", "needs_me": "❓ нужна проверка", "confirmed": "✅ подтверждены"}
DAYS_RU = {"mon": "пн", "tue": "вт", "wed": "ср", "thu": "чт", "fri": "пт", "sat": "сб", "sun": "вс"}

NEED_USERBOT = (
    "Для чатов нужен аккаунт для продаж. Сгенерируй сессию скриптом <code>scripts/make_session.py</code> "
    "у себя на компьютере и добавь её в Railway переменной <code>USERBOT_SESSION</code>."
)
ADDCHAT_HELP = "Пришли: <code>/addchat @chat1 @chat2</code> (аккаунт для продаж должен уже состоять в этих чатах)."
ASK_CHAT_RULES = (
    "✏️ Что поправить в правилах <b>{name}</b>?\n"
    "Например: «можно 2 поста в день», «хэштег #продам обязателен», «по выходным нельзя»."
)


def chat_name(chat) -> str:
    return escape(chat.title or (f"@{chat.username}" if chat.username else f"чат {chat.id}"))


def chat_card(chat, *, busy: bool = False) -> str:
    from app.services.chats import effective_language
    from app.services.scheduling import chat_limits

    r = chat.rules or {}
    head = f"<b>Чат #{chat.id} · {chat_name(chat)}</b>"
    if chat.username:
        head += f" · @{escape(chat.username)}"
    lines = [head]
    meta = [f"Тип: {chat.kind or '?'}"]
    if chat.members_count:
        meta.append(f"участников: {chat.members_count}")
    meta.append(f"язык: {LANG_RU[effective_language(chat).value]}" + (" (вручную)" if chat.language_override else ""))
    lines.append(" · ".join(meta))
    if busy:
        lines.append(f"\n{UPDATING}")
    if chat.check_note:
        lines.append(f"\n⚠️ {escape(chat.check_note)}")

    allowed = r.get("posting_allowed")
    lines.append("\n<b>Правила</b> — " + RULES_STATUS_RU.get(chat.rules_status.value, ""))
    if r.get("summary_ru"):
        lines.append(f"<i>{escape(r['summary_ru'])}</i>")
    lim = chat_limits(r, chat.slowmode_seconds, 1)
    facts = [
        "Объявления от частных лиц: " + {True: "можно", False: "🚫 нельзя", None: "❓ неясно"}[allowed],
        f"Лимит: {lim.per_day} пост(а) в сутки" + ("" if r.get("max_posts_per_day") else " (по умолчанию)"),
    ]
    if lim.min_interval.total_seconds():
        facts.append(f"Интервал между постами: {lim.min_interval.total_seconds() / 3600:g} ч")
    if r.get("allowed_days"):
        facts.append("Дни: " + ", ".join(DAYS_RU.get(d, d) for d in r["allowed_days"]))
    tags = [*(r.get("required_hashtags") or []), *(f"({t})" for t in (r.get("allowed_hashtags") or [])[:8])]
    if tags:
        facts.append("Хэштеги: " + escape(" ".join(tags)))
    if r.get("max_photos"):
        facts.append(f"Фото: до {r['max_photos']}")
    if not chat.can_send_photos:
        facts.append("Фото: 🚫 нельзя, только текст")
    if r.get("format_notes_ru"):
        facts.append("Формат: " + escape(r["format_notes_ru"]))
    if r.get("forbidden_ru"):
        facts.append("Запрещено: " + escape(r["forbidden_ru"]))
    lines.append("\n".join(f"• {f}" for f in facts))
    if r.get("questions_ru"):
        lines.append("\n❓ <b>Уточни:</b>\n" + "\n".join(f"• {escape(q)}" for q in r["questions_ru"]))
    if chat.rules_raw:
        lines.append(f"\n<blockquote expandable>{escape(chat.rules_raw[:1500])}</blockquote>")
    lines.append("\n" + ("🟢 Публикую сюда" if chat.is_active else "⚪️ Пока не публикую"))
    text = "\n".join(lines)
    return text if len(text) <= 4000 else text[:3990] + "…"


def chat_line(chat) -> str:
    from app.services.chats import effective_language

    state = "🟢" if chat.is_active else ("🚫" if not chat.can_post else "⚪️")
    last = f" · последний пост {chat.last_post_at:%d.%m %H:%M}" if chat.last_post_at else ""
    return f"{state} #{chat.id} {chat_name(chat)} · {LANG_RU[effective_language(chat).value]}{last}"


def previews_card(item, rows, seller: str, *, busy: bool = False) -> tuple[list[str], str]:
    """Returns (messages to send before the card, card text). Each version is shown exactly as it will be posted."""
    from app.services.listing import render_publication

    blocks = []
    for pub, chat in rows:
        post = render_publication(item, pub.text, pub.hashtags or [], pub.language, seller)
        blocks.append(f"📍 <b>{chat_name(chat)}</b> · {pub.language.value.upper()} · {len(post)} симв.\n"
                      f"<blockquote>{escape(post)}</blockquote>")
    head = f"<b>#{item.id} · {escape(item.display_name)}</b> — {fmt_eur(item.price)}\n📝 Версии постов для чатов"
    tail = "\n\nПроверь тексты. После одобрения посты встанут в очередь публикации."
    if busy:
        tail = f"\n\n{UPDATING}"
    one = head + "\n\n" + "\n\n".join(blocks) + tail
    if len(one) <= 4000:
        return [], one
    return [b if len(b) <= 4000 else b[:3990] + "…" for b in blocks], head + f"\n\nВерсии выше ⬆️ ({len(blocks)} шт.)" + tail


def posted(item_id: int, item_name: str, chat_title: str, link: str | None) -> str:
    where = f'<a href="{escape(link)}">{escape(chat_title)}</a>' if link else escape(chat_title)
    return f"📢 Опубликовано: <b>#{item_id} {escape(item_name)}</b> → {where}"


def publisher_status(st, settings) -> str:
    state = "🟢 включён" if st.enabled else "⚪️ выключен"
    lines = [
        f"<b>Публикации</b>: {state}",
        f"Сейчас: {'можно публиковать' if not st.block_reason else escape(st.block_reason)}",
        f"В очереди: {st.approved} · ждут одобрения: {st.drafts}",
        f"Опубликовано за сутки: {st.posted_24h} (лимит {settings.publish_max_per_day})",
        f"Чатов активно: {st.active_chats} · готовы принять пост сейчас: {st.ready_chats}",
    ]
    if st.next_title:
        lines.append(f"Следующий пост: {escape(st.next_title)}")
    lines.append(
        f"\n<i>Окно {settings.publish_window_start}:00–{settings.publish_window_end}:00, "
        f"пауза между постами {settings.publish_gap_min_minutes}–{settings.publish_gap_max_minutes} мин, "
        f"до {settings.publish_max_per_hour} в час.</i>"
    )
    return "\n".join(lines)
