"""Chats (add, rules card), per-chat previews and publisher controls."""

import re

from sqlalchemy import select
from telethon import events

from app.bot import keyboards, texts
from app.bot.core import BotCore, PendingInput
from app.db.enums import ApprovalKind
from app.db.models import Chat, Item
from app.db.session import session_scope
from app.services import chats as chat_service
from app.services import versions

MAX_REFS = 10


def apply_rules_edit(core: BotCore, chat_id: int, correction: str) -> bool:
    return core.run_on(
        ("chat", chat_id),
        lambda: core._approval_id(ApprovalKind.CHAT_RULES, chat_id=chat_id),
        lambda: chat_service.edit_rules(chat_id, correction),
        move_down=True,
    )


async def _open_chat_card(core: BotCore, chat_id: int) -> None:
    """Shows the chat card (creating an approval for it if the chat has none open)."""
    async with session_scope() as s:
        await chat_service.ensure_rules_approval(s, chat_id)
    aid = await core._approval_id(ApprovalKind.CHAT_RULES, chat_id=chat_id)
    await core.show_approval(aid, with_photos=False)


def register(core: BotCore) -> None:
    def admin_only(e) -> bool:
        return e.sender_id == core.admin

    def cmd(pattern: str):
        return events.NewMessage(from_users=[core.admin], pattern=pattern)

    async def need_userbot() -> bool:
        if core.userbot is None:
            await core.send(texts.NEED_USERBOT)
            return True
        return False

    # ---------- commands ----------

    @core.client.on(cmd(r"^/addchat(?:\s+(.+))?$"))
    async def on_addchat(event) -> None:
        if await need_userbot():
            return
        raw = (event.pattern_match.group(1) or "").replace(",", " ").split()
        refs = list(dict.fromkeys(raw))[:MAX_REFS]
        if not refs:
            await core.send(texts.ADDCHAT_HELP)
            return
        status = await core.send(f"🔎 Проверяю чаты: {len(refs)}…")

        async def go() -> None:
            lines = []
            for ref in refs:
                try:
                    res = await chat_service.add_chat(core.userbot, ref)
                except Exception as e:  # noqa: BLE001 - reported to the owner
                    lines.append(f"❌ {texts.escape(ref)}: {texts.escape(type(e).__name__)}: {texts.escape(str(e)[:150])}")
                    continue
                lines.append(f"❌ {texts.escape(ref)}: {texts.escape(res.error)}" if res.error
                             else f"✅ {texts.escape(ref)} → чат #{res.chat_id}")
            await core.edit(status.id, "<b>Чаты</b>\n" + "\n".join(lines) +
                            "\n\nКарточки с правилами приходят по одной — проверь и подтверди.")
            await core.show_next()

        core.spawn(go())

    @core.client.on(cmd(r"^/chats\b"))
    async def on_chats(event) -> None:
        async with session_scope() as s:
            rows = list(await s.scalars(select(Chat).order_by(Chat.id)))
        if not rows:
            await core.send("Чатов пока нет.\n" + texts.ADDCHAT_HELP)
            return
        await core.send("<b>Чаты</b>\n" + "\n".join(texts.chat_line(c) for c in rows) +
                        "\n\nОткрыть: /chat N · добавить: /addchat @username")

    @core.client.on(cmd(r"^/chat\s+(\d+)"))
    async def on_chat(event) -> None:
        chat_id = int(event.pattern_match.group(1))
        async with session_scope() as s:
            exists = await s.get(Chat, chat_id) is not None
        if not exists:
            await core.send("Чат не найден. Список: /chats")
            return
        await _open_chat_card(core, chat_id)

    @core.client.on(cmd(r"^/publisher\b"))
    async def on_publisher(event) -> None:
        if core.publisher is None:
            await core.send(texts.NEED_USERBOT)
            return
        st = await core.publisher.status()
        await core.send(texts.publisher_status(st, core.settings), buttons=keyboards.publisher(st.enabled))

    @core.client.on(cmd(r"^/versions\s+(\d+)"))
    async def on_versions(event) -> None:
        """Re-create per-chat versions for an item (e.g. after discarding them)."""
        item_id = int(event.pattern_match.group(1))
        async with session_scope() as s:
            if await s.get(Item, item_id) is None:
                await core.send(texts.NOT_FOUND)
                return
        await core.send(f"⏳ Готовлю версии постов для #{item_id}…")
        core.make_versions(item_id=item_id)

    # ---------- chat card ----------

    @core.client.on(events.CallbackQuery(data=re.compile(rb"^ch:(\w+):(\d+)$"), func=admin_only))
    async def on_chat_button(event) -> None:
        action = event.data_match.group(1).decode()
        chat_id = int(event.data_match.group(2))
        key = ("chat", chat_id)
        if key in core.busy and action != "later":
            await event.answer(texts.BUSY)
            return
        aid = await core._approval_id(ApprovalKind.CHAT_RULES, chat_id=chat_id)

        match action:
            case "ok":
                try:
                    chat = await chat_service.confirm(chat_id)
                except Exception as e:  # noqa: BLE001
                    await event.answer(str(e)[:190], alert=True)
                    return
                await event.edit(f"<b>Чат #{chat.id} · {texts.chat_name(chat)}</b>\n🟢 Правила подтверждены, публикую сюда",
                                 parse_mode="html", buttons=None)
                await event.answer("Чат включён")
                await core.show_next()
                core.make_versions(chat_id=chat_id)
            case "edit":
                async with session_scope() as s:
                    chat = await s.get(Chat, chat_id)
                core.pending = PendingInput(kind="chat_rules", item_id=chat_id)
                await core.send(texts.ASK_CHAT_RULES.format(name=texts.chat_name(chat)) + "\n\n/cancel — отмена")
                await event.answer()
            case "lang":
                lang = await chat_service.cycle_language(chat_id)
                await event.answer(f"Язык постов: {texts.LANG_RU[lang.value]}")
                if aid:
                    await core.refresh_approval(aid)
            case "recheck":
                if core.userbot is None:
                    await event.answer("Нет сессии аккаунта для продаж", alert=True)
                    return
                await event.answer("Перепроверяю…")
                core.run_on(key, lambda: core._approval_id(ApprovalKind.CHAT_RULES, chat_id=chat_id),
                            lambda: chat_service.recheck(core.userbot, chat_id))
            case "later":
                if aid:
                    await core.skip(aid)
                await event.edit(buttons=None)
                await event.answer("Отложено")
                await core.show_next()
            case "done":
                await chat_service.close_card(chat_id)
                await event.edit(buttons=None)
                await event.answer()
                await core.show_next()
            case "off":
                await chat_service.deactivate(chat_id)
                async with session_scope() as s:
                    chat = await s.get(Chat, chat_id)
                await event.edit(f"<b>Чат #{chat.id} · {texts.chat_name(chat)}</b>\n⚪️ Не публикую сюда. Вернуть: /chat {chat.id}",
                                 parse_mode="html", buttons=None)
                await event.answer()
                await core.show_next()
            case _:
                await event.answer()

    # ---------- previews card ----------

    @core.client.on(events.CallbackQuery(data=re.compile(rb"^pv:(\w+):(\d+)$"), func=admin_only))
    async def on_preview_button(event) -> None:
        action = event.data_match.group(1).decode()
        item_id = int(event.data_match.group(2))
        key = ("pv", item_id)
        if key in core.busy and action != "later":
            await event.answer(texts.BUSY)
            return
        aid = await core._approval_id(ApprovalKind.CHAT_PREVIEWS, item_id=item_id)

        match action:
            case "ok":
                n = await versions.approve(item_id)
                await event.edit(f"<b>#{item_id}</b> ✅ Версии одобрены ({n}) — посты в очереди публикации",
                                 parse_mode="html", buttons=None)
                await event.answer("Одобрено")
                await core.show_next()
            case "regen":
                await event.answer("Переписываю…")
                core.run_on(key, lambda: core._approval_id(ApprovalKind.CHAT_PREVIEWS, item_id=item_id),
                            lambda: versions.regenerate(item_id))
            case "later":
                if aid:
                    await core.skip(aid)
                await event.edit(buttons=None)
                await event.answer("Отложено")
                await core.show_next()
            case "drop":
                await versions.discard(item_id)
                await event.edit(f"<b>#{item_id}</b> 🗑 Версии удалены. Создать заново: /versions {item_id}",
                                 parse_mode="html", buttons=None)
                await event.answer()
                await core.show_next()
            case _:
                await event.answer()

    # ---------- publisher ----------

    @core.client.on(events.CallbackQuery(data=re.compile(rb"^pb:(\w+)$"), func=admin_only))
    async def on_publisher_button(event) -> None:
        if core.publisher is None:
            await event.answer("Нет сессии аккаунта для продаж", alert=True)
            return
        action = event.data_match.group(1).decode()
        if action in ("on", "off"):
            await core.publisher.set_enabled(action == "on")
            await event.answer("Включено" if action == "on" else "Выключено")
        elif action == "now":
            await event.answer("Публикую…")
            result = await core.publisher.tick(force=True)
            await core.send(f"📤 {texts.escape(result)}")
        else:
            await event.answer()
        st = await core.publisher.status()
        await event.edit(texts.publisher_status(st, core.settings), parse_mode="html",
                         buttons=keyboards.publisher(st.enabled))
