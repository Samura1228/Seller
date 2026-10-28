"""/agent mode: everything the owner writes (text, photos) goes to the AI assistant until /cancel."""

import re
from decimal import Decimal

from telethon import Button, events
from telethon.extensions import html

from app.bot import texts
from app.bot.core import BotCore
from app.db.models import Item
from app.db.session import session_scope
from app.services import pipeline, sales
from app.services.agent import AgentSession
from app.services.agent_tools import Proposal
from app.services.listing import fmt_eur

MAX_MSG = 4000
ACTION_RU = {"set_price": "💶 Цена", "reserve": "🔒 Бронь", "unreserve": "🔓 Снять бронь", "mark_sold": "💰 Продано",
             "withdraw": "🗑 Снять с продажи", "edit_text": "📝 Правка текста"}

START = ("🤖 <b>Агент на связи.</b> Пиши как в обычном чате, можно присылать фото и скриншоты.\n"
         "Я вижу вещи, цены, отклики, переписку с покупателями и чаты, могу искать в интернете. "
         "Менять что-то буду только после твоего «✅».\n\n/cancel — закончить разговор.")


def _split(text: str) -> list[str]:
    parts, cur = [], ""
    for para in text.split("\n"):
        if len(cur) + len(para) + 1 > MAX_MSG:
            parts.append(cur)
            cur = ""
        cur += para + "\n"
    return [p for p in [*parts, cur] if p.strip()]


def proposal_text(p: Proposal, item: Item | None) -> str:
    name = f"#{p.item_id} {item.display_name}" if item else f"#{p.item_id}"
    detail = ""
    if p.action == "set_price":
        detail = f"\n{fmt_eur(item.price) if item else '?'} → <b>{fmt_eur(p.price)}</b>" + (
            f", минимум {fmt_eur(p.min_price)}" if p.min_price is not None else "")
    elif p.action == "mark_sold" and p.price is not None:
        detail = f"\nЗа {fmt_eur(p.price)}"
    elif p.action == "edit_text":
        detail = f"\n<i>{texts.escape(p.instruction or '')}</i>"
    return f"{ACTION_RU[p.action]} · <b>{texts.escape(name)}</b>{detail}\n{texts.escape(p.summary_ru)}"


def register(core: BotCore) -> None:
    proposals: dict[int, Proposal] = {}
    counter = {"n": 0}

    def admin_only(e) -> bool:
        return e.sender_id == core.admin

    def in_agent(e) -> bool:
        # a button that asked for input (price, text edit…) wins over the agent for the next message
        return core.agent is not None and core.pending is None and not (e.raw_text or "").startswith("/")

    @core.client.on(events.NewMessage(from_users=[core.admin], pattern=r"^/agent\b"))
    async def on_agent(event) -> None:
        core.pending = None
        core.agent = AgentSession(core.userbot)
        await core.send(START)
        raise events.StopPropagation

    @core.client.on(events.NewMessage(from_users=[core.admin], pattern=r"^/cancel\b"))
    async def on_cancel(event) -> None:
        if core.agent is not None:
            core.agent = None
            await core.send("🤖 Разговор с агентом закончен. /agent — начать новый.")
            raise events.StopPropagation  # the regular /cancel handler doesn't need to answer too

    @core.client.on(events.NewMessage(from_users=[core.admin], func=in_agent))
    async def on_message(event) -> None:
        session = core.agent
        images = []
        if event.photo or (event.document and (event.file.mime_type or "").startswith("image/")):
            images.append(await event.download_media(bytes))
        elif event.document:
            await event.reply("Пока понимаю текст и картинки. Архив с вещами отправь после /cancel.")
            raise events.StopPropagation
        async with core.client.action(core.admin, "typing"):
            try:
                reply = await session.ask(event.raw_text or "", images)
            except Exception as e:  # noqa: BLE001 - shown to the owner, the session goes on
                await event.reply(f"⚠️ Агент споткнулся: {type(e).__name__}. Попробуй ещё раз.")
                raise events.StopPropagation from None
        if core.agent is not session:  # /cancel arrived while it was thinking
            raise events.StopPropagation
        for part in _split(reply.text):
            await core.client.send_message(core.admin, part, parse_mode=None, link_preview=False)
        for p in reply.proposals:
            counter["n"] += 1
            pid = counter["n"]
            proposals[pid] = p
            async with session_scope() as s:
                item = await s.get(Item, p.item_id)
            await core.send(proposal_text(p, item), buttons=[[Button.inline("✅ Применить", f"ag:ok:{pid}".encode()),
                                                               Button.inline("❌ Не надо", f"ag:no:{pid}".encode())]])
        raise events.StopPropagation

    @core.client.on(events.CallbackQuery(data=re.compile(rb"^ag:(ok|no):(\d+)$"), func=admin_only))
    async def on_decision(event) -> None:
        action, pid = event.data_match.group(1).decode(), int(event.data_match.group(2))
        p = proposals.pop(pid, None)
        msg = await event.get_message()
        card = html.unparse(msg.message, msg.entities)
        if p is None:
            await event.answer("Это предложение уже неактуально", alert=True)
            await event.edit(buttons=None)
            return
        if action == "no":
            await event.edit(f"{card}\n\n❌ Не применено", parse_mode="html", buttons=None)
            await event.answer()
            return
        await event.answer("Применяю…")
        try:
            result = await apply(core, p)
        except Exception as e:  # noqa: BLE001
            result = f"⚠️ Не получилось: {type(e).__name__}: {e}"[:300]
        await event.edit(f"{card}\n\n{texts.escape(result)}", parse_mode="html", buttons=None)
        if core.agent is not None:  # the agent knows what happened, for the next questions
            core.agent.messages.append({"role": "user", "content": [
                {"type": "text", "text": f"[Система: владелец {'применил' if result.startswith('✅') else 'попытался применить'} "
                                         f"«{p.summary_ru}»: {result}]"}]})
            core.agent.messages.append({"role": "assistant", "content": [{"type": "text", "text": "Принято."}]})


async def apply(core: BotCore, p: Proposal) -> str:
    from app.bot.handlers.sales import do_sale

    if p.action == "set_price":
        await pipeline.set_manual_price(p.item_id, p.price, p.min_price)
        posts = await sales.refresh_posts(core.userbot, p.item_id)  # price in posts that are already out
        async with session_scope() as s:
            item = await s.get(Item, p.item_id)
        tail = f", посты обновлены: {posts.done}" if posts.done else ""
        return f"✅ Цена {fmt_eur(item.price)}, минимум {fmt_eur(item.min_price)}{tail}"
    if p.action == "reserve":
        await sales.reserve(p.item_id)
        return "✅ Забронировано"
    if p.action == "unreserve":
        await sales.unreserve(p.item_id)
        return "✅ Бронь снята"
    if p.action == "mark_sold":
        await do_sale(core, p.item_id, Decimal(p.price) if p.price is not None else None, None)
        return "✅ Продажа записана"
    if p.action == "withdraw":
        await pipeline.withdraw(p.item_id)
        posts = await sales.remove_posts(core.userbot, p.item_id)
        return f"✅ Снято с продажи, постов удалено: {posts.done}"
    if p.action == "edit_text":
        await pipeline.rewrite(p.item_id, p.instruction, core)
        return f"✅ Текст поправлен — посмотреть: /item {p.item_id}"
    return "?"
