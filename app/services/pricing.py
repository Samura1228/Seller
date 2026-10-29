import logging
from decimal import Decimal
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import prompts
from app.ai.client import structured, web_research
from app.ai.schemas import PriceProposal
from app.db.enums import Actor, PriceReason
from app.db.models import Item, PriceResearch
from app.integrations import apify_bazaraki
from app.services.items import set_price
from app.services.recognition import item_facts

log = logging.getLogger(__name__)


def search_query(item: Item) -> str:
    raw = item.recognition_raw or {}
    return (raw.get("search_query") or " ".join(filter(None, [item.brand, item.model])) or item.title or "").strip()


async def research_and_propose(s: AsyncSession, item: Item) -> PriceResearch:
    query = search_query(item)
    facts = item_facts(item)

    comps: list[dict] = []
    comps_note = ""
    warnings: list[str] = []
    try:
        comps = await apify_bazaraki.search(query)
    except apify_bazaraki.ComparablesError as e:
        comps_note = f"(Bazaraki comparables unavailable: {e})"
        log.warning("Comparables failed for item %s: %s", item.id, e)
        warnings.append(f"аналоги с Bazaraki ({e})")

    web_failed = False
    try:
        research = await web_research(
            purpose="price_research",
            system=prompts.load("price_research"),
            prompt=f"Item:\n{facts}\n\nSearch hint: {query}",
            item_id=item.id,
        )
    except Exception as e:  # web search problems must not block pricing: comps + knowledge still work
        log.warning("Web research failed for item %s: %s", item.id, e)
        research = f"(web research unavailable: {type(e).__name__})"
        web_failed = True
        warnings.append(f"поиск в вебе ({type(e).__name__}: {str(e)[:120]})")

    comps_text = "\n".join(
        f"[{i}] €{c['price']:.0f} — {c['title']} ({c.get('location') or '?'})" for i, c in enumerate(comps)
    ) or f"none {comps_note}"
    proposal = await structured(
        purpose="price_propose",
        output=PriceProposal,
        system=prompts.load("price_propose"),
        content=(
            f"Item card:\n{facts}\n\n"
            f"Web research notes:\n{research or 'none'}\n\n"
            f"Comparable Bazaraki listings (search: {query!r}):\n{comps_text}"
        ),
        item_id=item.id,
        max_tokens=1500,
    )

    relevant = [comps[i] for i in proposal.relevant_comp_indexes if 0 <= i < len(comps)]
    new_price, source = proposal.new_price_eur, proposal.new_price_source_url
    if web_failed or not _from_notes(source, research):
        # A "new price" that isn't in what the web search actually found is the model's memory: drop it.
        new_price, source = None, None
    pr = PriceResearch(
        item_id=item.id,
        new_price=Decimal(new_price) if new_price else None,
        new_price_source_url=source,
        comps=relevant,
        proposed_price=Decimal(proposal.price_eur),
        proposed_min=Decimal(min(proposal.min_price_eur, proposal.price_eur)),
        rationale=proposal.rationale_ru,
        warnings=warnings,
    )
    s.add(pr)
    item.new_price_ref = pr.new_price
    await set_price(s, item, pr.proposed_price, pr.proposed_min, PriceReason.INITIAL, Actor.AI)
    return pr


def _from_notes(url: str | None, notes: str) -> bool:
    """The price's source must be a site the web search actually returned (same domain)."""
    host = urlparse(url or "").netloc.lower().removeprefix("www.")
    return bool(host) and host in notes.lower()


async def latest_research(s: AsyncSession, item_id: int) -> PriceResearch | None:
    return await s.scalar(
        select(PriceResearch).where(PriceResearch.item_id == item_id).order_by(PriceResearch.id.desc()).limit(1)
    )


def comps_summary(pr: PriceResearch | None) -> str | None:
    if not pr or not pr.comps:
        return None
    prices = sorted(c["price"] for c in pr.comps)
    lo, hi = prices[0], prices[-1]
    rng = f"€{lo:.0f}" if lo == hi else f"€{lo:.0f}–{hi:.0f}"
    return f"{rng} ({len(prices)} объявл.)"
