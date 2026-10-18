"""Reads what we need to know about a buy/sell chat through the selling account."""

import re
from dataclasses import dataclass, field

from telethon import TelegramClient, errors, functions, types, utils

CYRILLIC = re.compile(r"[а-яёА-ЯЁ]")
LATIN = re.compile(r"[a-zA-Z]")


class ChatCheckError(Exception):
    """Chat can't be used at all (not found, private, user instead of chat...)."""


@dataclass
class ChatSnapshot:
    tg_chat_id: int
    username: str | None
    title: str
    kind: str  # group | channel | forum
    members_count: int | None
    about: str
    pinned_texts: list[str] = field(default_factory=list)
    pinned_media_without_text: int = 0
    recent_texts: list[str] = field(default_factory=list)
    slowmode_seconds: int | None = None
    can_post: bool = True
    can_send_photos: bool = True
    problems: list[str] = field(default_factory=list)
    linked_chat_id: int | None = None  # a channel's discussion group (marked peer id)

    def rules_source(self) -> str:
        parts = []
        if self.about:
            parts.append(f"[Chat description]\n{self.about}")
        for i, t in enumerate(self.pinned_texts, 1):
            parts.append(f"[Pinned message {i}]\n{t}")
        if self.pinned_media_without_text:
            parts.append(f"[{self.pinned_media_without_text} pinned message(s) are images/files without text]")
        return "\n\n".join(parts)


def normalize_ref(ref: str) -> str:
    ref = ref.strip().rstrip("/")
    ref = re.sub(r"^(https?://)?(www\.)?(t|telegram)\.me/", "", ref)
    if ref.startswith("+") or ref.startswith("joinchat/"):
        return f"https://t.me/{ref}"  # private invite link: Telethon resolves it if we're a member
    return "@" + ref.lstrip("@")


def detect_language(texts: list[str]) -> str:
    """ru / en / mixed by the share of messages written mostly in Cyrillic."""
    ru = en = 0
    for t in texts:
        c, latin = len(CYRILLIC.findall(t)), len(LATIN.findall(t))
        if c + latin < 10:
            continue
        if c > latin:
            ru += 1
        else:
            en += 1
    total = ru + en
    if total == 0:
        return "mixed"
    share = ru / total
    if share >= 0.65:
        return "ru"
    if share <= 0.35:
        return "en"
    return "mixed"


def _banned(rights: types.ChatBannedRights | None, attr: str) -> bool:
    return bool(rights and getattr(rights, attr, False))


async def inspect_chat(client: TelegramClient, ref: str | int, recent_limit: int = 80) -> ChatSnapshot:
    target = ref if isinstance(ref, int) else normalize_ref(ref)
    try:
        entity = await client.get_entity(target)
    except (ValueError, errors.UsernameNotOccupiedError, errors.UsernameInvalidError, errors.InviteHashExpiredError) as e:
        raise ChatCheckError(f"не найден ({e.__class__.__name__}). Проверь ссылку и что аккаунт для продаж вступил в чат") from e
    except errors.ChannelPrivateError as e:
        raise ChatCheckError("приватный чат, аккаунт для продаж в нём не состоит") from e

    if isinstance(entity, types.User):
        raise ChatCheckError("это пользователь или бот, а не чат")

    problems: list[str] = []
    can_post = can_photos = True
    slowmode = None
    members = None
    about = ""
    linked = None

    if isinstance(entity, types.Channel):
        full = await client(functions.channels.GetFullChannelRequest(entity))
        about = full.full_chat.about or ""
        slowmode = full.full_chat.slowmode_seconds or None
        members = full.full_chat.participants_count
        if entity.broadcast and getattr(full.full_chat, "linked_chat_id", None):
            linked = utils.get_peer_id(types.PeerChannel(full.full_chat.linked_chat_id))
        kind = "forum" if entity.forum else ("channel" if entity.broadcast else "group")
        if entity.left:
            can_post = False
            problems.append("аккаунт для продаж не состоит в чате — вступи вручную")
        if entity.broadcast and not (entity.creator or (entity.admin_rights and entity.admin_rights.post_messages)):
            can_post = False
            problems.append("это канал: публиковать могут только админы. Ищи привязанный чат-обсуждение")
        if entity.forum:
            can_post = False
            problems.append("чат с темами (форум) — публикацию в темы пока не поддерживаю")
        if getattr(entity, "send_paid_messages_stars", None):
            can_post = False
            problems.append("сообщения в этом чате платные (Stars)")
    elif isinstance(entity, types.Chat):
        full = await client(functions.messages.GetFullChatRequest(entity.id))
        about = full.full_chat.about or ""
        members = entity.participants_count
        kind = "group"
        if entity.left or entity.deactivated:
            can_post = False
            problems.append("аккаунт не состоит в чате или чат удалён")
    else:
        raise ChatCheckError("неизвестный тип чата")

    banned, default_banned = getattr(entity, "banned_rights", None), getattr(entity, "default_banned_rights", None)
    is_admin = bool(getattr(entity, "creator", False) or getattr(entity, "admin_rights", None))
    if not is_admin:
        if _banned(banned, "send_messages") or _banned(default_banned, "send_messages"):
            can_post = False
            problems.append("писать в чат запрещено")
        if any(_banned(r, a) for r in (banned, default_banned) for a in ("send_media", "send_photos")):
            can_photos = False
            problems.append("фото отправлять нельзя — пойдёт только текст")

    pinned_texts: list[str] = []
    pinned_media = 0
    async for m in client.iter_messages(entity, limit=5, filter=types.InputMessagesFilterPinned()):
        if m.message and m.message.strip():
            pinned_texts.append(m.message.strip()[:3000])
        elif m.media:
            pinned_media += 1

    recent: list[str] = []
    async for m in client.iter_messages(entity, limit=recent_limit):
        if m.message and m.message.strip():
            recent.append(m.message.strip()[:600])

    return ChatSnapshot(
        tg_chat_id=utils.get_peer_id(entity),
        username=getattr(entity, "username", None),
        title=entity.title,
        kind=kind,
        members_count=members,
        about=about.strip(),
        pinned_texts=pinned_texts,
        pinned_media_without_text=pinned_media,
        recent_texts=recent,
        slowmode_seconds=slowmode,
        can_post=can_post,
        can_send_photos=can_photos,
        problems=problems,
        linked_chat_id=linked,
    )
