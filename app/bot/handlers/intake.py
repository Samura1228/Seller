"""Receiving the zip archive (via MTProto, up to 2 GB) and running the batch pipeline."""

import logging
import time
from html import escape

from telethon import events

from app.bot import texts
from app.bot.core import BotCore
from app.db.enums import BatchStatus
from app.db.models import IntakeBatch
from app.db.session import session_scope
from app.services import intake, pipeline

log = logging.getLogger(__name__)
ZIP_MIMES = {"application/zip", "application/x-zip-compressed", "application/x-zip", "multipart/x-zip"}


def is_zip(event: events.NewMessage.Event) -> bool:
    f = event.message.file
    if not f:
        return False
    return (f.name or "").lower().endswith(".zip") or (f.mime_type or "") in ZIP_MIMES


def register(core: BotCore) -> None:
    @core.client.on(events.NewMessage(from_users=[core.admin], func=lambda e: e.message.document is not None))
    async def on_document(event: events.NewMessage.Event) -> None:
        if not is_zip(event):
            await event.reply(texts.NOT_ZIP)
            return
        core.spawn(handle_batch(core, event))
        raise events.StopPropagation


async def handle_batch(core: BotCore, event: events.NewMessage.Event) -> None:
    f = event.message.file
    async with session_scope() as s:
        batch = IntakeBatch(file_name=(f.name or "archive.zip")[:255], size_bytes=f.size)
        s.add(batch)
        await s.flush()
        batch_id = batch.id

    status = await event.reply(f"⬇️ Скачиваю архив ({texts.mb(f.size)})…", parse_mode="html")
    last_edit = 0.0

    def throttled(text: str, final: bool = False) -> None:
        nonlocal last_edit
        now = time.monotonic()
        if final or now - last_edit > 3:
            last_edit = now
            core.spawn(core.edit(status.id, text))

    zip_path = core.settings.tmp_dir / f"batch_{batch_id}.zip"
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        await event.message.download_media(
            file=str(zip_path),
            progress_callback=lambda cur, total: throttled(f"⬇️ Скачиваю: {texts.mb(cur)} / {texts.mb(total)}"),
        )

        await intake.set_batch(batch_id, status=BatchStatus.UNPACKING)
        await core.edit(status.id, "📦 Распаковываю и обрабатываю фото…")
        item_ids, report = await intake.import_zip(batch_id, zip_path, core.settings)
        await intake.set_batch(batch_id, items_found=len(item_ids), report=report, status=BatchStatus.PROCESSING)

        summary = texts.batch_report(len(item_ids), report)
        if not item_ids:
            await intake.set_batch(batch_id, status=BatchStatus.DONE)
            await core.edit(status.id, summary + "\n\nНечего обрабатывать.")
            return

        async def progress(done: int, failed: int, total: int) -> None:
            text = f"{summary}\n\n🧠 Распознаю, ищу цены, пишу тексты: {done}/{total}"
            if failed:
                text += f" · ошибок: {failed}"
            throttled(text, final=done == total)

        await core.edit(status.id, f"{summary}\n\n🧠 Распознаю, ищу цены, пишу тексты: 0/{len(item_ids)}")
        done, failed = await pipeline.run_batch(item_ids, core, core.settings.pipeline_concurrency, progress)
        await intake.set_batch(batch_id, status=BatchStatus.DONE)
        await core.send(
            f"✅ Архив «{f.name}» обработан: {done - failed} готово к подтверждению"
            + (f", {failed} с ошибкой" if failed else "")
            + f".\nВ очереди: {await core.queue_size()}. Карточки приходят по одной, /next — показать текущую."
        )
    except Exception as e:
        log.exception("Batch %s failed", batch_id)
        await intake.set_batch(batch_id, status=BatchStatus.FAILED, error=str(e)[:1000])
        await core.edit(status.id, f"❌ Не удалось обработать архив: <code>{escape(str(e)[:300])}</code>")
    finally:
        zip_path.unlink(missing_ok=True)
