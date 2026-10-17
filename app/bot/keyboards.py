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
