"""Thin wrapper around the Claude API: structured output via a forced tool, web research, usage logging."""

import base64
import copy
import logging
from decimal import Decimal
from functools import lru_cache
from typing import Any

import anthropic
from pydantic import BaseModel

from app.config import get_settings
from app.db.models import AiUsage
from app.db.session import session_scope

log = logging.getLogger(__name__)

# USD per million tokens (input, output). Unknown models are logged with cost 0.
PRICES: dict[str, tuple[Decimal, Decimal]] = {
    "claude-opus-5-5": (Decimal("4"), Decimal("20")),
    "claude-sonnet-5-5": (Decimal("2"), Decimal("10")),
    "claude-haiku-5-5": (Decimal("0.10"), Decimal("0.50")),
}
WEB_SEARCH_USD = Decimal("0.01")


class AiError(Exception):
    pass


@lru_cache
def _client() -> anthropic.AsyncAnthropic:
    return anthropic.AsyncAnthropic(api_key=get_settings().anthropic_api_key, max_retries=4, timeout=180)


def image_block(jpeg: bytes) -> dict[str, Any]:
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(jpeg).decode()},
    }


def tool_schema(model_cls: type[BaseModel]) -> dict[str, Any]:
    """Pydantic JSON schema with $refs inlined (keeps the tool schema simple and portable)."""
    schema = model_cls.model_json_schema()
    defs = schema.pop("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                return resolve(copy.deepcopy(defs[node["$ref"].split("/")[-1]]))
            out = {}
            for k, v in node.items():
                if k == "title":  # pydantic's auto-generated schema titles are noise
                    continue
                if k == "properties":  # keys here are field names (a field may be called "title")
                    out[k] = {name: resolve(prop) for name, prop in v.items()}
                else:
                    out[k] = resolve(v)
            return out
        if isinstance(node, list):
            return [resolve(v) for v in node]
        return node

    return resolve(schema)


async def _log_usage(purpose: str, model: str, item_id: int | None, usage: Any) -> None:
    inp = (usage.input_tokens or 0) + (getattr(usage, "cache_creation_input_tokens", 0) or 0) + (
        getattr(usage, "cache_read_input_tokens", 0) or 0
    )
    out = usage.output_tokens or 0
    searches = 0
    stu = getattr(usage, "server_tool_use", None)
    if stu is not None:
        searches = getattr(stu, "web_search_requests", 0) or 0
    p_in, p_out = PRICES.get(model, (Decimal(0), Decimal(0)))
    cost = (p_in * inp + p_out * out) / Decimal(1_000_000) + WEB_SEARCH_USD * searches
    try:
        async with session_scope() as s:
            s.add(AiUsage(purpose=purpose, model=model, item_id=item_id, input_tokens=inp,
                          output_tokens=out, web_searches=searches, cost_usd=cost))
    except Exception:  # usage logging must never break the pipeline
        log.exception("Failed to log AI usage")


async def structured[T: BaseModel](
    *,
    purpose: str,
    output: type[T],
    system: str,
    content: list[dict[str, Any]] | str,
    item_id: int | None = None,
    model: str | None = None,
    max_tokens: int = 4096,
) -> T:
    """Forces Claude to answer by calling a single tool whose input schema is `output`."""
    model = model or get_settings().ai_model
    tool_name = "submit_result"
    resp = await _client().messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": content}],
        tools=[{
            "name": tool_name,
            "description": "Submit the final structured result.",
            "input_schema": tool_schema(output),
        }],
        tool_choice={"type": "tool", "name": tool_name},
    )
    await _log_usage(purpose, model, item_id, resp.usage)
    for block in resp.content:
        if block.type == "tool_use" and block.name == tool_name:
            return output.model_validate(block.input)
    raise AiError(f"{purpose}: model returned no structured result (stop_reason={resp.stop_reason})")


async def web_research(
    *, purpose: str, system: str, prompt: str, item_id: int | None = None, model: str | None = None
) -> str:
    """Free-text research with the server-side web search tool. Handles pause_turn continuations."""
    settings = get_settings()
    model = model or settings.ai_model
    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
    tools = [{
        "type": "web_search_20250305",
        "name": "web_search",
        "max_uses": settings.ai_web_search_max_uses,
        "user_location": {"type": "approximate", "city": "Limassol", "country": "CY", "timezone": "Asia/Nicosia"},
    }]
    texts: list[str] = []
    for _ in range(5):
        resp = await _client().messages.create(
            model=model, max_tokens=2048, system=system, messages=messages, tools=tools
        )
        await _log_usage(purpose, model, item_id, resp.usage)
        texts.extend(b.text for b in resp.content if b.type == "text")
        if resp.stop_reason != "pause_turn":
            break
        # Send the paused turn back unchanged so the server-side loop can continue.
        messages = [*messages, {"role": "assistant", "content": resp.content}]
    return "\n".join(t for t in texts if t).strip()
