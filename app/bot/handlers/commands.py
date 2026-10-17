from sqlalchemy import func, select
from telethon import events

from app.bot import keyboards, texts
from app.bot.core import BotCore
from app.db.enums import ItemStatus
from app.db.models import AiUsage, Item
from app.db.session import session_scope
from app.services import pipeline


def register(core: BotCore) -> None:
    def cmd(pattern: str):
        return events.NewMessage(from_users=[core.admin], pattern=pattern)

    @core.client.on(cmd(r"^/(start|help)\b"))
    async def on_help(event) -> None:
        await core.send(texts.HELP)

    @core.client.on(cmd(r"^/cancel\b"))
    async def on_cancel(event) -> None:
        core.pending = None
        await core.send(texts.CANCELLED)

    @core.client.on(cmd(r"^/next\b"))
    async def on_next(event) -> None:
        await core.show_next(force=True)

    @core.client.on(cmd(r"^/items\b"))
    async def on_items(event) -> None:
        async with session_scope() as s:
            counts = dict((await s.execute(select(Item.status, func.count()).group_by(Item.status))).all())
            items = list(await s.scalars(select(Item).order_by(Item.id.desc()).limit(60)))
        if not items:
            await core.send("Вещей пока нет. Отправь .zip с фото.")
            return
        head = " · ".join(f"{texts.STATUS_RU[st]}: {n}" for st, n in counts.items())
        lines = [texts.item_line(i) for i in items]
        chunk = f"<b>Всего: {sum(counts.values())}</b>\n{head}\n"
        for line in lines:
            if len(chunk) + len(line) > 3900:
                await core.send(chunk)
                chunk = ""
            chunk += "\n" + line
        await core.send(chunk + "\n\nОткрыть: /item N")

    @core.client.on(cmd(r"^/item\s+(\d+)"))
    async def on_item(event) -> None:
        item_id = int(event.pattern_match.group(1))
        async with session_scope() as s:
            item = await s.get(Item, item_id)
        if item is None:
            await core.send(texts.NOT_FOUND)
            return
        if item.status == ItemStatus.PENDING_APPROVAL:
            await core.show_card(item_id)
            return
        if item.pipeline_error and item.status in (ItemStatus.NEW, ItemStatus.RECOGNIZED):
            await core.send(texts.failed(item, item.pipeline_error), buttons=keyboards.failed(item_id))
            return
        from app.bot.handlers.sales import render_item

        item, text, buttons = await render_item(core, item_id)
        photos = [str(core.settings.data_dir / p.path) for p in item.photos[:10]]
        if photos:
            await core.client.send_file(core.admin, photos)
        await core.send(text, buttons=buttons)

    @core.client.on(cmd(r"^/retry\s+(\d+)"))
    async def on_retry(event) -> None:
        item_id = int(event.pattern_match.group(1))
        await core.send(f"🔁 Повторяю обработку #{item_id}…")
        core.spawn(pipeline.process_item(item_id, core))

    @core.client.on(cmd(r"^/cost\b"))
    async def on_cost(event) -> None:
        async with session_scope() as s:
            total, calls, searches = (await s.execute(
                select(func.coalesce(func.sum(AiUsage.cost_usd), 0), func.count(), func.coalesce(func.sum(AiUsage.web_searches), 0))
            )).one()
        await core.send(f"🤖 AI: ${total:.2f} · вызовов: {calls} · поисков в вебе: {searches}\n"
                        "<i>Apify считается отдельно в его кабинете.</i>")
