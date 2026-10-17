"""Sends one post (photo album + text) to a chat from the selling account."""

from pathlib import Path

from telethon import TelegramClient

from app.userbot.client import resolve_chat

CAPTION_LIMIT = 1024
ALBUM_LIMIT = 10


async def send_post(
    client: TelegramClient, tg_chat_id: int | None, username: str | None, text: str, photos: list[Path], with_photos: bool
) -> list[int]:
    """Returns ids of all sent messages. Plain text (no markdown) so AI text can't break formatting."""
    peer = await resolve_chat(client, tg_chat_id, username)
    photos = [p for p in photos if p.exists()][:ALBUM_LIMIT] if with_photos else []

    if not photos:
        msg = await client.send_message(peer, text, parse_mode=None, link_preview=False)
        return [msg.id]

    files = [str(p) for p in photos]
    if len(text) <= CAPTION_LIMIT:
        sent = await client.send_file(peer, files if len(files) > 1 else files[0], caption=text, parse_mode=None)
        sent = sent if isinstance(sent, list) else [sent]
        return [m.id for m in sent]

    # Too long for a caption: album first, then the text as a reply to it.
    sent = await client.send_file(peer, files if len(files) > 1 else files[0])
    sent = sent if isinstance(sent, list) else [sent]
    msg = await client.send_message(peer, text, parse_mode=None, link_preview=False, reply_to=sent[0].id)
    return [*(m.id for m in sent), msg.id]


def message_link(tg_chat_id: int | None, username: str | None, msg_id: int) -> str | None:
    if username:
        return f"https://t.me/{username}/{msg_id}"
    if tg_chat_id is not None and str(tg_chat_id).startswith("-100"):
        return f"https://t.me/c/{str(tg_chat_id)[4:]}/{msg_id}"
    return None
