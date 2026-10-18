"""Finds buy/sell chats among the selling account's dialogs."""

from dataclasses import dataclass

from telethon import TelegramClient, functions, types, utils

from app.ai import prompts
from app.ai.client import structured
from app.ai.schemas import MarketplaceList
from app.config import get_settings

MAX_DIALOGS = 300
MAX_CANDIDATES = 60


@dataclass
class Found:
    tg_chat_id: int
    title: str
    username: str | None
    kind: str  # group | channel | forum
    members: int | None
    is_marketplace: bool = False
    reason: str = ""


async def list_groups(client: TelegramClient, known: set[int]) -> list[Found]:
    """Groups and channels the account is in, except chats already added."""
    out: list[Found] = []
    async for d in client.iter_dialogs(limit=MAX_DIALOGS):
        e = d.entity
        if isinstance(e, types.Channel):
            kind = "forum" if e.forum else ("channel" if e.broadcast else "group")
        elif isinstance(e, types.Chat) and not e.deactivated:
            kind = "group"
        else:
            continue
        peer_id = utils.get_peer_id(e)
        if peer_id in known:
            continue
        out.append(Found(tg_chat_id=peer_id, title=e.title or "", username=getattr(e, "username", None), kind=kind,
                         members=getattr(e, "participants_count", None)))
    return out


async def classify(found: list[Found]) -> list[Found]:
    """Marks marketplaces with one cheap AI call. Marketplaces first, then the rest (for manual picking)."""
    found = found[:MAX_CANDIDATES * 2]
    if not found:
        return []
    listing = "\n".join(
        f"[{i}] {f.title} | @{f.username or '-'} | {f.kind} | members: {f.members or '?'}" for i, f in enumerate(found)
    )
    res = await structured(purpose="marketplace_classify", output=MarketplaceList,
                           system=prompts.load("marketplace_classify"), content=listing,
                           model=get_settings().ai_model_fast, max_tokens=4000)
    for v in res.verdicts:
        if 0 <= v.id < len(found):
            found[v.id].is_marketplace = v.is_marketplace
            found[v.id].reason = v.reason_ru
    found.sort(key=lambda f: (not f.is_marketplace, -(f.members or 0)))
    return found[:MAX_CANDIDATES]


def _title(f) -> str:
    t = getattr(f, "title", "")
    return (getattr(t, "text", t) or "").strip()


async def folders(client: TelegramClient) -> list:
    """The account's chat folders (DialogFilter / shared DialogFilterChatlist), without the default 'All'."""
    res = await client(functions.messages.GetDialogFiltersRequest())
    items = getattr(res, "filters", res)
    return [f for f in items if isinstance(f, (types.DialogFilter, types.DialogFilterChatlist))]


async def folder_names(client: TelegramClient) -> list[str]:
    return [_title(f) for f in await folders(client)]


def find_folder(items: list, name: str):
    want = name.strip().casefold()
    exact = [f for f in items if _title(f).casefold() == want]
    if exact:
        return exact[0]
    partial = [f for f in items if want in _title(f).casefold()]
    return partial[0] if len(partial) == 1 else None


async def list_folder(client: TelegramClient, name: str, known: set[int]) -> list[Found] | None:
    """Groups/channels from one chat folder (e.g. «Лимассол»). None if there is no such folder."""
    folder = find_folder(await folders(client), name)
    if folder is None:
        return None
    wanted = {utils.get_peer_id(p) for p in [*folder.pinned_peers, *folder.include_peers]}
    excluded = {utils.get_peer_id(p) for p in getattr(folder, "exclude_peers", [])}
    by_groups, by_channels = bool(getattr(folder, "groups", False)), bool(getattr(folder, "broadcasts", False))
    out = []
    for f in await list_groups(client, known):
        in_folder = f.tg_chat_id in wanted or (by_groups and f.kind != "channel") or (by_channels and f.kind == "channel")
        if in_folder and f.tg_chat_id not in excluded:
            out.append(f)
    return out
