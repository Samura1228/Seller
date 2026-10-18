"""Reads what we need to know about a buy/sell chat through the selling account."""

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

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
    forum_topics: list[dict] = field(default_factory=list)  # [{id, title, closed}]
    guessed_topic_id: int | None = None

    def rules_source(self) -> str:
        parts = []
        if self.forum_topics:
            parts.append("[Forum topics]\n" + "\n".join(
                f"[{t['id']}] {t['title']}" + (" (closed)" if t.get("closed") else "") for t in self.forum_topics))
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


def _forever(until: datetime | None) -> bool:
    # Telegram: 0 (Telethon gives the 1970 epoch) or more than a year ahead means "forever".
    return until is None or until.year <= 1970 or until > datetime.now(UTC) + timedelta(days=366)


def _in_force(rights: types.ChatBannedRights | None) -> types.ChatBannedRights | None:
    if rights is None or _forever(rights.until_date) or rights.until_date > datetime.now(UTC):
        return rights
    return None  # a temporary restriction that has already expired


def _until(rights: types.ChatBannedRights) -> str:
    if _forever(rights.until_date):
        return "навсегда"
    return "до " + rights.until_date.astimezone(ZoneInfo("Asia/Nicosia")).strftime("%d.%m %H:%M")


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

    # These are Telegram's own permissions for the selling account — not the chat's written rules.
    mine = _in_force(getattr(entity, "banned_rights", None))  # restriction on this account only
    everyone = _in_force(getattr(entity, "default_banned_rights", None))  # chat-wide setting
    is_admin = bool(getattr(entity, "creator", False) or getattr(entity, "admin_rights", None))
    if not is_admin:
        if _banned(everyone, "send_messages"):
            can_post = False
            problems.append("Telegram: в этом чате писать запрещено для всех участников, могут только админы "
                            "(объявления там, видимо, публикуются через админа или бота — смотри закреп)")
        elif _banned(mine, "send_messages"):
            can_post = False
            problems.append(f"Telegram: аккаунту для продаж лично запрещено писать в чат ({_until(mine)}). "
                            "Обычно это капча-бот при вступлении или мут от админа: открой чат с аккаунта для продаж, "
                            "пройди проверку или напиши админу, потом нажми «🔄 Перепроверить чат»")
        muted = _banned(everyone, "send_messages") or _banned(mine, "send_messages")  # already explained above
        if any(_banned(everyone, a) for a in ("send_media", "send_photos")):
            can_photos = False
            if not muted:
                problems.append("Telegram: фото в этом чате запрещены для всех участников — пойдёт только текст")
        elif any(_banned(mine, a) for a in ("send_media", "send_photos")):
            can_photos = False
            if not muted:
                problems.append(f"Telegram: аккаунту для продаж лично запрещено отправлять фото ({_until(mine)}) — "
                                "пока пойдёт только текст; после снятия ограничения нажми «🔄 Перепроверить чат»")

    pinned_texts: list[str] = []
    pinned_media = 0
    async for m in client.iter_messages(entity, limit=5, filter=types.InputMessagesFilterPinned()):
        if m.message and m.message.strip():
            pinned_texts.append(m.message.strip()[:3000])
        elif m.media:
            pinned_media += 1

    topics: list[dict] = []
    guessed = None
    if isinstance(entity, types.Channel) and entity.forum:
        res = await client(functions.messages.GetForumTopicsRequest(
            peer=entity, offset_date=None, offset_id=0, offset_topic=0, limit=50))
        topics = [{"id": t.id, "title": t.title, "closed": bool(t.closed)}
                  for t in res.topics if isinstance(t, types.ForumTopic)]
        guessed = guess_sale_topic(topics)
        rules_topic = next((t for t in topics if RULES_TOPIC.search(t["title"])), None)
        if rules_topic:  # rules often live in their own topic rather than in pinned messages
            async for m in client.iter_messages(entity, reply_to=rules_topic["id"], limit=5):
                if m.message and m.message.strip():
                    pinned_texts.append(m.message.strip()[:3000])
        if guessed is None:
            problems.append("чат с темами: выбери тему для объявлений в карточке чата")

    recent: list[str] = []
    recent_kwargs = {"reply_to": guessed} if guessed else {}
    async for m in client.iter_messages(entity, limit=recent_limit, **recent_kwargs):
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
        forum_topics=topics,
        guessed_topic_id=guessed,
    )


SALE_TOPIC = re.compile(r"барахол|продам|продаж|продаю|объявлен|куплю|отдам|market|sell|sale|buy|ad[s]?\b|вещ", re.I)
RULES_TOPIC = re.compile(r"правил|rules", re.I)


def guess_sale_topic(topics: list[dict]) -> int | None:
    """The open topic for sale ads, by title; None if unclear (the owner picks it in the chat card)."""
    open_topics = [t for t in topics if not t.get("closed")]
    for t in open_topics:
        if SALE_TOPIC.search(t["title"]) and not RULES_TOPIC.search(t["title"]):
            return t["id"]
    return None
