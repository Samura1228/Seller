"""Online marketplaces (Bazaraki, Kyma.cy, Facebook Marketplace) through the helper on the owner's Mac.

The server keeps the queue and the pacing; the helper asks for a task, fills the platform's form in a real
browser, sends a screenshot, and presses "Publish" only after the owner confirms in the bot.

ExternalListing.status: waiting → filling → filled → approved → published
                        (→ cancelled / failed);  published → removal_needed → removing → removed.
"""

import json
import logging
import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.ai import prompts
from app.ai.client import structured
from app.ai.schemas import FormPlan, MarketplaceText
from app.config import get_settings
from app.db.enums import ExternalPlatform, ExternalStatus, ItemStatus, Language, TextPlatform
from app.db.models import ExternalListing, Item, ItemText
from app.db.session import session_scope
from app.services import settings_store
from app.services.copywriting import master_text, text_rules_block
from app.services.listing import fmt_eur, strip_emoji
from app.services.recognition import item_facts

log = logging.getLogger(__name__)

PLATFORMS = (ExternalPlatform.BAZARAKI, ExternalPlatform.KYMA, ExternalPlatform.FACEBOOK)
NAMES = {ExternalPlatform.BAZARAKI: "Bazaraki", ExternalPlatform.KYMA: "Kyma.cy",
         ExternalPlatform.FACEBOOK: "Facebook Marketplace"}
# Bazaraki and Facebook are read by Cypriots and expats alike (Bazaraki also machine-translates ads for readers);
# Kyma's audience is largely Russian-speaking.
LANGUAGE = {ExternalPlatform.BAZARAKI: Language.EN, ExternalPlatform.KYMA: Language.RU,
            ExternalPlatform.FACEBOOK: Language.EN}
TEXT_PLATFORM = {ExternalPlatform.BAZARAKI: TextPlatform.BAZARAKI, ExternalPlatform.KYMA: TextPlatform.KYMA,
                 ExternalPlatform.FACEBOOK: TextPlatform.FACEBOOK}
TITLE_LIMIT = {ExternalPlatform.BAZARAKI: 70, ExternalPlatform.KYMA: 80, ExternalPlatform.FACEBOOK: 100}
CATEGORIES = {
    ExternalPlatform.BAZARAKI: (
        "Mobile phones, communication; Kids' stuff; Clothes, footwear, accessories; Computers, gaming; "
        "Electronics, appliances (DVD, TVs, videos / Audio, stereo / Kitchen appliances / Home appliances / "
        "Cameras, camcorders / Health, beauty appliances / Air, cooling, heating); Home, garden, pool "
        "(Home decor, accessories / Furniture / Tools, equipment / Kitchenware / Garden, patio); "
        "Hobbies, sports, leisure; Health, beauty; Free stuff"),
    ExternalPlatform.KYMA: "Electronics; Furniture; Home; Fashion; Kids; Sports; Hobbies; Other",
    ExternalPlatform.FACEBOOK: (
        "Electronics; Home & garden (Furniture, Household, Tools, Garden); Clothing & accessories; "
        "Entertainment (Video games, Books, Music); Family (Baby & kids, Health & beauty, Pet supplies); "
        "Hobbies (Sports & outdoors, Musical instruments, Toys & games, Arts & crafts); Miscellaneous"),
}
SELLABLE = (ItemStatus.READY, ItemStatus.PUBLISHED, ItemStatus.HAS_INQUIRIES)
OPEN = (ExternalStatus.WAITING, ExternalStatus.FILLING, ExternalStatus.FILLED, ExternalStatus.APPROVED)
STALE_AFTER = timedelta(minutes=60)  # a task the helper took but never reported back
RETRY_AFTER = timedelta(hours=2)     # after a failed attempt or an unanswered screenshot
MAX_ATTEMPTS = 3


def k_enabled(p: ExternalPlatform) -> str:
    return f"marketplace_enabled:{p.value}"


def k_next(p: ExternalPlatform) -> str:
    return f"marketplace_next:{p.value}"


K_HELPER_SEEN = "helper_last_seen"


@dataclass
class Task:
    listing_id: int
    kind: str  # publish | remove
    platform: str
    item_id: int
    title: str = ""
    body: str = ""
    price: int | None = None
    category_path: list[str] | None = None
    condition: str | None = None
    language: str = "en"
    location: str = "Limassol"
    photo_ids: list[int] | None = None
    url: str | None = None


def footer(lang: Language, seller: str) -> str:
    if lang == Language.RU:
        return f"Лимасол, самовывоз / встреча, без доставки. Торг уместен.\nПишите в Telegram: @{seller}"
    return f"Pickup in Limassol, no delivery. Price negotiable.\nMessage me on Telegram: @{seller}"


async def is_enabled(p: ExternalPlatform) -> bool:
    return bool(await settings_store.get(k_enabled(p), True))


# ---------- queue ----------

async def enqueue(item_id: int, platforms: tuple[ExternalPlatform, ...] = PLATFORMS) -> int:
    """Puts an approved item in the queue of each platform it isn't on yet. Returns how many were added."""
    added = 0
    async with session_scope() as s:
        item = await s.get(Item, item_id)
        if item is None or item.status not in SELLABLE or item.quantity_left == 0:
            return 0
        have = set(await s.scalars(select(ExternalListing.platform).where(ExternalListing.item_id == item_id)))
        for p in platforms:
            if p not in have:
                s.add(ExternalListing(item_id=item_id, platform=p, status=ExternalStatus.WAITING, meta={}))
                added += 1
    return added


async def enqueue_all() -> int:
    async with session_scope() as s:
        ids = list(await s.scalars(select(Item.id).where(Item.status.in_(SELLABLE)).order_by(Item.id)))
    return sum([await enqueue(i) for i in ids])


async def item_gone(s, item_id: int) -> list[ExternalListing]:
    """Sold out / withdrawn: unpublished ads are cancelled, published ones must be removed by the helper."""
    to_remove = []
    for ext in await s.scalars(select(ExternalListing).where(ExternalListing.item_id == item_id)):
        if ext.status in OPEN:
            ext.status = ExternalStatus.CANCELLED
        elif ext.status == ExternalStatus.PUBLISHED:
            ext.status = ExternalStatus.REMOVAL_NEEDED
            to_remove.append(ext)
    return to_remove


async def skip(listing_id: int) -> ExternalListing:
    async with session_scope() as s:
        ext = await s.get(ExternalListing, listing_id)
        if ext.status in OPEN or ext.status == ExternalStatus.FAILED:
            ext.status = ExternalStatus.CANCELLED
        return ext


# ---------- helper API ----------

async def claim(platforms: list[str], now: datetime | None = None) -> Task | None:
    """The next task for the helper: a removal first, then a publication on a platform whose pace allows it."""
    settings = get_settings()
    now = now or datetime.now(UTC)
    await settings_store.put_time(K_HELPER_SEEN, now)
    wanted = [p for p in PLATFORMS if p.value in platforms]
    await _release_stale(now)

    async with session_scope() as s:
        ext = await s.scalar(select(ExternalListing).where(
            ExternalListing.status == ExternalStatus.REMOVAL_NEEDED,
            ExternalListing.platform.in_(wanted)).order_by(ExternalListing.id).limit(1))
        if ext is not None:
            ext.status = ExternalStatus.REMOVING
            ext.meta = {**(ext.meta or {}), "claimed_at": now.isoformat()}
            return Task(listing_id=ext.id, kind="remove", platform=ext.platform.value, item_id=ext.item_id, url=ext.url)

    local = now.astimezone(ZoneInfo(settings.timezone))
    if not settings.publish_window_start <= local.hour < settings.publish_window_end:
        return None
    for p in wanted:
        if not await is_enabled(p):
            continue
        nxt = await settings_store.get_time(k_next(p))
        if nxt and now < nxt:
            continue
        async with session_scope() as s:
            busy = await s.scalar(select(func.count()).select_from(ExternalListing).where(
                ExternalListing.platform == p,
                ExternalListing.status.in_([ExternalStatus.FILLING, ExternalStatus.FILLED, ExternalStatus.APPROVED])))
            today = await s.scalar(select(func.count()).select_from(ExternalListing).where(
                ExternalListing.platform == p, ExternalListing.published_at >= now - timedelta(hours=24)))
            if busy or today >= settings.marketplace_daily_limit:
                continue
            candidates = list(await s.scalars(
                select(ExternalListing).join(Item, Item.id == ExternalListing.item_id)
                .where(ExternalListing.platform == p, ExternalListing.status == ExternalStatus.WAITING,
                       Item.status.in_(SELLABLE), Item.quantity_sold < Item.quantity_total)
                .order_by(Item.priority.desc(), Item.price.desc().nulls_last(), ExternalListing.id)))
            ext = next((c for c in candidates if not _retry_later((c.meta or {}), now)), None)
            if ext is None:
                continue
            ext.status = ExternalStatus.FILLING
            ext.meta = {**(ext.meta or {}), "claimed_at": now.isoformat()}
            listing_id = ext.id
        try:
            return await _publish_task(listing_id)
        except Exception as e:  # e.g. the AI text failed: put it back, try later
            log.exception("Preparing marketplace task %s failed", listing_id)
            await failed(listing_id, f"text: {type(e).__name__}: {e}", now)
            return None
    return None


def _retry_later(meta: dict, now: datetime) -> bool:
    after = meta.get("retry_after")
    return bool(after and now < datetime.fromisoformat(after))


async def _release_stale(now: datetime) -> None:
    async with session_scope() as s:
        for ext in await s.scalars(select(ExternalListing).where(ExternalListing.status.in_(
                [ExternalStatus.FILLING, ExternalStatus.APPROVED, ExternalStatus.REMOVING]))):
            claimed = (ext.meta or {}).get("claimed_at")
            if claimed and now - datetime.fromisoformat(claimed) > STALE_AFTER:
                ext.status = (ExternalStatus.REMOVAL_NEEDED if ext.status == ExternalStatus.REMOVING
                              else ExternalStatus.WAITING)


async def _publish_task(listing_id: int) -> Task:
    async with session_scope() as s:
        ext = await s.get(ExternalListing, listing_id)
        item = await s.get(Item, ext.item_id, options=[selectinload(Item.texts), selectinload(Item.photos)])
        platform = ext.platform
        lang = LANGUAGE[platform]
        text = next((t for t in item.texts if t.platform == TEXT_PLATFORM[platform] and t.language == lang), None)
        meta = dict(ext.meta or {})
        if text is None or not meta.get("category_path"):
            generated = await write_text(item, platform)
            if text is None:
                text = ItemText(platform=TEXT_PLATFORM[platform], language=lang, title="", body="")
                item.texts.append(text)
            text.title, text.body = generated.title, generated.body
            meta.update(category_path=generated.category_path, condition=generated.condition)
            ext.meta = meta
        seller = get_settings().seller_username
        return Task(
            listing_id=ext.id, kind="publish", platform=platform.value, item_id=item.id,
            title=text.title, body=f"{text.body}\n\n{footer(lang, seller)}",
            price=int(item.price) if item.price is not None else None,
            category_path=meta.get("category_path"), condition=meta.get("condition"), language=lang.value,
            photo_ids=[p.id for p in item.photos[:10]],
        )


async def write_text(item: Item, platform: ExternalPlatform) -> MarketplaceText:
    lang = LANGUAGE[platform]
    master = master_text(item, lang) or master_text(item, Language.EN) or master_text(item, Language.RU)
    rules = text_rules_block(item)
    content = (
        f"Platform: {NAMES[platform]}\nLanguage: {'Russian' if lang == Language.RU else 'English'}\n"
        f"Title length limit: {TITLE_LIMIT[platform]} characters\nCategories on this platform: {CATEGORIES[platform]}\n\n"
        f"Item card:\n{item_facts(item)}\n\n"
        + (f"[Approved Telegram text]\n{master.title}\n{master.body}\n\n" if master else "")
        + (f"{rules}\n" if rules else "")
    )
    out = await structured(purpose="marketplace_text", output=MarketplaceText, system=prompts.load("marketplace_listing"),
                           content=content, item_id=item.id, model=get_settings().ai_model_fast, max_tokens=1500)
    out.title = strip_emoji(out.title)[:TITLE_LIMIT[platform]]
    out.body = strip_emoji(out.body)
    return out


async def filled(listing_id: int, screenshot: str | None) -> ExternalListing:
    async with session_scope() as s:
        ext = await s.get(ExternalListing, listing_id)
        ext.status, ext.filled_at = ExternalStatus.FILLED, datetime.now(UTC)
        ext.meta = {**(ext.meta or {}), "screenshot": screenshot}
        return ext


async def decide(listing_id: int, publish: bool) -> ExternalListing:
    async with session_scope() as s:
        ext = await s.get(ExternalListing, listing_id)
        if ext.status != ExternalStatus.FILLED:
            raise ValueError("помощник уже закрыл эту форму — заполню заново позже")
        if publish:
            ext.status = ExternalStatus.APPROVED
            ext.meta = {**(ext.meta or {}), "claimed_at": datetime.now(UTC).isoformat()}
        else:
            ext.status = ExternalStatus.CANCELLED
        return ext


async def decision(listing_id: int) -> str:
    """For the helper waiting on a filled form: pending | publish | cancel."""
    await settings_store.put_time(K_HELPER_SEEN, datetime.now(UTC))
    async with session_scope() as s:
        ext = await s.get(ExternalListing, listing_id)
    if ext is None or ext.status in (ExternalStatus.CANCELLED, ExternalStatus.WAITING):
        return "cancel"
    return {ExternalStatus.FILLED: "pending", ExternalStatus.APPROVED: "publish"}.get(ext.status, "cancel")


async def no_answer(listing_id: int, now: datetime | None = None) -> None:
    """The helper waited for my decision too long and closed the form: try again later."""
    now = now or datetime.now(UTC)
    async with session_scope() as s:
        ext = await s.get(ExternalListing, listing_id)
        if ext.status == ExternalStatus.FILLED:
            ext.status = ExternalStatus.WAITING
            ext.meta = {**(ext.meta or {}), "retry_after": (now + RETRY_AFTER).isoformat()}


async def published(listing_id: int, url: str | None, now: datetime | None = None) -> ExternalListing:
    settings = get_settings()
    now = now or datetime.now(UTC)
    async with session_scope() as s:
        ext = await s.get(ExternalListing, listing_id)
        ext.status, ext.published_at, ext.url, ext.error = ExternalStatus.PUBLISHED, now, url, None
        platform = ext.platform
    gap = random.uniform(settings.marketplace_gap_min_minutes, settings.marketplace_gap_max_minutes)
    await settings_store.put_time(k_next(platform), now + timedelta(minutes=gap))
    return ext


async def failed(listing_id: int, error: str, now: datetime | None = None) -> ExternalListing:
    now = now or datetime.now(UTC)
    async with session_scope() as s:
        ext = await s.get(ExternalListing, listing_id)
        meta = dict(ext.meta or {})
        ext.error = error[:1000]
        if ext.status == ExternalStatus.REMOVING:
            ext.status = ExternalStatus.REMOVAL_NEEDED
            meta["retry_after"] = (now + RETRY_AFTER).isoformat()
        else:
            meta["attempts"] = meta.get("attempts", 0) + 1
            meta["retry_after"] = (now + RETRY_AFTER).isoformat()
            ext.status = ExternalStatus.FAILED if meta["attempts"] >= MAX_ATTEMPTS else ExternalStatus.WAITING
        ext.meta = meta
        return ext


async def removed(listing_id: int) -> ExternalListing:
    async with session_scope() as s:
        ext = await s.get(ExternalListing, listing_id)
        ext.status, ext.removed_at = ExternalStatus.REMOVED, datetime.now(UTC)
        return ext


async def retry(listing_id: int) -> None:
    async with session_scope() as s:
        ext = await s.get(ExternalListing, listing_id)
        if ext.status in (ExternalStatus.FAILED, ExternalStatus.CANCELLED):
            ext.status = ExternalStatus.WAITING
            ext.meta = {k: v for k, v in (ext.meta or {}).items() if k not in ("attempts", "retry_after")}


# ---------- status for /platforms ----------

@dataclass
class PlatformStatus:
    platform: ExternalPlatform
    enabled: bool
    counts: dict[str, int]
    today: int


async def status() -> tuple[list[PlatformStatus], datetime | None]:
    now = datetime.now(UTC)
    out = []
    async with session_scope() as s:
        rows = (await s.execute(select(ExternalListing.platform, ExternalListing.status, func.count())
                                .group_by(ExternalListing.platform, ExternalListing.status))).all()
        today = dict((await s.execute(select(ExternalListing.platform, func.count()).where(
            ExternalListing.published_at >= now - timedelta(hours=24)).group_by(ExternalListing.platform))).all())
    for p in PLATFORMS:
        counts = {st.value: n for pl, st, n in rows if pl == p}
        out.append(PlatformStatus(platform=p, enabled=await is_enabled(p), counts=counts, today=today.get(p, 0)))
    return out, await settings_store.get_time(K_HELPER_SEEN)


async def listing_label(listing_id: int) -> tuple[ExternalListing, Item]:
    async with session_scope() as s:
        ext = await s.get(ExternalListing, listing_id)
        item = await s.get(Item, ext.item_id)
        return ext, item


def price_label(item: Item) -> str:
    return fmt_eur(item.price)


# ---------- browser agent: one planning step ----------

MAX_SNAPSHOT_CHARS = 24000


async def plan_step(listing_id: int, goal: str, snapshot: dict, history: list[dict]) -> FormPlan:
    """The helper sends what it sees; Claude says what to fill/click next. The API key stays on the server."""
    async with session_scope() as s:
        ext = await s.get(ExternalListing, listing_id)
        item = await s.get(Item, ext.item_id, options=[selectinload(Item.texts), selectinload(Item.photos)])
        platform, lang, meta, url = ext.platform, LANGUAGE[ext.platform], dict(ext.meta or {}), ext.url
        text = next((t for t in item.texts if t.platform == TEXT_PLATFORM[platform] and t.language == lang), None)
        seller = get_settings().seller_username
        ad = {
            "title": text.title if text else item.display_name,
            "description": f"{text.body}\n\n{footer(lang, seller)}" if text else "",
            "price_eur": int(item.price) if item.price is not None else None,
            "category_path": meta.get("category_path"), "condition": meta.get("condition"),
            "location": "Limassol", "photos": len(item.photos[:10]), "ad_url": url,
        }
    page = json.dumps(snapshot, ensure_ascii=False)[:MAX_SNAPSHOT_CHARS]
    done = json.dumps(history[-30:], ensure_ascii=False)
    content = (f"Site: {NAMES[platform]}\nGoal: {goal}\n\nAd data:\n{json.dumps(ad, ensure_ascii=False, indent=1)}\n\n"
               f"Actions done so far (with results):\n{done}\n\nCurrent page snapshot:\n{page}")
    return await structured(purpose=f"marketplace_agent_{platform.value}", output=FormPlan,
                            system=prompts.load("marketplace_agent"), content=content, item_id=ext.item_id,
                            max_tokens=2000)
