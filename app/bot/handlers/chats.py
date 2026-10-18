"""Chats (add, rules card), per-chat previews and publisher controls."""

import asyncio
import re

from sqlalchemy import select
from telethon import events
from telethon.extensions import html

from app.bot import keyboards, texts
from app.bot.core import BotCore, PendingInput
from app.db.enums import ApprovalKind
from app.db.models import Chat, Item, Publication
from app.db.session import session_scope
from app.services import chat_discovery, post_watch, versions
from app.services import chats as chat_service

MAX_REFS = 10
MAX_OTHERS_SHOWN = 20
ADD_PAUSE_SECONDS = 3  # between chat inspections, to stay far from Telegram flood limits


def _scan_text(found, selected, folder: str = "") -> str:
    markets = sum(1 for f in found if f.is_marketplace)
    if folder:
        head = f"🔎 В папке «{texts.escape(folder)}» новых чатов: <b>{len(found)}</b>."
        if markets < len(found):
            head += f"\n🤔 — AI сомневается, что это барахолка ({len(found) - markets} шт.), проверь их."
    else:
        head = (f"🔎 Групп и каналов у аккаунта для продаж (ещё не добавленных): <b>{len(found)}</b>, "
                f"из них похожи на барахолки: <b>{markets}</b>.\nПодсказка: <code>/scanchats Лимассол</code> — только чаты из папки.")
    lines = [head,
             "Отмеченные ✅ добавлю. Нажми на чат, чтобы снять или поставить галочку.",
             "📢 — канал: писать туда могут только админы, поэтому добавлю его чат-обсуждение, если он есть."]
    if not found:
        lines = ["🔎 Новых чатов не нашёл: всё уже добавлено."]
    return "\n".join(lines)


def _scan_buttons(found, selected, folder: str = ""):
    from telethon import Button

    if folder:
        shown = list(range(len(found)))
    else:
        shown = [i for i, f in enumerate(found) if f.is_marketplace]
        shown += [i for i, f in enumerate(found) if not f.is_marketplace][:MAX_OTHERS_SHOWN]
    rows = []
    for i in shown:
        f = found[i]
        mark = "✅" if i in selected else "▫️"
        icon = " 📢" if f.kind == "channel" else ""
        doubt = " 🤔" if folder and not f.is_marketplace else ""
        rows.append([Button.inline(f"{mark} {f.title[:38]}{icon}{doubt}", f"sc:t:{i}".encode())])
    rows.append([Button.inline(f"➕ Добавить выбранные ({len(selected)})", b"sc:add:0"),
                 Button.inline("Отмена", b"sc:x:0")])
    return rows


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

    @core.client.on(cmd(r"^/scanchats(?:\s+(.+))?$"))
    async def on_scanchats(event) -> None:
        if await need_userbot():
            return
        folder = (event.pattern_match.group(1) or "").strip()
        where = f"в папке «{texts.escape(folder)}»" if folder else "в группах и каналах аккаунта для продаж"
        status = await core.send(f"🔎 Смотрю чаты {where}…")
        try:
            known = await chat_service.known_tg_ids()
            if folder:
                found = await chat_discovery.list_folder(core.userbot, folder, known)
                if found is None:
                    names = await chat_discovery.folder_names(core.userbot)
                    await core.edit(status.id, f"Папку «{texts.escape(folder)}» не нашёл. Папки аккаунта: "
                                    + (", ".join(f"«{texts.escape(n)}»" for n in names) or "нет папок"))
                    return
            else:
                found = await chat_discovery.list_groups(core.userbot, known)
            found = await chat_discovery.classify(found)
        except Exception as e:  # noqa: BLE001
            await core.edit(status.id, f"⚠️ Не получилось: <code>{texts.escape(str(e)[:200])}</code>")
            return
        # From a folder the owner picked everything on purpose: preselect all, only flag doubtful ones.
        selected = set(range(len(found))) if folder else {i for i, f in enumerate(found) if f.is_marketplace}
        core.scan = {"found": found, "selected": selected, "auto": [], "folder": folder}
        await core.edit(status.id, _scan_text(found, selected, folder), buttons=_scan_buttons(found, selected, folder)
                        if found else None)

    @core.client.on(events.CallbackQuery(data=re.compile(rb"^sc:(\w+):(\d+)$"), func=admin_only))
    async def on_scan_button(event) -> None:
        action = event.data_match.group(1).decode()
        idx = int(event.data_match.group(2))
        scan = getattr(core, "scan", None)
        if not scan:
            await event.answer("Список устарел, запусти /scanchats ещё раз", alert=True)
            return
        found, selected = scan["found"], scan["selected"]
        if action == "t":
            folder = scan.get("folder", "")
            selected.symmetric_difference_update({idx})
            await event.edit(_scan_text(found, selected, folder), parse_mode="html",
                             buttons=_scan_buttons(found, selected, folder))
            await event.answer()
        elif action == "x":
            core.scan = None
            await event.edit("Отменено.", buttons=None)
            await event.answer()
        elif action == "add":
            picked = [found[i] for i in sorted(selected)]
            if not picked:
                await event.answer("Ничего не выбрано", alert=True)
                return
            await event.edit(f"⏳ Добавляю чаты: 0/{len(picked)}…", buttons=None)
            await event.answer()
            msg_id = event.message_id

            async def go() -> None:
                lines, auto = [], []
                for n, f in enumerate(picked, 1):
                    ref = f"@{f.username}" if f.username else f.tg_chat_id
                    try:
                        res = await chat_service.add_chat(core.userbot, ref)
                    except Exception as e:  # noqa: BLE001 - reported to the owner
                        lines.append(f"❌ {texts.escape(f.title)}: {texts.escape(type(e).__name__)}")
                        continue
                    if res.error:
                        lines.append(f"❌ {texts.escape(f.title)}: {texts.escape(res.error)}")
                    else:
                        mark = "✅" if res.auto_rules else "❓"
                        lines.append(f"{mark} {texts.escape(f.title)} → чат #{res.chat_id}"
                                     + (f" ({texts.escape(res.note)})" if res.note else ""))
                        if res.auto_rules:
                            auto.append(res.chat_id)
                    await core.edit(msg_id, f"⏳ Добавляю чаты: {n}/{len(picked)}…")
                    await asyncio.sleep(ADD_PAUSE_SECONDS)
                core.scan = {"found": [], "selected": set(), "auto": auto}
                summary = ("<b>Добавление чатов</b>\n" + "\n".join(lines) +
                           "\n\n✅ — правила понятны, ❓ — нужна твоя проверка (карточки придут по одной).")
                from telethon import Button
                buttons = [[Button.inline(f"✅ Включить все с понятными правилами ({len(auto)})", b"sc:auto:0")]] \
                    if auto else None
                await core.edit(msg_id, summary[:4000], buttons=buttons)
                await core.show_next()

            core.spawn(go())
        elif action == "auto":
            ids = list(scan.get("auto") or [])
            done = 0
            for chat_id in ids:
                try:
                    await chat_service.confirm(chat_id)
                    done += 1
                    core.make_versions(chat_id=chat_id)
                except Exception:  # noqa: BLE001 - e.g. the chat turned out to be read-only
                    continue
            scan["auto"] = []
            await event.answer(f"Включено чатов: {done}")
            await event.edit(buttons=None)
            await core.send(f"🟢 Включил чатов: {done}. Для одобренных вещей готовлю версии постов — карточки придут сюда.")
            await core.show_next()
        else:
            await event.answer()

    @core.client.on(cmd(r"^/recheckchats\b"))
    async def on_recheckchats(event) -> None:
        if await need_userbot():
            return
        ids = await chat_service.needing_recheck()
        if not ids:
            await core.send("Все чаты в порядке — перепроверять нечего.")
            return
        status = await core.send(f"🔄 Перепроверяю чаты, куда нельзя было писать: {len(ids)}…")

        async def go() -> None:
            lines = []
            for chat_id in ids:
                try:
                    res = await chat_service.recheck(core.userbot, chat_id)
                except Exception as e:  # noqa: BLE001
                    lines.append(f"❌ чат #{chat_id}: {texts.escape(type(e).__name__)}")
                    continue
                async with session_scope() as s:
                    chat = await s.get(Chat, chat_id)
                name = texts.chat_name(chat)
                if res.error:
                    lines.append(f"❌ #{chat_id} {name}: {texts.escape(res.error)}")
                elif chat.can_post:
                    if not chat.is_active:
                        async with session_scope() as s:
                            await chat_service.ensure_rules_approval(s, chat_id)
                    lines.append(f"✅ #{chat_id} {name}: теперь можно публиковать — карточка придёт ниже")
                else:
                    lines.append(f"🚫 #{chat_id} {name}: {texts.escape(chat.check_note or 'писать нельзя')}")
                await asyncio.sleep(ADD_PAUSE_SECONDS)
            await core.edit(status.id, "<b>Перепроверка</b>\n" + "\n".join(lines)[:3900])
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

    # ---------- where the posts are ----------

    @core.client.on(cmd(r"^/posts(?:[_ ]+(\d+))?\s*$"))
    async def on_posts(event) -> None:
        item_id = event.pattern_match.group(1)
        if item_id is None:
            await core.send(texts.posts_summary(await post_watch.overview()))
            return
        rows = await post_watch.overview(int(item_id))
        if not rows:
            async with session_scope() as s:
                item = await s.get(Item, int(item_id))
            if item is None:
                await core.send(texts.NOT_FOUND)
                return
            rows = [(item, [])]
        await core.send(texts.posts_detail(*rows[0]))

    @core.client.on(cmd(r"^/checkposts\b"))
    async def on_checkposts(event) -> None:
        if core.post_watcher is None:
            await core.send(texts.NEED_USERBOT)
            return
        await core.send("🔎 Проверяю, на месте ли посты во всех чатах…")

        async def go() -> None:
            res = await core.post_watcher.check_all()
            lines = [f"Проверил постов: {res.checked}. "
                     + (f"Удалено в чатах: {len(res.removals)} — подробности выше." if res.removals else "Все на месте ✅")]
            lines += [f"⚠️ {texts.escape(e)}" for e in res.errors]
            await core.send("\n".join(lines))

        core.spawn(go())

    @core.client.on(events.CallbackQuery(data=re.compile(rb"^pw:(\w+):(\d+)$"), func=admin_only))
    async def on_post_removed_button(event) -> None:
        action, pub_id = event.data_match.group(1).decode(), int(event.data_match.group(2))
        msg = await event.get_message()
        current = html.unparse(msg.message, msg.entities)  # keep the card's formatting when appending a line
        match action:
            case "again":
                ok, note = await post_watch.repost(pub_id)
                if not ok:
                    await event.answer(note, alert=True)
                    return
                await event.edit(current + f"\n\n🔁 {texts.escape(note)}", parse_mode="html", buttons=None)
            case "off":
                async with session_scope() as s:
                    chat_id = (await s.get(Publication, pub_id)).chat_id
                await chat_service.deactivate(chat_id)
                await event.edit(current + f"\n\n⏸ Чат выключен, туда больше не публикую. Вернуть: /chat {chat_id}",
                                 parse_mode="html", buttons=None)
            case _:
                await event.edit(buttons=None)
        await event.answer()

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
            case "topic":
                async with session_scope() as s:
                    chat = await s.get(Chat, chat_id)
                if not chat.forum_topics:
                    await event.answer("Тем не нашёл — нажми «Перепроверить чат»", alert=True)
                    return
                await event.edit(buttons=keyboards.topics(chat))
                await event.answer("Выбери тему, куда публиковать")
            case "card":
                await event.answer()
                if aid:
                    await core.refresh_approval(aid)
                else:
                    async with session_scope() as s:
                        chat = await s.get(Chat, chat_id)
                    await event.edit(texts.chat_card(chat), parse_mode="html", buttons=keyboards.chat(chat))
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

    @core.client.on(events.CallbackQuery(data=re.compile(rb"^ct:(\d+):(\d+)$"), func=admin_only))
    async def on_topic_pick(event) -> None:
        chat_id, topic_id = int(event.data_match.group(1)), int(event.data_match.group(2))
        try:
            title = await chat_service.set_topic(chat_id, topic_id)
        except ValueError as e:
            await event.answer(str(e), alert=True)
            return
        await event.answer(f"Тема: {title}")
        aid = await core._approval_id(ApprovalKind.CHAT_RULES, chat_id=chat_id)
        if aid:
            await core.refresh_approval(aid)
        else:
            async with session_scope() as s:
                chat = await s.get(Chat, chat_id)
            await event.edit(texts.chat_card(chat), parse_mode="html", buttons=keyboards.chat(chat))

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
