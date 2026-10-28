"""Inquiry cards and item sale actions: reserve, sold (with price), withdraw, assign item to an inquiry."""

import re
from decimal import Decimal

from sqlalchemy import select
from telethon import events

from app.bot import keyboards, texts
from app.bot.core import BotCore, PendingInput
from app.db.models import Buyer, Inquiry, Item
from app.db.session import session_scope
from app.services import inquiries, pipeline, pricing, recognition, replies, sales, sheets
from app.services.listing import fmt_eur


async def do_sale(core: BotCore, item_id: int, price: Decimal | None, buyer_id: int | None) -> None:
    try:
        res = await sales.sell(item_id, price, buyer_id)
    except sales.SaleError as e:
        await core.send(f"⚠️ {texts.escape(str(e))}")
        return
    if buyer_id:
        core.spawn(sheets.push_buyer(buyer_id))  # «Купил» in the buyers sheet
    if res.sold_out:
        posts = await sales.remove_posts(core.userbot, item_id)
    else:
        posts = await sales.refresh_posts(core.userbot, item_id)
    async with session_scope() as s:
        item = await s.get(Item, item_id)
    await core.send(texts.sale_done(item, res, posts))


async def _item_and_card(core: BotCore, item_id: int) -> Item | None:
    async with session_scope() as s:
        return await s.get(Item, item_id)


async def render_item(core: BotCore, item_id: int):
    """Text + buttons of the /item card (status line included), so edits never leave a stale status."""
    async with session_scope() as s:
        item = await recognition.load_item(s, item_id)
        research = await pricing.latest_research(s, item_id)
        text = texts.review_card(item, research, core.settings.seller_username)
    return item, f"{text}\n\nСтатус: {texts.STATUS_RU[item.status]}", keyboards.item_actions(item)


async def _refresh_item_card(core: BotCore, event, item_id: int) -> None:
    _, text, buttons = await render_item(core, item_id)
    await event.edit(text, parse_mode="html", buttons=buttons, link_preview=False)


_last_suggestion: dict[int, str] = {}  # inquiry id → last suggested text, so "another variant" differs


async def _suggest_reply(core: BotCore, inquiry_id: int) -> None:
    try:
        sug = await replies.suggest(inquiry_id, core.userbot, _last_suggestion.get(inquiry_id))
    except Exception as e:  # noqa: BLE001 - shown to the owner
        await core.send(f"⚠️ Не получилось предложить ответ: {texts.escape(type(e).__name__)}")
        return
    _last_suggestion[inquiry_id] = sug.text
    async with session_scope() as s:
        inq = await s.get(Inquiry, inquiry_id)
        item = await s.get(Item, inq.item_id) if inq.item_id else None
        buyer = await s.get(Buyer, inq.buyer_id) if inq.buyer_id else None
    await core.send(texts.reply_suggestion(item, buyer, sug), buttons=keyboards.reply_suggestion(inquiry_id, buyer))


def register(core: BotCore) -> None:
    def admin_only(e) -> bool:
        return e.sender_id == core.admin

    # ---------- inquiry cards ----------

    @core.client.on(events.CallbackQuery(data=re.compile(rb"^iq:(\w+):(\d+)(?::(\d+))?$"), func=admin_only))
    async def on_inquiry(event) -> None:
        action = event.data_match.group(1).decode()
        inq_id = int(event.data_match.group(2))
        arg = int(event.data_match.group(3)) if event.data_match.group(3) else None
        async with session_scope() as s:
            inq = await s.get(Inquiry, inq_id)
            item = await s.get(Item, inq.item_id) if inq and inq.item_id else None
        if inq is None:
            await event.answer(texts.NOT_FOUND, alert=True)
            return
        if item is None and action in ("res", "unres", "sold", "soldp", "soldx"):
            await event.answer("Сначала выбери, о какой вещи речь", alert=True)
            return

        match action:
            case "res":
                try:
                    await sales.reserve(item.id, inq.buyer_id)
                except sales.SaleError as e:
                    await event.answer(str(e), alert=True)
                    return
                await event.answer("Забронировано")
                await core.refresh_inquiry(inq_id)
            case "unres":
                try:
                    await sales.unreserve(item.id)
                except sales.SaleError as e:
                    await event.answer(str(e), alert=True)
                    return
                await event.answer("Бронь снята, вещь снова в продаже")
                await core.refresh_inquiry(inq_id)
            case "sold":
                await event.edit(buttons=keyboards.sold_price("iq", inq_id, fmt_eur(item.price)))
                await event.answer()
            case "soldp":
                await event.answer("Записываю продажу…")
                await do_sale(core, item.id, None, inq.buyer_id)
                await core.refresh_inquiry(inq_id)
            case "soldx":
                core.pending = PendingInput(kind="sold_price", item_id=item.id, extra={"buyer_id": inq.buyer_id,
                                                                                     "inquiry_id": inq_id})
                await core.send(texts.ASK_SOLD_PRICE.format(name=texts.escape(item.display_name)) + "\n\n/cancel — отмена")
                await event.answer()
            case "back":
                await core.refresh_inquiry(inq_id)
                await event.answer()
            case "pick":
                cands = await inquiries.candidate_items()
                await event.edit(buttons=keyboards.inquiry(inq, None, None, None, cands))
                await event.answer("Выбери вещь")
            case "set":
                await inquiries.assign_item(inq_id, arg)
                await event.answer("Готово")
                await core.refresh_inquiry(inq_id)
            case "reply":
                await event.answer("Пишу вариант ответа…")
                core.spawn(_suggest_reply(core, inq_id))
            case "spam":
                await inquiries.dismiss(inq_id)
                await event.edit("🚫 Сообщение не про продажу — скрыл.", buttons=None)
                await event.answer()
            case _:
                await event.answer()

    # ---------- item actions (from /item and inquiry cards) ----------

    @core.client.on(events.CallbackQuery(data=re.compile(rb"^it:(\w+):(\d+)$"), func=admin_only))
    async def on_item_action(event) -> None:
        action = event.data_match.group(1).decode()
        item_id = int(event.data_match.group(2))
        item = await _item_and_card(core, item_id)
        if item is None:
            await event.answer(texts.NOT_FOUND, alert=True)
            return

        match action:
            case "res":
                try:
                    await sales.reserve(item_id)
                except sales.SaleError as e:
                    await event.answer(str(e), alert=True)
                    return
                await event.answer("Забронировано")
                await _refresh_item_card(core, event, item_id)
            case "unres":
                try:
                    await sales.unreserve(item_id)
                except sales.SaleError as e:
                    await event.answer(str(e), alert=True)
                    return
                await event.answer("Бронь снята, вещь снова в продаже")
                await _refresh_item_card(core, event, item_id)
            case "sold":
                await event.edit(buttons=keyboards.sold_price("it", item_id, fmt_eur(item.price)))
                await event.answer()
            case "soldp":
                await event.answer("Записываю продажу…")
                await do_sale(core, item_id, None, None)
                await _refresh_item_card(core, event, item_id)
            case "soldx":
                core.pending = PendingInput(kind="sold_price", item_id=item_id)
                await core.send(texts.ASK_SOLD_PRICE.format(name=texts.escape(item.display_name)) + "\n\n/cancel — отмена")
                await event.answer()
            case "back":
                await _refresh_item_card(core, event, item_id)
                await event.answer()
            case "wd":
                await event.edit(buttons=keyboards.confirm_withdraw(item_id))
                await event.answer()
            case "wdyes":
                try:
                    await pipeline.withdraw(item_id)
                except Exception as e:  # noqa: BLE001
                    await event.answer(f"Не получилось: {e}"[:190], alert=True)
                    return
                await event.answer("Снято с продажи")
                await event.edit(buttons=None)
                posts = await sales.remove_posts(core.userbot, item_id)
                await core.send(texts.withdrawn_done(item, posts))
            case _:
                await event.answer()

    # ---------- commands ----------

    @core.client.on(events.NewMessage(from_users=[core.admin], pattern=r"^/inquiries\b"))
    async def on_inquiries(event) -> None:
        async with session_scope() as s:
            rows = (await s.execute(
                select(Inquiry, Item, Buyer).outerjoin(Item, Item.id == Inquiry.item_id)
                .outerjoin(Buyer, Buyer.id == Inquiry.buyer_id).order_by(Inquiry.id.desc()).limit(20))).all()
        if not rows:
            await core.send("Откликов пока нет.")
            return
        await core.send("<b>Последние отклики</b>\n" + "\n".join(texts.inquiry_line(i, it, b) for i, it, b in rows))

    @core.client.on(events.NewMessage(from_users=[core.admin], pattern=r"^/sheetsync\b"))
    async def on_sheetsync(event) -> None:
        if not sheets.enabled():
            await core.send("Google-таблица не подключена: задай <code>SHEETS_WEBHOOK_URL</code> и "
                            "<code>SHEETS_SECRET</code> на Railway (инструкция — в README, раздел «Google-таблица»).")
            return
        await core.send("📊 Выгружаю все отклики и покупателей в Google-таблицу…")
        try:
            n_inq, n_buyers = await sheets.sync_all()
        except Exception as e:  # noqa: BLE001 - shown to the owner
            await core.send(f"⚠️ Не получилось: {texts.escape(f'{type(e).__name__}: {e}'[:300])}")
            return
        await core.send(f"📊 Готово: откликов {n_inq}, покупателей {n_buyers}.")

    @core.client.on(events.NewMessage(from_users=[core.admin], pattern=r"^/inquiry[_ ]+(\d+)"))
    async def on_inquiry_card(event) -> None:
        inquiry_id = int(event.pattern_match.group(1))
        async with session_scope() as s:
            exists = await s.get(Inquiry, inquiry_id) is not None
        if not exists:
            await core.send("Отклик не найден. Список: /inquiries")
            return
        text, buttons = await core.render_inquiry(inquiry_id)
        await core.send(text, buttons=buttons)


async def handle_sold_price_input(core: BotCore, pending: PendingInput, text: str, event) -> None:
    from app.bot.handlers.review import parse_price

    parsed = parse_price(text)
    if not parsed:
        await event.reply(texts.BAD_PRICE, parse_mode="html")
        return
    core.pending = None
    await do_sale(core, pending.item_id, parsed[0], pending.extra.get("buyer_id"))
    if inquiry_id := pending.extra.get("inquiry_id"):
        await core.refresh_inquiry(inquiry_id)
