"""Approval card buttons and the owner's typed answers (price, quantity, corrections, text edits)."""

import re
from decimal import Decimal

from sqlalchemy import select
from telethon import events

from app.bot import keyboards, texts
from app.bot.core import BotCore, PendingInput
from app.db.enums import Actor, ApprovalKind, ApprovalStatus, ItemStatus
from app.db.models import Approval, Item
from app.db.session import session_scope
from app.services import pipeline, versions
from app.services.items import set_status

PRICE_RE = re.compile(r"^\s*€?\s*(\d{1,6}(?:[.,]\d{1,2})?)\s*€?\s*(?:[/\\\s-]+\s*€?\s*(\d{1,6}(?:[.,]\d{1,2})?)\s*€?)?\s*$")


def parse_price(text: str) -> tuple[Decimal, Decimal | None] | None:
    m = PRICE_RE.match(text)
    if not m:
        return None
    price = Decimal(m.group(1).replace(",", "."))
    floor = Decimal(m.group(2).replace(",", ".")) if m.group(2) else None
    if price <= 0:
        return None
    return price, floor


async def _item_name(item_id: int) -> str | None:
    async with session_scope() as s:
        item = await s.get(Item, item_id)
        return texts.escape(item.display_name) if item else None


def register(core: BotCore) -> None:
    def admin_only(e) -> bool:
        return e.sender_id == core.admin

    @core.client.on(events.CallbackQuery(data=re.compile(rb"^rv:(\w+):(\d+)$"), func=admin_only))
    async def on_review(event: events.CallbackQuery.Event) -> None:
        action = event.data_match.group(1).decode()
        item_id = int(event.data_match.group(2))
        name = await _item_name(item_id)
        if name is None:
            await event.answer(texts.NOT_FOUND, alert=True)
            return
        if item_id in core.busy and action not in {"later"}:
            await event.answer(texts.BUSY)
            return

        match action:
            case "ok":
                try:
                    await pipeline.approve(item_id)
                except Exception as e:  # noqa: BLE001 - shown to the owner
                    await event.answer(f"Не получилось: {e}", alert=True)
                    return
                async with session_scope() as s:
                    item = await s.get(Item, item_id)
                    verdict = texts.resolved_card(item, "✅ Утверждено, готово к публикации")
                await event.edit(verdict, parse_mode="html", buttons=None)
                await event.answer("Утверждено")
                await core.show_next()
                core.make_versions(item_id=item_id)  # per-chat versions for active chats (no-op if none)

            case "qty":
                await event.edit(buttons=keyboards.quantity(item_id))
                await event.answer()

            case "price" | "fix" | "edit":
                core.pending = PendingInput(kind=action, item_id=item_id)
                prompt = {"price": texts.ASK_PRICE, "fix": texts.ASK_FIX, "edit": texts.ASK_EDIT}[action]
                await core.send(prompt.format(name=name) + "\n\n/cancel — отмена")
                await event.answer()

            case "reprice":
                core.run_on_item(item_id, lambda: pipeline.reprice(item_id, core))
                await event.answer("Ищу цены заново…")

            case "regen":
                core.run_on_item(item_id, lambda: pipeline.rewrite(item_id, None, core))
                await event.answer("Пишу новый текст…")

            case "later":
                async with session_scope() as s:
                    approval = await s.scalar(select(Approval).where(
                        Approval.item_id == item_id, Approval.kind == ApprovalKind.ITEM_REVIEW,
                        Approval.status == ApprovalStatus.PENDING,
                    ))
                    if approval:
                        approval.status = ApprovalStatus.SKIPPED
                        approval.bot_message_id = None
                await event.edit(f"<b>#{item_id} · {name}</b>\n⏭ Отложено — вернётся в конце очереди",
                                 parse_mode="html", buttons=None)
                await event.answer()
                await core.show_next()

            case "drop":
                await event.edit(buttons=keyboards.confirm_drop(item_id))
                await event.answer()

            case "dropno":
                await event.edit(buttons=keyboards.review(item_id))
                await event.answer()

            case "dropyes":
                try:
                    await pipeline.withdraw(item_id)
                except Exception as e:  # noqa: BLE001
                    await event.answer(f"Не получилось: {e}", alert=True)
                    return
                await event.edit(f"<b>#{item_id} · {name}</b>\n🗑 Снята с продажи", parse_mode="html", buttons=None)
                await event.answer()
                await core.show_next()

            case "reopen":
                async with session_scope() as s:
                    item = await s.get(Item, item_id)
                    if item.status != ItemStatus.READY:
                        await event.answer("Вернуть на правку можно только готовую вещь", alert=True)
                        return
                    await set_status(s, item, ItemStatus.PENDING_APPROVAL, Actor.ME, "reopened for edits")
                    s.add(Approval(item_id=item_id, kind=ApprovalKind.ITEM_REVIEW))
                await versions.drop_unposted(item_id)
                await event.edit(buttons=None)
                await event.answer()
                await core.show_card(item_id, with_photos=False)

            case _:
                await event.answer()

    @core.client.on(events.CallbackQuery(data=re.compile(rb"^qty:(\w+):(\d+)(?::(\d+))?$"), func=admin_only))
    async def on_qty(event: events.CallbackQuery.Event) -> None:
        action = event.data_match.group(1).decode()
        item_id = int(event.data_match.group(2))
        if action == "set":
            await pipeline.set_quantity(item_id, int(event.data_match.group(3)))
            await core.refresh_card(item_id)
            await event.answer("Количество обновлено")
        elif action == "other":
            core.pending = PendingInput(kind="qty", item_id=item_id)
            await core.send(texts.ASK_QTY_NUMBER + "\n\n/cancel — отмена")
            await event.answer()
        else:
            await event.edit(buttons=keyboards.review(item_id))
            await event.answer()

    @core.client.on(events.CallbackQuery(data=re.compile(rb"^rt:go:(\d+)$"), func=admin_only))
    async def on_retry(event: events.CallbackQuery.Event) -> None:
        item_id = int(event.data_match.group(1))
        await event.edit(buttons=None)
        await event.answer("Повторяю обработку…")
        core.spawn(pipeline.process_item(item_id, core))

    @core.client.on(events.NewMessage(
        from_users=[core.admin],
        func=lambda e: bool(e.raw_text) and not e.raw_text.startswith("/") and e.message.document is None,
    ))
    async def on_text(event: events.NewMessage.Event) -> None:
        if event.is_reply and await _reply_to_versions(core, event):
            return
        pending = core.pending
        if pending is None:
            await event.reply(texts.NO_PENDING_INPUT)
            return
        text = event.raw_text.strip()
        item_id = pending.item_id

        if pending.kind == "price":
            parsed = parse_price(text)
            if not parsed:
                await event.reply(texts.BAD_PRICE, parse_mode="html")
                return
            core.pending = None
            await pipeline.set_manual_price(item_id, *parsed)
            await core.show_card(item_id, with_photos=False)

        elif pending.kind == "qty":
            if not text.isdigit() or not 1 <= int(text) <= 999:
                await event.reply(texts.BAD_QTY)
                return
            core.pending = None
            await pipeline.set_quantity(item_id, int(text))
            await core.show_card(item_id, with_photos=False)

        elif pending.kind == "sold_price":
            from app.bot.handlers.sales import handle_sold_price_input

            await handle_sold_price_input(core, pending, text, event)

        elif pending.kind == "chat_rules":
            from app.bot.handlers.chats import apply_rules_edit

            core.pending = None
            started = apply_rules_edit(core, item_id, text)
            await event.reply("⏳ Применяю, карточка чата обновится ниже…" if started else texts.BUSY)

        elif pending.kind in ("versions_edit", "version_edit"):
            from app.bot.handlers.chats import apply_versions_edit

            core.pending = None
            started = apply_versions_edit(core, item_id, text, pending.extra.get("publication_id"))
            await event.reply("⏳ Применяю, версии обновятся ниже…" if started else texts.BUSY)

        elif pending.kind in ("fix", "edit"):
            core.pending = None
            if pending.kind == "fix":
                started = core.run_on_item(item_id, lambda: pipeline.correct(item_id, text, core), move_down=True)
            else:
                started = core.run_on_item(item_id, lambda: pipeline.rewrite(item_id, text, core), move_down=True)
            await event.reply("⏳ Применяю, карточка обновится ниже…" if started else texts.BUSY)


ALL_VERSIONS = re.compile(r"во\s+всех|всех\s+(верси|объявлен|пост|текст)|все\s+(верси|объявлен|пост|текст)|везде|\ball\b", re.I)


async def _reply_to_versions(core: BotCore, event) -> bool:
    """A reply to a version message edits that version; a reply to the versions card edits all of them."""
    from app.bot.handlers.chats import apply_versions_edit

    reply_id, text = event.reply_to_msg_id, event.raw_text.strip()
    pub = await versions.find_draft_by_preview(reply_id)
    if pub is not None:
        item_id, pub_id = pub.item_id, (None if ALL_VERSIONS.search(text) else pub.id)
    else:
        async with session_scope() as s:
            approval = await s.scalar(select(Approval).where(
                Approval.kind == ApprovalKind.CHAT_PREVIEWS, Approval.bot_message_id == reply_id,
                Approval.status == ApprovalStatus.PENDING))
        if approval is None:
            return False
        item_id, pub_id = approval.item_id, None
    started = apply_versions_edit(core, item_id, text, pub_id)
    what = "эту версию" if pub_id else "все версии"
    await event.reply(f"⏳ Правлю {what}, обновлённые версии придут ниже…" if started else texts.BUSY)
    return True
