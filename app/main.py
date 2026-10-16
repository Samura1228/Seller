"""Entry point: bot-panel + HTTP API in one asyncio loop (stage 2 adds the userbot and scheduler here)."""

import asyncio
import logging
import signal

import uvicorn

from app.api.main import api
from app.bot.core import BotCore, recover_interrupted
from app.bot.handlers import commands, intake, review
from app.config import get_settings


async def main() -> None:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    for d in (settings.photos_dir, settings.tmp_dir, settings.sessions_dir):
        d.mkdir(parents=True, exist_ok=True)

    core = BotCore(settings)
    # Order matters: the zip handler stops propagation so documents never reach the text handler.
    intake.register(core)
    commands.register(core)
    review.register(core)
    await core.client.start(bot_token=settings.bot_token)
    me = await core.client.get_me()
    logging.info("Bot started as @%s", me.username)
    if problem := settings.storage_problem():
        logging.error("Storage misconfigured: %s", problem)
        await core.send(f"⚠️ <b>Настройка хранилища:</b> {problem}")
    core.spawn(recover_interrupted(core))

    server = uvicorn.Server(uvicorn.Config(api, host="0.0.0.0", port=settings.port, log_level="warning"))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    tasks = [
        asyncio.create_task(server.serve(), name="api"),
        asyncio.create_task(core.client.run_until_disconnected(), name="bot"),
        asyncio.create_task(stop.wait(), name="stop"),
    ]
    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for t in done:
        if t.get_name() != "stop" and t.exception():
            logging.error("Task %s crashed", t.get_name(), exc_info=t.exception())
    server.should_exit = True
    await core.client.disconnect()
    for t in tasks:
        t.cancel()


if __name__ == "__main__":
    asyncio.run(main())
