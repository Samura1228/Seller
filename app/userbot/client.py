"""Userbot on the selling account (MTProto user session). Used for chats, publishing and, later, inquiries."""

import logging

from telethon import TelegramClient
from telethon.sessions import StringSession

from app.config import Settings

log = logging.getLogger(__name__)


class UserbotUnavailable(Exception):
    pass


async def start_userbot(settings: Settings) -> TelegramClient | None:
    """Connects with USERBOT_SESSION. Never prompts for codes: the session is generated locally."""
    if not settings.userbot_session:
        return None
    client = TelegramClient(
        StringSession(settings.userbot_session), settings.tg_api_id, settings.tg_api_hash,
        device_model="Limassol Seller", system_version="server", app_version="1.0",
        flood_sleep_threshold=0,  # never sleep silently on FloodWait: the publisher handles it explicitly
    )
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise UserbotUnavailable("USERBOT_SESSION недействительна — сгенерируй новую через scripts/make_session.py")
    me = await client.get_me()
    # StringSession keeps no entity cache between restarts: load dialogs so chats can be resolved by id.
    await client.get_dialogs(limit=None)
    log.info("Userbot started as @%s (id %s)", me.username, me.id)
    return client


async def resolve_chat(client: TelegramClient, tg_chat_id: int | None, username: str | None):
    """Input entity for a stored chat: by id from the dialog cache, falling back to @username."""
    if tg_chat_id is not None:
        try:
            return await client.get_input_entity(tg_chat_id)
        except ValueError:
            pass
    if username:
        return await client.get_input_entity(f"@{username}")
    raise ValueError("chat cannot be resolved: not in dialogs and has no username")
