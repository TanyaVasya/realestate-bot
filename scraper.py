"""Fetch a listing page and gather raw signals for the LLM to structure.

We do not hand-parse every field: layouts change and the sites are hostile to
scraping. Instead we collect the high-signal, machine-readable bits these sites
embed (JSON-LD, OpenGraph, title, meta description, a text snippet) and let
Claude turn that into clean fields.
"""
import json
import os
import re
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

import config

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-AU,en;q=0.9",
}

URL_RE = re.compile(r"https?://[^\s]+")

# e.g. /property-apartment-nsw-waterloo-151028456  ->  type, state, suburb, id
SLUG_RE = re.compile(
    r"/property-(?P<type>[a-z+]+)-(?P<state>[a-z]{2,3})-(?P<suburb>[^/]+?)-(?P<id>\d+)"
)


def find_listing_url(text: str) -> str | None:
    """Return the first real-estate URL found in a message, if any."""
    for match in URL_RE.findall(text or ""):
        host = urlparse(match).netloc.lower().lstrip("www.")
        if any(h in host for h in config.LISTING_HOSTS):
            return match.rstrip(").,")
    return None


def remove_urls(text: str) -> str:
    """The message text with any URLs stripped (the human's own comment)."""
    return URL_RE.sub("", text or "").strip()


def parse_url(url: str) -> dict:
    """Extract whatever the URL itself reveals. Works even when fetching fails."""
    m = SLUG_RE.search(urlparse(url).path)
    if not m:
        return {}
    suburb = m.group("suburb").replace("+", " ").replace("-", " ").title()
    return {
        "property_type": m.group("type").replace("+", " ").title(),
        "suburb": f"{suburb}, {m.group('state').upper()}",
        "listing_id": m.group("id"),
    }


GOOGLEBOT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


# A persistent Chrome profile so Akamai's clearance cookies stick between
# runs (first fetch after a reboot is a little slower, then fast).
PROFILE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".chrome-profile")


def _fetch_browser(url: str) -> str:
    """Fetch via a real Chrome (patchright stealth) — beats Akamai/Kasada.

    Headed but positioned off-screen so no window bothers the user. Requires
    Google Chrome installed and a desktop session (i.e. running locally).
    """
    from patchright.sync_api import sync_playwright

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR,
            channel="chrome",
            headless=False,
            no_viewport=True,
            args=["--window-position=-3000,-3000", "--window-size=1280,900"],
        )
        try:
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            # Give Akamai's JS challenge time to resolve and the listing to render.
            page.wait_for_timeout(6000)
            return page.content()
        finally:
            ctx.close()


def _fetch_html(url: str) -> str:
    """Plain fetch (fast), then a real browser, then ZenRows if configured."""
    for headers in (BROWSER_HEADERS, GOOGLEBOT_HEADERS):
        try:
            resp = httpx.get(url, headers=headers, timeout=25, follow_redirects=True)
            if resp.status_code == 200 and len(resp.text) > 2000:
                return resp.text
        except httpx.HTTPError:
            pass

    # Real Chrome: the reliable path for Akamai-protected sites (realestate.com.au).
    try:
        html = _fetch_browser(url)
        if len(html) > 5000:
            return html
    except Exception:  # noqa: BLE001 - Chrome missing / no display / timeout
        pass

    if config.ZENROWS_API_KEY:
        params = {"url": url, "apikey": config.ZENROWS_API_KEY, "js_render": "true"}
        resp = httpx.get("https://api.zenrows.com/v1/", params=params, timeout=60)
        resp.raise_for_status()
        return resp.text

    raise RuntimeError("blocked")


def scrape(url: str) -> dict:
    """Return raw signals: {source, url, url_fields, json_ld, og, title, text}.

    Never raises: if the page can't be fetched, returns what the URL alone tells
    us so the listing is still saved with type/suburb/id.
    """
    host = urlparse(url).netloc.lower().lstrip("www.")
    source = next((h for h in config.LISTING_HOSTS if h in host), host)
    url_fields = parse_url(url)
    base = {"source": source, "url": url, "url_fields": url_fields,
            "json_ld": [], "og": {}, "title": "", "image_url": "", "text": ""}

    try:
        html = _fetch_html(url)
    except Exception:  # noqa: BLE001 - blocked / timeout: fall back to URL only
        base["blocked"] = True
        return base

    soup = BeautifulSoup(html, "html.parser")

    json_ld = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            json_ld.append(json.loads(tag.string or "{}"))
        except (json.JSONDecodeError, TypeError):
            continue

    og = {
        m.get("property", m.get("name")): m.get("content")
        for m in soup.find_all("meta")
        if m.get("content") and (m.get("property", "").startswith("og:")
                                 or m.get("name", "") == "description")
    }

    body_text = soup.get_text(" ", strip=True)

    base.update({
        "json_ld": json_ld,
        "og": og,
        "title": (soup.title.string if soup.title else "") or "",
        "image_url": og.get("og:image", ""),
        # Trim: enough for the model to find price/beds/inspection, not the whole DOM.
        "text": body_text[:6000],
    })
    return base
