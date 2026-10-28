"""Google Sheet with every inquiry and the base of buyers who wrote to us.

The sheet has a tiny Apps Script web app (scripts/google_sheet.gs) that upserts rows by id; the server
only POSTs JSON to it. Setup needs no Google Cloud project: SHEETS_WEBHOOK_URL + SHEETS_SECRET.
Sheet writes are best effort: a failure is logged and never blocks inquiry handling.
"""

import logging
from collections import defaultdict
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select

from app.config import get_settings
from app.db.models import Buyer, Chat, Inquiry, Item, Publication, Sale
from app.db.session import session_scope
from app.services.listing import fmt_eur

log = logging.getLogger(__name__)

INTENT_RU = {"question": "вопрос", "wants_to_buy": "хочет купить", "price_offer": "торг", "reserve": "бронь",
             "other": "другое"}
STATUS_RU = {"new": "новая", "recognized": "распознана", "pending_approval": "на подтверждении", "ready": "готова",
             "published": "опубликована", "has_inquiries": "есть отклики", "reserved": "бронь", "sold": "продана",
             "withdrawn": "снята"}
BATCH = 200


def _http() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=60, follow_redirects=True)


def enabled() -> bool:
    return bool(get_settings().sheets_webhook_url)


def _when(dt) -> str:
    return dt.astimezone(ZoneInfo(get_settings().timezone)).strftime("%Y-%m-%d %H:%M") if dt else ""


def _link(buyer: Buyer | None) -> str:
    return f"https://t.me/{buyer.username}" if buyer and buyer.username else ""


def inquiry_row(inq: Inquiry, item: Item | None, buyer: Buyer | None, chat: Chat | None) -> dict:
    return {
        "ID отклика": inq.id,
        "Дата": _when(inq.created_at),
        "Покупатель": (buyer.display_name if buyer else "") or "",
        "Username": f"@{buyer.username}" if buyer and buyer.username else "",
        "Telegram ID": f"'{buyer.tg_user_id}" if buyer else "",  # leading ' keeps it text in Sheets
        "Вещь": f"#{item.id} {item.display_name}" if item else "не определено",
        "Цена": fmt_eur(item.price) if item else "",
        "Минимум": fmt_eur(item.min_price) if item else "",
        "Предложил": fmt_eur(inq.price_offer) if inq.price_offer else "",
        "Намерение": INTENT_RU.get(inq.intent or "", inq.intent or ""),
        "Суть": inq.summary or "",
        "Сообщение": (inq.text or "")[:1000],
        "Где": f"ответ в {chat.title or chat.username}" if chat else "личка",
        "Ссылка на чат": _link(buyer),
        "Статус вещи": STATUS_RU.get(item.status.value, item.status.value) if item else "",
    }


async def _buyer_rows(s, buyer_ids: set[int]) -> list[dict]:
    if not buyer_ids:
        return []
    buyers = {b.id: b for b in await s.scalars(select(Buyer).where(Buyer.id.in_(buyer_ids)))}
    inqs = (await s.execute(select(Inquiry, Item).outerjoin(Item, Item.id == Inquiry.item_id)
                            .where(Inquiry.buyer_id.in_(buyer_ids)).order_by(Inquiry.id))).all()
    sales = (await s.execute(select(Sale, Item).join(Item, Item.id == Sale.item_id)
                             .where(Sale.buyer_id.in_(buyer_ids)))).all()
    per: dict[int, list] = defaultdict(list)
    for inq, item in inqs:
        per[inq.buyer_id].append((inq, item))
    bought: dict[int, list[str]] = defaultdict(list)
    for sale, item in sales:
        bought[sale.buyer_id].append(f"#{item.id} {item.display_name} за {fmt_eur(sale.final_price)}")
    rows = []
    for bid, b in buyers.items():
        lst = per.get(bid, [])
        items = list(dict.fromkeys(f"#{it.id} {it.display_name}" for _, it in lst if it))
        rows.append({
            "Telegram ID": f"'{b.tg_user_id}",
            "Имя": b.display_name or "",
            "Username": f"@{b.username}" if b.username else "",
            "Ссылка на чат": _link(b),
            "Первое сообщение": _when(lst[0][0].created_at) if lst else _when(b.first_seen_at),
            "Последнее сообщение": _when(lst[-1][0].created_at) if lst else "",
            "Откликов": len(lst),
            "Интересовался": "; ".join(items),
            "Купил": "; ".join(bought.get(bid, [])),
        })
    return rows


async def _rows_for(inquiry_ids: list[int] | None) -> tuple[list[dict], list[dict]]:
    async with session_scope() as s:
        q = (select(Inquiry, Item, Buyer, Chat)
             .outerjoin(Item, Item.id == Inquiry.item_id).outerjoin(Buyer, Buyer.id == Inquiry.buyer_id)
             .outerjoin(Publication, Publication.id == Inquiry.publication_id)
             .outerjoin(Chat, Chat.id == Publication.chat_id).order_by(Inquiry.id))
        if inquiry_ids is not None:
            q = q.where(Inquiry.id.in_(inquiry_ids))
        rows = (await s.execute(q)).all()
        inquiries = [inquiry_row(inq, item, buyer, chat) for inq, item, buyer, chat in rows]
        buyers = await _buyer_rows(s, {inq.buyer_id for inq, *_ in rows if inq.buyer_id})
    return inquiries, buyers


async def _post(inquiries: list[dict], buyers: list[dict]) -> None:
    settings = get_settings()
    payload = {"secret": settings.sheets_secret or "", "inquiries": inquiries, "buyers": buyers}
    # Apps Script answers a POST with a redirect to the result page; the script has already run by then.
    async with _http() as http:
        r = await http.post(settings.sheets_webhook_url, json=payload)
    r.raise_for_status()
    try:
        body = r.json()
    except ValueError:
        raise RuntimeError(f"unexpected answer from the sheet script: {r.text[:200]}") from None
    if not body.get("ok"):
        raise RuntimeError(f"sheet script: {body.get('error')}")


async def push_inquiry(inquiry_id: int) -> None:
    """New or updated inquiry → its row in «Отклики» and its buyer's row in «Покупатели». Never raises."""
    if not enabled():
        return
    try:
        await _post(*await _rows_for([inquiry_id]))
    except Exception:
        log.exception("Google Sheet update failed for inquiry %s", inquiry_id)


async def push_buyer(buyer_id: int) -> None:
    if not enabled():
        return
    try:
        async with session_scope() as s:
            buyers = await _buyer_rows(s, {buyer_id})
        await _post([], buyers)
    except Exception:
        log.exception("Google Sheet update failed for buyer %s", buyer_id)


async def sync_all() -> tuple[int, int]:
    """Everything at once (first setup, or after the sheet was edited by hand). Raises on failure."""
    inquiries, buyers = await _rows_for(None)
    for i in range(0, max(len(inquiries), len(buyers), 1), BATCH):
        await _post(inquiries[i:i + BATCH], buyers[i:i + BATCH])
    return len(inquiries), len(buyers)
