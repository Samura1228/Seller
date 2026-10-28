"""Checks the request we send to the Claude API and how responses are parsed (HTTP mocked)."""

import json

import anthropic
import httpx2 as httpx
import pytest

from app.ai import client as ai
from app.ai.schemas import MasterTexts


def _msg(content, stop_reason, searches=0):
    return {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
        "content": content, "stop_reason": stop_reason, "stop_sequence": None,
        "usage": {"input_tokens": 1000, "output_tokens": 200, "server_tool_use": {"web_search_requests": searches}},
    }


@pytest.fixture
def mock_api(monkeypatch):
    requests, responses, usage = [], [], []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=responses.pop(0))

    fake = anthropic.AsyncAnthropic(api_key="test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(ai, "_client", lambda: fake)

    async def log_usage(purpose, model, item_id, u):
        usage.append((purpose, u.input_tokens, u.output_tokens, u.server_tool_use.web_search_requests))

    monkeypatch.setattr(ai, "_log_usage", log_usage)
    return requests, responses, usage


async def test_structured_uses_json_output_and_validates(mock_api):
    requests, responses, usage = mock_api
    payload = {"ru": {"title": "Т", "body": "Б"}, "en": {"title": "T", "body": "B"}}
    responses.append(_msg([{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}], "end_turn"))

    out = await ai.structured(purpose="listing", output=MasterTexts, system="sys", content="card")

    assert out.ru.title == "Т" and out.en.body == "B"
    req = requests[0]
    assert "tool_choice" not in req and "tools" not in req
    fmt = req["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["schema"]["additionalProperties"] is False
    assert usage == [("listing", 1000, 200, 0)]


async def test_web_research_continues_after_pause_turn(mock_api):
    requests, responses, usage = mock_api
    responses.append(_msg([{"type": "text", "text": "Searching…"}], "pause_turn", searches=2))
    responses.append(_msg([{"type": "text", "text": "New price €349 https://sony.com"}], "end_turn", searches=1))

    text = await ai.web_research(purpose="price_research", system="sys", prompt="item")

    assert "€349" in text
    assert len(requests) == 2
    tool = requests[0]["tools"][0]
    assert tool["type"] == "web_search_20250305" and "user_location" not in tool  # API rejects country CY
    assert requests[1]["messages"][-1]["role"] == "assistant"  # paused turn sent back
    assert [u[3] for u in usage] == [2, 1]


async def test_structured_bad_or_truncated_output_raises(mock_api):
    _, responses, _ = mock_api
    responses.append(_msg([{"type": "text", "text": "not json"}], "end_turn"))
    with pytest.raises(ai.AiError, match="invalid JSON"):
        await ai.structured(purpose="x", output=MasterTexts, system="s", content="c")
    responses.append(_msg([{"type": "text", "text": '{"ru": {"ti'}], "max_tokens"))
    with pytest.raises(ai.AiError, match="max_tokens"):
        await ai.structured(purpose="x", output=MasterTexts, system="s", content="c")
