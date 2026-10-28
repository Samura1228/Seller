"""Entry point: HTTP API, bot-panel, userbot (selling account) and publisher in one asyncio loop."""

import asyncio
import contextlib
import logging
import signal

import uvicorn

from app.api.helper import set_notifier
from app.api.main import api
from app.bot.core import BotCore, recover_interrupted
from app.bot.handlers import chats, commands, intake, marketplaces, review, sales
from app.config import get_settings
from app.db.lock import acquire_runner_lock
from app.services.post_watch import PostWatcher
from app.services.publisher import Publisher
from app.userbot.client import UserbotUnavailable, start_userbot
from app.userbot.listener import InquiryListener


async def _scan_unread(listener: InquiryListener) -> None:
    try:
        n = await listener.scan_unread()
        if n:
            logging.info("Processed %d unread private chats after start", n)
    except Exception:
        logging.exception("Unread scan failed")


async def main() -> None:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    for d in (settings.photos_dir, settings.tmp_dir, settings.sessions_dir):
        d.mkdir(parents=True, exist_ok=True)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    # 1. Health endpoint first, so Railway can switch to this deployment and stop the old one.
    server = uvicorn.Server(uvicorn.Config(api, host="0.0.0.0", port=settings.port, log_level="warning"))
    tasks = [asyncio.create_task(server.serve(), name="api"), asyncio.create_task(stop.wait(), name="stop")]

    # 2. Only one instance may run the Telegram sessions: wait until the old deployment has exited.
    lock_task = asyncio.create_task(acquire_runner_lock(settings.database_url))
    await asyncio.wait([lock_task, tasks[1]], return_when=asyncio.FIRST_COMPLETED)
    if stop.is_set():
        lock_task.cancel()
        server.should_exit = True
        return
    lock_conn = lock_task.result()

    # 3. Bot-panel.
    core = BotCore(settings)
    intake.register(core)  # registered first: the zip handler stops propagation before the text handler
    commands.register(core)
    review.register(core)
    chats.register(core)
    sales.register(core)
    marketplaces.register(core)
    set_notifier(core)  # the helper's API reports filled forms / published ads through the bot
    await core.client.start(bot_token=settings.bot_token)
    me = await core.client.get_me()
    logging.info("Bot started as @%s", me.username)
    if problem := settings.storage_problem():
        logging.error("Storage misconfigured: %s", problem)
        await core.send(f"⚠️ <b>Настройка хранилища:</b> {problem}")

    # 4. Userbot + publisher (optional until USERBOT_SESSION is set).
    try:
        core.userbot = await start_userbot(settings)
    except UserbotUnavailable as e:
        logging.error("Userbot unavailable: %s", e)
        await core.send(f"⚠️ <b>Аккаунт для продаж:</b> {e}")
    except Exception as e:  # noqa: BLE001 - keep the bot-panel alive
        logging.exception("Userbot failed to start")
        await core.send(f"⚠️ <b>Аккаунт для продаж</b> не подключился: <code>{type(e).__name__}: {str(e)[:200]}</code>")
    if core.userbot is not None:
        listener = InquiryListener(core.userbot, core)
        listener.register()
        core.spawn(_scan_unread(listener))
        core.publisher = Publisher(settings, core.userbot, core)
        tasks.append(asyncio.create_task(core.publisher.run(), name="publisher"))
        core.post_watcher = PostWatcher(settings, core.userbot, core)
        tasks.append(asyncio.create_task(core.post_watcher.run(), name="post_watcher"))
        tasks.append(asyncio.create_task(core.userbot.run_until_disconnected(), name="userbot"))

    tasks.append(asyncio.create_task(core.client.run_until_disconnected(), name="bot"))
    core.spawn(recover_interrupted(core))

    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for t in done:
        if t.get_name() != "stop" and not t.cancelled() and t.exception():
            logging.error("Task %s crashed", t.get_name(), exc_info=t.exception())
    server.should_exit = True
    for t in tasks:
        t.cancel()
    await core.client.disconnect()
    if core.userbot is not None:
        await core.userbot.disconnect()
    with contextlib.suppress(Exception):
        await lock_conn.close()
    if not stop.is_set():
        raise SystemExit(1)  # a component died unexpectedly: non-zero exit so Railway restarts the service


if __name__ == "__main__":
    asyncio.run(main())
