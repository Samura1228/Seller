"""/agent: a free conversation with Claude that can read the system's data, look at photos and search the web.

One session at a time (the bot has one owner); it lives in memory until /cancel or a restart.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.ai import prompts
from app.ai.client import _client, _log_usage, image_block
from app.config import get_settings
from app.services.agent_tools import TOOLS, Proposal, ToolContext, run_tool, shrink

log = logging.getLogger(__name__)

MAX_TURNS = 10          # tool rounds per owner message
KEEP_MESSAGES = 40      # history length sent to the model
KEEP_IMAGES = 3         # older images are replaced by a placeholder to keep requests small


@dataclass
class Reply:
    text: str
    proposals: list[Proposal] = field(default_factory=list)


def _block_dict(b: Any) -> dict:
    return b.model_dump(exclude_none=True) if hasattr(b, "model_dump") else dict(b)


class AgentSession:
    def __init__(self, userbot: Any = None):
        self.userbot = userbot
        self.messages: list[dict] = []
        self.lock = asyncio.Lock()
        self.started = datetime.now(ZoneInfo(get_settings().timezone))

    def _system(self) -> list[dict]:
        now = datetime.now(ZoneInfo(get_settings().timezone))
        seller = get_settings().seller_username
        return [{"type": "text", "text": prompts.load("agent"), "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": f"Now: {now:%Y-%m-%d %H:%M} (Cyprus). Selling account: @{seller}."}]

    def _tools(self) -> list[dict]:
        web = {"type": "web_search_20250305", "name": "web_search", "max_uses": 5}
        return [*TOOLS, web]

    def _trimmed(self) -> list[dict]:
        msgs = self.messages[-KEEP_MESSAGES:]
        # never start in the middle of a tool exchange
        while msgs and not (msgs[0]["role"] == "user" and not _has_tool_result(msgs[0])):
            msgs = msgs[1:]
        # keep only the last few images
        seen = 0
        out = []
        for m in reversed(msgs):
            content = m["content"]
            if isinstance(content, list):
                new = []
                for b in content:
                    if b.get("type") == "image":
                        seen += 1
                        if seen > KEEP_IMAGES:
                            new.append({"type": "text", "text": "[earlier image]"})
                            continue
                    new.append(b)
                content = new
            out.append({"role": m["role"], "content": content})
        return list(reversed(out))

    async def ask(self, text: str, images: list[bytes] | None = None) -> Reply:
        async with self.lock:
            content: list[dict] = [image_block(shrink(img)) for img in (images or [])[:4]]
            content.append({"type": "text", "text": text or ("Что на фото?" if images else "…")})
            self.messages.append({"role": "user", "content": content})
            ctx = ToolContext(userbot=self.userbot)
            settings = get_settings()
            texts: list[str] = []
            for _ in range(MAX_TURNS):
                resp = await _client().messages.create(
                    model=settings.ai_model, max_tokens=2500, system=self._system(),
                    tools=self._tools(), messages=self._trimmed())
                await _log_usage("agent", settings.ai_model, None, resp.usage)
                blocks = [_block_dict(b) for b in resp.content]
                self.messages.append({"role": "assistant", "content": blocks})
                texts = [b.text for b in resp.content if b.type == "text"] or texts
                if resp.stop_reason == "pause_turn":  # a long web search: let it continue
                    continue
                calls = [b for b in resp.content if b.type == "tool_use"]
                if resp.stop_reason != "tool_use" or not calls:
                    break
                results = []
                for c in calls:
                    out = await run_tool(c.name, c.input or {}, ctx)
                    results.append({"type": "tool_result", "tool_use_id": c.id, "content": out})
                self.messages.append({"role": "user", "content": results})
            else:
                texts.append("(остановился: слишком много шагов — уточни вопрос)")
            return Reply(text="\n\n".join(t.strip() for t in texts if t.strip()) or "…", proposals=ctx.proposals)


def _has_tool_result(m: dict) -> bool:
    c = m.get("content")
    return isinstance(c, list) and any(b.get("type") == "tool_result" for b in c)
