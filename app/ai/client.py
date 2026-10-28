"""Thin wrapper around the Claude API: structured JSON outputs, web research, usage logging."""

import base64
import logging
from decimal import Decimal
from functools import lru_cache
from typing import Any

import anthropic
from anthropic import transform_schema
from pydantic import BaseModel, ValidationError

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


def _strip(node: Any) -> Any:
    """Drops pydantic noise (titles, defaults) that would otherwise leak into descriptions."""
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k in ("title", "default"):
                continue
            if k in ("properties", "$defs"):  # keys here are names, not schema keywords
                out[k] = {name: _strip(sub) for name, sub in v.items()}
            else:
                out[k] = _strip(v)
        return out
    if isinstance(node, list):
        return [_strip(v) for v in node]
    return node


def _require_all(node: Any) -> Any:
    """Every property required (nullable ones stay nullable): deterministic output, no optional-param limits."""
    if isinstance(node, dict):
        if node.get("type") == "object" and "properties" in node:
            node["required"] = list(node["properties"])
        for v in node.values():
            _require_all(v)
    elif isinstance(node, list):
        for v in node:
            _require_all(v)
    return node


def output_schema(model_cls: type[BaseModel]) -> dict[str, Any]:
    """JSON schema accepted by structured outputs (additionalProperties: false, no numeric constraints)."""
    return _require_all(transform_schema(_strip(model_cls.model_json_schema())))


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
    """Asks Claude for JSON matching `output` (native structured outputs) and validates it."""
    model = model or get_settings().ai_model
    resp = await _client().messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": content}],
        output_config={"format": {"type": "json_schema", "schema": output_schema(output)}},
    )
    await _log_usage(purpose, model, item_id, resp.usage)
    if resp.stop_reason in ("max_tokens", "refusal"):
        raise AiError(f"{purpose}: no complete result (stop_reason={resp.stop_reason})")
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    if not text:
        raise AiError(f"{purpose}: empty response (stop_reason={resp.stop_reason})")
    try:
        return output.model_validate_json(text)
    except ValidationError as e:
        raise AiError(f"{purpose}: invalid JSON from model: {e}") from e


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
        # No user_location: the API rejects country "CY" (not in its supported list), which failed every search.
        # The prompt itself asks for Cyprus / EU prices in EUR.
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
