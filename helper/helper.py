"""Limassol Seller — marketplace helper for the Mac.

Takes ads from the server, fills the forms of Bazaraki / Kyma.cy / Facebook Marketplace in a real browser
with your logins, sends a screenshot to the bot and presses "Publish" only after your ✅ there.

    python helper.py login bazaraki     # once per site: log in by hand in the window that opens
    python helper.py check              # is the server reachable, is the token right
    python helper.py run                # work: wait for tasks, fill, ask, publish

Settings: environment variables or ~/.limassol-helper/config.env
    SERVER_URL=https://<your-app>.up.railway.app
    HELPER_TOKEN=<same as HELPER_TOKEN on Railway>
    PLATFORMS=bazaraki,kyma,facebook
"""

import asyncio
import logging
import os
import random
import sys
import tempfile
import time
from pathlib import Path

import httpx
from playwright.async_api import BrowserContext, Page, async_playwright
from sites import SITES, looks_like_publish

HOME = Path.home() / ".limassol-helper"
PROFILE = HOME / "chrome-profile"  # one browser profile with all your logins; never leaves this Mac
log = logging.getLogger("helper")

MAX_STEPS = 25
DECISION_TIMEOUT = 45 * 60  # seconds to wait for your ✅ in the bot, then close the form and retry later
IDLE_SLEEP = (50, 90)
ACTION_TIMEOUT_MS = 15000  # a click/fill that can't happen in 15 s is reported to the AI instead of hanging


def load_config() -> dict:
    cfg = {}
    f = HOME / "config.env"
    if f.exists():
        for line in f.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    for k in ("SERVER_URL", "HELPER_TOKEN", "PLATFORMS", "BROWSER_CHANNEL", "BROWSER_PATH", "HEADLESS"):
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    return cfg


class Api:
    def __init__(self, base: str, token: str):
        self.http = httpx.AsyncClient(base_url=base.rstrip("/"), headers={"Authorization": f"Bearer {token}"},
                                      timeout=120)

    async def _post(self, path: str, **kw) -> dict:
        r = await self.http.post(path, **kw)
        r.raise_for_status()
        return r.json()

    async def next(self, platforms: list[str]) -> dict | None:
        return (await self._post("/api/helper/next", json={"platforms": platforms}))["task"]

    async def plan(self, task_id: int, goal: str, snapshot: dict, history: list) -> dict:
        return await self._post(f"/api/helper/tasks/{task_id}/plan",
                                json={"goal": goal, "snapshot": snapshot, "history": history})

    async def filled(self, task_id: int, png: bytes, note: str | None) -> None:
        await self._post(f"/api/helper/tasks/{task_id}/filled", content=png, params={"note": note or ""},
                         headers={"Content-Type": "image/png"})

    async def decision(self, task_id: int) -> str:
        r = await self.http.get(f"/api/helper/tasks/{task_id}/decision")
        r.raise_for_status()
        return r.json()["decision"]

    async def no_answer(self, task_id: int) -> None:
        await self._post(f"/api/helper/tasks/{task_id}/no_answer")

    async def published(self, task_id: int, url: str | None) -> None:
        await self._post(f"/api/helper/tasks/{task_id}/published", json={"url": url})

    async def failed(self, task_id: int, error: str, reason: str = "error") -> None:
        await self._post(f"/api/helper/tasks/{task_id}/failed", json={"error": error[:900], "reason": reason})

    async def removed(self, task_id: int) -> None:
        await self._post(f"/api/helper/tasks/{task_id}/removed")

    async def photo(self, photo_id: int) -> bytes:
        r = await self.http.get(f"/api/helper/photo/{photo_id}")
        r.raise_for_status()
        return r.content


# ---------- the page as the AI sees it ----------

SNAPSHOT_JS = r"""
() => {
  document.querySelectorAll('[data-lh-id]').forEach(e => e.removeAttribute('data-lh-id'));
  const sel = 'input, textarea, select, button, a[href], [role=button], [role=option], [role=menuitem], ' +
              '[role=radio], [role=checkbox], [role=combobox], [role=tab], [role=link], [contenteditable=true], ' +
              '[tabindex]:not([tabindex="-1"])';
  const visible = e => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const clean = t => (t || '').replace(/\s+/g, ' ').trim().slice(0, 90);
  const labelOf = e => {
    if (e.getAttribute('aria-label')) return e.getAttribute('aria-label');
    if (e.id) { const l = document.querySelector('label[for="' + CSS.escape(e.id) + '"]'); if (l) return l.innerText; }
    const p = e.closest('label'); if (p) return p.innerText;
    return e.getAttribute('placeholder') || e.getAttribute('name') || e.getAttribute('title') || '';
  };
  const out = []; let n = 0;
  for (const e of document.querySelectorAll(sel)) {
    const isFile = e.tagName === 'INPUT' && e.type === 'file';
    if (!isFile && !visible(e)) continue;
    if (e.tagName === 'INPUT' && e.type === 'hidden') continue;
    if (out.length >= 300) break;
    e.setAttribute('data-lh-id', String(++n));
    const item = { id: n, tag: e.tagName.toLowerCase() };
    if (e.type) item.type = e.type;
    if (e.getAttribute('role')) item.role = e.getAttribute('role');
    const text = clean(e.innerText || e.value); if (text && e.tagName !== 'SELECT') item.text = text;
    const label = clean(labelOf(e)); if (label && label !== text) item.label = label;
    if ((e.tagName === 'INPUT' || e.tagName === 'TEXTAREA') && e.value && !isFile) item.value = clean(e.value);
    if (e.tagName === 'SELECT') item.options = [...e.options].slice(0, 60).map(o => clean(o.text));
    if (e.checked) item.checked = true;
    if (e.disabled) item.disabled = true;
    if (e.tagName === 'A') item.href = e.href.slice(0, 150);
    out.push(item);
  }
  const msgs = [...document.querySelectorAll('[role=alert], .error, .errors, .alert, [class*="error"]')]
    .filter(visible).map(e => clean(e.innerText)).filter(Boolean).slice(0, 10);
  const heads = [...document.querySelectorAll('h1, h2, h3')].filter(visible).map(e => clean(e.innerText))
    .filter(Boolean).slice(0, 10);
  return { url: location.href, title: document.title, headings: heads, messages: msgs, elements: out };
}
"""


async def snapshot(page: Page) -> dict:
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:  # noqa: BLE001 - a slow page is still worth a look
        pass
    return await page.evaluate(SNAPSHOT_JS)


def pick_option(options: list[str], wanted: str) -> str | None:
    """Exact (case-insensitive) match first, then the first option containing the wanted text."""
    w = wanted.strip().lower()
    if not w:
        return None
    return next((o for o in options if o.strip().lower() == w), None) or \
        next((o for o in options if w in o.lower()), None)


async def human_pause(lo: float = 0.6, hi: float = 1.8) -> None:
    await asyncio.sleep(random.uniform(lo, hi))


async def do_action(page: Page, action: dict, elements: dict[int, dict], photos: list[str], *, allow_publish: bool) -> str:
    el_id, kind, value = action.get("element"), action.get("action"), action.get("value")
    info = elements.get(el_id)
    if info is None:
        return f"element {el_id} not found"
    loc = page.locator(f'[data-lh-id="{el_id}"]').first
    if kind == "click" and not allow_publish and info.get("tag") != "a" \
            and looks_like_publish(f"{info.get('text', '')} {info.get('label', '')}"):
        return "refused: this looks like the final publish button — the owner confirms it in the bot first"
    if kind == "fill":
        text = value or ""
        await loc.click()
        await loc.fill("")
        if len(text) <= 120:
            await loc.press_sequentially(text, delay=random.randint(25, 70))
        else:
            await loc.fill(text)
    elif kind == "select":
        match = pick_option(info.get("options") or [], value or "")
        if match is None:
            return f"option {value!r} not found; options: {info.get('options')}"
        await loc.select_option(label=match)
    elif kind == "click":
        await loc.click()
    elif kind == "check":
        try:
            await loc.check()
        except Exception:  # noqa: BLE001 - custom checkboxes
            await loc.click()
    elif kind == "upload_photos":
        await loc.set_input_files(photos)
    else:
        return f"unknown action {kind}"
    await human_pause()
    return "ok"


async def run_agent(page: Page, api: Api, task: dict, goal: str, photos: list[str], *,
                    allow_publish: bool = False, max_steps: int = MAX_STEPS) -> tuple[dict, list]:
    """Snapshot → plan → act, until the AI says ready/done/logged_out/blocked."""
    history: list[dict] = []
    plan: dict = {}
    for _ in range(max_steps):
        snap = await snapshot(page)
        elements = {e["id"]: e for e in snap["elements"]}
        plan = await api.plan(task["listing_id"], goal, snap, history)
        if plan["state"] != "continue":
            return plan | {"_snapshot": snap}, history
        if not plan["actions"]:
            history.append({"note": "no actions returned; page may still be loading"})
            await asyncio.sleep(3)
            continue
        for a in plan["actions"]:
            try:
                result = await do_action(page, a, elements, photos, allow_publish=allow_publish)
            except Exception as e:  # noqa: BLE001 - reported to the AI, which tries another way
                result = f"error: {type(e).__name__}: {str(e)[:150]}"
            history.append({**a, "result": result, "url": page.url})
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:  # noqa: BLE001
            pass
    return {"state": "blocked", "note_ru": f"не справился за {max_steps} шагов"}, history


async def publish(ctx: BrowserContext, api: Api, task: dict, tmp: Path, decision_timeout: int = DECISION_TIMEOUT,
                  poll: float = 10) -> str:
    photos = []
    for pid in task.get("photo_ids") or []:
        p = tmp / f"{pid}.jpg"
        p.write_bytes(await api.photo(pid))
        photos.append(str(p))
    page = await ctx.new_page()
    page.set_default_timeout(ACTION_TIMEOUT_MS)
    try:
        await page.goto(task.get("start_url") or SITES[task["platform"]]["start"], wait_until="domcontentloaded")
        await human_pause(1.5, 3)
        plan, _ = await run_agent(page, api, task, "publish", photos)
        if plan["state"] in ("logged_out", "blocked"):
            await api.failed(task["listing_id"], plan.get("note_ru") or plan["state"], plan["state"])
            return plan["state"]
        if plan["state"] != "ready" or not plan.get("publish_element"):
            await api.failed(task["listing_id"], plan.get("note_ru") or "form not completed")
            return "failed"
        publish_id = plan["publish_element"]
        await page.locator(f'[data-lh-id="{publish_id}"]').first.scroll_into_view_if_needed()
        await api.filled(task["listing_id"], await page.screenshot(full_page=True), plan.get("note_ru"))

        deadline = time.monotonic() + decision_timeout
        decision = "pending"
        while decision == "pending" and time.monotonic() < deadline:
            await asyncio.sleep(poll)
            decision = await api.decision(task["listing_id"])
        if decision == "pending":
            await api.no_answer(task["listing_id"])
            return "no_answer"
        if decision != "publish":
            return "cancelled"

        await page.locator(f'[data-lh-id="{publish_id}"]').first.click()
        try:
            await page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:  # noqa: BLE001
            pass
        await human_pause(2, 4)
        after, _ = await run_agent(page, api, task, "after_publish", photos, allow_publish=True, max_steps=5)
        if after["state"] in ("logged_out", "blocked"):
            await api.failed(task["listing_id"], "после публикации: " + (after.get("note_ru") or after["state"]),
                             after["state"])
            return after["state"]
        await api.published(task["listing_id"], after.get("ad_url") or page.url)
        return "published"
    except Exception as e:  # noqa: BLE001 - reported, the server retries later
        log.exception("Publishing task %s failed", task.get("listing_id"))
        await api.failed(task["listing_id"], f"{type(e).__name__}: {e}")
        return "failed"
    finally:
        await page.close()


async def remove(ctx: BrowserContext, api: Api, task: dict) -> str:
    page = await ctx.new_page()
    page.set_default_timeout(ACTION_TIMEOUT_MS)
    page.on("dialog", lambda d: asyncio.ensure_future(d.accept()))
    try:
        if not task.get("url"):
            await api.failed(task["listing_id"], "нет ссылки на объявление — сними его вручную")
            return "failed"
        await page.goto(task["url"], wait_until="domcontentloaded")
        await human_pause(1.5, 3)
        plan, _ = await run_agent(page, api, task, "remove", [], allow_publish=True, max_steps=10)
        if plan["state"] == "done":
            await api.removed(task["listing_id"])
            return "removed"
        await api.failed(task["listing_id"], plan.get("note_ru") or plan["state"],
                         plan["state"] if plan["state"] in ("logged_out", "blocked") else "error")
        return plan["state"]
    except Exception as e:  # noqa: BLE001
        log.exception("Removing task %s failed", task.get("listing_id"))
        await api.failed(task["listing_id"], f"{type(e).__name__}: {e}")
        return "failed"
    finally:
        await page.close()


# ---------- commands ----------

async def open_context(pw, cfg: dict, headless: bool = False) -> BrowserContext:
    PROFILE.mkdir(parents=True, exist_ok=True)
    kw = {"headless": headless, "viewport": {"width": 1280, "height": 900}, "locale": "en-GB",
          "timezone_id": "Asia/Nicosia"}
    if cfg.get("BROWSER_PATH"):
        kw["executable_path"] = cfg["BROWSER_PATH"]
    else:
        kw["channel"] = cfg.get("BROWSER_CHANNEL", "chrome")  # your installed Google Chrome
    return await pw.chromium.launch_persistent_context(str(PROFILE), **kw)


async def cmd_login(cfg: dict, platform: str) -> None:
    site = SITES[platform]
    async with async_playwright() as pw:
        ctx = await open_context(pw, cfg)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(site["login"])
        print(f"Войди в {site['name']} в открывшемся окне, потом просто закрой окно браузера.")
        while ctx.pages:
            await asyncio.sleep(1)
        await ctx.close()
    print("Готово: вход сохранён в профиле помощника.")


async def cmd_check(cfg: dict) -> None:
    api = Api(cfg["SERVER_URL"], cfg["HELPER_TOKEN"])
    await api.next([])  # no platforms: only marks the helper as online
    print("Сервер доступен, токен верный. В боте /platforms покажет «помощник на связи».")


async def cmd_run(cfg: dict) -> None:
    api = Api(cfg["SERVER_URL"], cfg["HELPER_TOKEN"])
    platforms = [p.strip() for p in cfg.get("PLATFORMS", "bazaraki,kyma,facebook").split(",") if p.strip()]
    log.info("Helper started for %s", ", ".join(platforms))
    async with async_playwright() as pw:
        ctx = await open_context(pw, cfg, headless=cfg.get("HEADLESS") == "1")
        while True:
            try:
                task = await api.next(platforms)
            except httpx.HTTPError as e:
                log.warning("Server unreachable: %s", e)
                task = None
            if task is None:
                await asyncio.sleep(random.uniform(*IDLE_SLEEP))
                continue
            log.info("Task %s: %s on %s (item #%s)", task["listing_id"], task["kind"], task["platform"], task["item_id"])
            with tempfile.TemporaryDirectory() as tmp:
                result = await (publish(ctx, api, task, Path(tmp)) if task["kind"] == "publish" else remove(ctx, api, task))
            log.info("Task %s: %s", task["listing_id"], result)
            await asyncio.sleep(random.uniform(20, 60))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config()
    args = sys.argv[1:]
    if not args or args[0] not in ("login", "check", "run"):
        print(__doc__)
        sys.exit(1)
    if args[0] == "login":
        if len(args) < 2 or args[1] not in SITES:
            print("Укажи сайт: " + ", ".join(SITES))
            sys.exit(1)
        asyncio.run(cmd_login(cfg, args[1]))
        return
    missing = [k for k in ("SERVER_URL", "HELPER_TOKEN") if not cfg.get(k)]
    if missing:
        print(f"Не заданы: {', '.join(missing)} — пропиши их в {HOME / 'config.env'}")
        sys.exit(1)
    asyncio.run(cmd_check(cfg) if args[0] == "check" else cmd_run(cfg))


if __name__ == "__main__":
    main()
