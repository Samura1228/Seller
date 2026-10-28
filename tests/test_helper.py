"""The Mac helper in a real (headless) Chromium against a fake multi-step 'post an ad' site.

The AI planner is replaced by a rule-based fake; the point is the browser side: snapshots, filling,
photo upload, the publish guard, waiting for the owner's decision, reporting back.
"""

import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("playwright")
CHROMIUM = os.environ.get("CHROMIUM_PATH", "/opt/pw-browsers/chromium")
pytestmark = pytest.mark.skipif(not Path(CHROMIUM).exists(), reason="no chromium")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "helper"))
from playwright.async_api import async_playwright  # noqa: E402

import helper  # noqa: E402

PAGES = {
    "/": '<a href="/add">+ Post ad</a> <a href="/help">Help</a>',
    "/add": '<h1>Choose a category</h1><a href="/add/electronics">Electronics, appliances</a> <a href="/add/home">Home</a>',
    "/add/electronics": """
      <h1>Ad details</h1>
      <form action="/add/preview" method="get">
        <label for="t">Title</label><input id="t" name="title">
        <label for="d">Description</label><textarea id="d" name="desc"></textarea>
        <label for="p">Price, €</label><input id="p" name="price">
        <label for="c">Condition</label><select id="c" name="cond"><option>Choose</option><option>New</option><option>Used</option></select>
        <label for="f">Photos</label><input id="f" type="file" multiple onchange="document.getElementById('n').value=this.files.length">
        <input id="n" name="nphotos" type="hidden">
        <button type="button" onclick="location='/adv/bad'">Publish now</button>
        <button type="submit">Next</button>
      </form>""",
    "/add/preview": '<h1>Preview</h1><button onclick="location=\'/adv/6812000_speaker\'">Publish</button>',
    "/adv/6812000_speaker": "<h1>Your ad is live</h1>",
}


class FakeApi:
    def __init__(self):
        self.calls, self.filled_png, self.result = [], None, None
        self.decisions = ["pending", "publish"]

    async def photo(self, pid):
        return b"\xff\xd8\xff\xe0fakejpeg"

    async def plan(self, task_id, goal, snap, history):
        self.calls.append((goal, snap["url"]))
        els = snap["elements"]

        def find(text):
            return next(e["id"] for e in els if text in (e.get("text", "") + " " + e.get("label", "")))

        path = "/" + snap["url"].split("/", 3)[3].split("?")[0]
        done = {"state": "continue", "actions": [], "publish_element": None, "ad_url": None, "note_ru": ""}
        if goal == "after_publish":
            return done | {"state": "done", "ad_url": snap["url"]}
        if path == "/":
            return done | {"actions": [{"element": find("Post ad"), "action": "click", "value": None}]}
        if path == "/add":
            return done | {"actions": [{"element": find("Electronics"), "action": "click", "value": None}]}
        if path == "/add/electronics":
            return done | {"actions": [
                {"element": find("Publish now"), "action": "click", "value": None},  # must be refused
                {"element": find("Title"), "action": "fill", "value": "B&O Beosound Explore, sealed"},
                {"element": find("Description"), "action": "fill", "value": "New in box.\n" + "Great sound. " * 15},
                {"element": find("Price"), "action": "fill", "value": "185"},
                {"element": find("Condition"), "action": "select", "value": "new"},  # partial, case-insensitive
                {"element": find("Photos"), "action": "upload_photos", "value": None},
                {"element": find("Next"), "action": "click", "value": None},
            ]}
        if path == "/add/preview":
            return done | {"state": "ready", "publish_element": find("Publish"), "note_ru": "всё заполнено"}
        return done | {"state": "blocked", "note_ru": f"unexpected page {path}"}

    async def filled(self, task_id, png, note):
        self.filled_png = png

    async def decision(self, task_id):
        return self.decisions.pop(0) if len(self.decisions) > 1 else self.decisions[0]

    async def no_answer(self, task_id):
        self.result = ("no_answer",)

    async def published(self, task_id, url):
        self.result = ("published", url)

    async def failed(self, task_id, error, reason="error"):
        self.result = ("failed", error, reason)


async def _context(pw, tmp_path):
    ctx = await pw.chromium.launch_persistent_context(str(tmp_path / "profile"), headless=True, executable_path=CHROMIUM)

    async def serve(route):
        path = "/" + route.request.url.split("/", 3)[3].split("?")[0]
        body = PAGES.get(path)
        if body is None:
            await route.fulfill(status=404, body="not found")
        else:
            await route.fulfill(status=200, content_type="text/html", body=f"<html><body>{body}</body></html>")

    await ctx.route("https://fake.market/**", serve)
    return ctx


async def test_helper_fills_waits_and_publishes(tmp_path, monkeypatch):
    monkeypatch.setattr(helper, "human_pause", lambda *a, **k: _noop())
    api = FakeApi()
    task = {"listing_id": 7, "kind": "publish", "platform": "bazaraki", "item_id": 4, "photo_ids": [11, 12],
            "start_url": "https://fake.market/"}
    async with async_playwright() as pw:
        ctx = await _context(pw, tmp_path)
        result = await helper.publish(ctx, api, task, tmp_path, decision_timeout=30, poll=0.01)
        await ctx.close()

    assert result == "published", api.result
    assert api.result == ("published", "https://fake.market/adv/6812000_speaker")
    assert api.filled_png and api.filled_png[:4] == b"\x89PNG"
    # the form was really filled: the preview URL carries the submitted values
    preview = next(u for g, u in api.calls if "/add/preview" in u)
    assert "title=B%26O+Beosound+Explore%2C+sealed" in preview and "price=185" in preview
    assert "cond=New" in preview and "nphotos=2" in preview
    assert not any("/adv/bad" in u for _, u in api.calls)  # the decoy publish button was refused


async def test_helper_gives_up_waiting_for_owner(tmp_path, monkeypatch):
    monkeypatch.setattr(helper, "human_pause", lambda *a, **k: _noop())
    api = FakeApi()
    api.decisions = ["pending"]
    task = {"listing_id": 8, "kind": "publish", "platform": "bazaraki", "item_id": 4, "photo_ids": [],
            "start_url": "https://fake.market/"}
    async with async_playwright() as pw:
        ctx = await _context(pw, tmp_path)
        assert await helper.publish(ctx, api, task, tmp_path, decision_timeout=0.05, poll=0.01) == "no_answer"
        await ctx.close()
    assert api.result == ("no_answer",)


def test_publish_guard_words():
    from sites import looks_like_publish

    assert looks_like_publish("Publish") and looks_like_publish("Опубликовать") and looks_like_publish("Post ad")
    assert not looks_like_publish("Next") and not looks_like_publish("Continue to publish")
    assert not looks_like_publish("Price")


async def _noop():
    return None
