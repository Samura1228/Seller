import httpx
import pytest

from app.integrations import apify_bazaraki


async def test_search_normalizes_rows(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers["authorization"]
        captured["body"] = request.read()
        return httpx.Response(200, json=[
            {"title": "Sony XM4", "price": 150, "detailUrl": "https://www.bazaraki.com/adv/1/", "location": "Limassol"},
            {"title": "Wanted XM4", "price": None},
            {"title": "Free", "price": 0},
        ])

    real = httpx.AsyncClient
    monkeypatch.setattr(apify_bazaraki.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler)))
    comps = await apify_bazaraki.search("sony wh-1000xm4")
    assert comps == [{"source": "bazaraki", "title": "Sony XM4", "price": 150.0, "url": "https://www.bazaraki.com/adv/1/",
                      "location": "Limassol", "negotiable": None, "posted_at": None}]
    assert "logiover~bazaraki-scraper/run-sync-get-dataset-items" in captured["url"]
    assert captured["auth"] == "Bearer test"
    assert b'"scrapeDetails":false' in captured["body"].replace(b" ", b"")


async def test_http_error_raises(monkeypatch):
    real = httpx.AsyncClient
    monkeypatch.setattr(apify_bazaraki.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(lambda r: httpx.Response(402, text="no credits"))))
    with pytest.raises(apify_bazaraki.ComparablesError, match="402"):
        await apify_bazaraki.search("x")
