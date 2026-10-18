"""Inline keyboards. Callback data format: '<prefix>:<action>:<item_id>[:<arg>]' (<= 64 bytes)."""

from telethon import Button


def review(item_id: int) -> list[list[Button]]:
    i = item_id
    return [
        [Button.inline("✅ Утвердить", f"rv:ok:{i}".encode())],
        [Button.inline("🔢 Кол-во", f"rv:qty:{i}".encode()), Button.inline("✏️ Поправить факты", f"rv:fix:{i}".encode())],
        [Button.inline("💶 Цена", f"rv:price:{i}".encode()), Button.inline("🔄 Пересчитать цену", f"rv:reprice:{i}".encode())],
        [Button.inline("📝 Правка текста", f"rv:edit:{i}".encode()), Button.inline("🔄 Новый текст", f"rv:regen:{i}".encode())],
        [Button.inline("⏭ Позже", f"rv:later:{i}".encode()), Button.inline("🗑 Не продаю", f"rv:drop:{i}".encode())],
    ]


def confirm_drop(item_id: int) -> list[list[Button]]:
    return [[
        Button.inline("Да, не продаю", f"rv:dropyes:{item_id}".encode()),
        Button.inline("Отмена", f"rv:dropno:{item_id}".encode()),
    ]]


def quantity(item_id: int) -> list[list[Button]]:
    nums = [Button.inline(str(n), f"qty:set:{item_id}:{n}".encode()) for n in range(1, 7)]
    return [nums[:3], nums[3:], [Button.inline("Другое число", f"qty:other:{item_id}".encode()),
                                 Button.inline("← Назад", f"qty:back:{item_id}".encode())]]


def failed(item_id: int) -> list[list[Button]]:
    return [[Button.inline("🔁 Повторить", f"rt:go:{item_id}".encode()),
             Button.inline("🗑 Не продаю", f"rv:dropyes:{item_id}".encode())]]


def reopen(item_id: int) -> list[list[Button]]:
    return [[Button.inline("↩️ Вернуть на правку", f"rv:reopen:{item_id}".encode())]]


def chat(c) -> list[list[Button]]:
    i = c.id
    rows = []
    if c.can_post and not c.is_active:
        rows.append([Button.inline("✅ Подтвердить и публиковать", f"ch:ok:{i}".encode())])
    rows.append([Button.inline("✏️ Поправить правила", f"ch:edit:{i}".encode()),
                 Button.inline("🌐 Язык", f"ch:lang:{i}".encode())])
    rows.append([Button.inline("🔄 Перепроверить чат", f"ch:recheck:{i}".encode())])
    if c.kind == "forum":
        rows.insert(0, [Button.inline("🧵 Тема для объявлений", f"ch:topic:{i}".encode())])
    if c.is_active:
        rows.append([Button.inline("✅ Готово", f"ch:done:{i}".encode()),
                     Button.inline("⏸ Не публиковать сюда", f"ch:off:{i}".encode())])
    else:
        rows.append([Button.inline("⏭ Позже", f"ch:later:{i}".encode()),
                     Button.inline("🚫 Не публиковать сюда", f"ch:off:{i}".encode())])
    return rows


def previews(item_id: int) -> list[list[Button]]:
    i = item_id
    return [
        [Button.inline("✅ Одобрить все версии", f"pv:ok:{i}".encode())],
        [Button.inline("🔄 Переписать", f"pv:regen:{i}".encode()), Button.inline("⏭ Позже", f"pv:later:{i}".encode())],
        [Button.inline("🗑 Не публиковать", f"pv:drop:{i}".encode())],
    ]


def publisher(enabled: bool) -> list[list[Button]]:
    toggle = Button.inline("⏸ Выключить", b"pb:off") if enabled else Button.inline("▶️ Включить", b"pb:on")
    return [[toggle, Button.inline("🔄 Обновить", b"pb:refresh")],
            [Button.inline("📤 Опубликовать следующий сейчас", b"pb:now")]]


# ---------- stage 3: inquiries & sales ----------

def _item_sale_row(prefix: str, ref: int, item) -> list[Button]:
    from app.db.enums import ItemStatus

    if item.status == ItemStatus.RESERVED:
        return [Button.inline("🔓 Снять бронь", f"{prefix}:unres:{ref}".encode()),
                Button.inline("💰 Продано", f"{prefix}:sold:{ref}".encode())]
    return [Button.inline("🔒 Бронь", f"{prefix}:res:{ref}".encode()),
            Button.inline("💰 Продано", f"{prefix}:sold:{ref}".encode())]


def inquiry(inq, item, buyer, chat, candidates) -> list[list[Button]]:
    from app.db.enums import InquiryStatus, ItemStatus
    from app.userbot.sender import message_link

    rows: list[list[Button]] = []
    links = []
    if buyer is not None and buyer.username:
        links.append(Button.url("✉️ Открыть чат", f"https://t.me/{buyer.username}"))
    if chat is not None and (link := message_link(chat.tg_chat_id, chat.username, inq.tg_message_id, chat.forum_topic_id)):
        links.append(Button.url("💬 К ответу в чате", link))
    if item is None:
        if inq.status != InquiryStatus.HANDLED:
            for c in candidates:
                rows.append([Button.inline(f"#{c.id} {c.display_name}"[:60], f"iq:set:{inq.id}:{c.id}".encode())])
            rows.append([Button.inline("🚫 Не про продажу", f"iq:spam:{inq.id}".encode())])
        return [*rows, links] if links else rows
    if item.status in (ItemStatus.READY, ItemStatus.PUBLISHED, ItemStatus.HAS_INQUIRIES, ItemStatus.RESERVED):
        rows.append(_item_sale_row("iq", inq.id, item))
        rows.append([Button.inline("🔁 Это другая вещь", f"iq:pick:{inq.id}".encode())])
    if links:
        rows.append(links)
    return rows


def sold_price(prefix: str, ref: int, price_label: str) -> list[list[Button]]:
    return [[Button.inline(f"💰 За {price_label}", f"{prefix}:soldp:{ref}".encode()),
             Button.inline("✏️ Другая цена", f"{prefix}:soldx:{ref}".encode())],
            [Button.inline("← Назад", f"{prefix}:back:{ref}".encode())]]


def item_actions(item) -> list[list[Button]] | None:
    from app.db.enums import ItemStatus

    rows = []
    if item.status in (ItemStatus.READY, ItemStatus.PUBLISHED, ItemStatus.HAS_INQUIRIES, ItemStatus.RESERVED):
        rows.append(_item_sale_row("it", item.id, item))
        rows.append([Button.inline("🗑 Снять с продажи", f"it:wd:{item.id}".encode())])
    if item.status == ItemStatus.READY:
        rows.append([Button.inline("↩️ Вернуть на правку", f"rv:reopen:{item.id}".encode())])
    return rows or None


def confirm_withdraw(item_id: int) -> list[list[Button]]:
    return [[Button.inline("Да, снять и удалить посты", f"it:wdyes:{item_id}".encode()),
             Button.inline("Отмена", f"it:back:{item_id}".encode())]]


def topics(chat) -> list[list[Button]]:
    rows = []
    for t in (chat.forum_topics or [])[:40]:
        if t.get("closed"):
            continue
        mark = "✅ " if t["id"] == chat.forum_topic_id else ""
        rows.append([Button.inline(f"{mark}{t['title']}"[:60], f"ct:{chat.id}:{t['id']}".encode())])
    rows.append([Button.inline("← Назад", f"ch:card:{chat.id}".encode())])
    return rows
