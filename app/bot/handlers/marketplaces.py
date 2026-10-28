"""Marketplaces in the bot: /platforms, and the owner's decision on a form the helper filled."""

import re

from telethon import events

from app.bot import keyboards, texts
from app.bot.core import BotCore
from app.db.enums import ExternalPlatform
from app.services import marketplaces, settings_store


def register(core: BotCore) -> None:
    def admin_only(e) -> bool:
        return e.sender_id == core.admin

    async def show_platforms(event=None) -> None:
        rows, seen = await marketplaces.status()
        text = texts.platforms_status(rows, seen, core.settings)
        if not core.settings.helper_token:
            text += "\n\n⚠️ На сервере не задан <code>HELPER_TOKEN</code> — помощник не сможет подключиться."
        if event is None:
            await core.send(text, buttons=keyboards.platforms(rows))
        else:
            await event.edit(text, parse_mode="html", buttons=keyboards.platforms(rows), link_preview=False)

    @core.client.on(events.NewMessage(from_users=[core.admin], pattern=r"^/platforms\b"))
    async def on_platforms(event) -> None:
        await show_platforms()

    @core.client.on(events.CallbackQuery(data=re.compile(rb"^mp:(\w+):(\w+)$"), func=admin_only))
    async def on_platforms_button(event) -> None:
        action, arg = event.data_match.group(1).decode(), event.data_match.group(2).decode()
        if action == "toggle":
            p = ExternalPlatform(arg)
            on = not await marketplaces.is_enabled(p)
            await settings_store.put(marketplaces.k_enabled(p), on)
            await event.answer(("Включено: " if on else "Выключено: ") + marketplaces.NAMES[p])
        elif action == "all":
            n = await marketplaces.enqueue_all()
            await event.answer(f"Добавлено в очередь: {n}")
        else:
            await event.answer()
        await show_platforms(event)

    @core.client.on(events.CallbackQuery(data=re.compile(rb"^mk:(\w+):(\d+)$"), func=admin_only))
    async def on_listing_button(event) -> None:
        action, listing_id = event.data_match.group(1).decode(), int(event.data_match.group(2))
        ext, item = await marketplaces.listing_label(listing_id)
        head = texts.mk_head(ext, item, marketplaces.NAMES[ext.platform])
        match action:
            case "pub" | "cancel":
                try:
                    await marketplaces.decide(listing_id, publish=action == "pub")
                except ValueError as e:
                    await event.answer(str(e), alert=True)
                    await event.edit(buttons=None)
                    return
                verdict = "✅ Публикую — пришлю ссылку." if action == "pub" else "❌ Не публикую на этой площадке."
                await event.edit(f"{head}\n{verdict}", parse_mode="html", buttons=None)
                await event.answer()
            case "retry":
                await marketplaces.retry(listing_id)
                await event.edit(f"{head}\n🔁 Вернул в очередь.", parse_mode="html", buttons=None)
                await event.answer()
            case "skip":
                await marketplaces.skip(listing_id)
                await event.edit(f"{head}\n⏭ Пропущено.", parse_mode="html", buttons=None)
                await event.answer()
            case _:
                await event.answer()
