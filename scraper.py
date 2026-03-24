"""Fetch and parse events from nycforfree.co Squarespace JSON API."""

import time
import re
from datetime import datetime, timezone
from html import unescape

import requests
from bs4 import BeautifulSoup

EVENTS_URL = "https://www.nycforfree.co/events?format=json"
SITE_BASE = "https://www.nycforfree.co"

# Default Squarespace map center — events at this exact point have no real location
DEFAULT_LAT = 40.7207559
DEFAULT_LNG = -74.0007613

_cache = {"events": None, "fetched_at": 0}
CACHE_TTL = 3600  # 1 hour

# ── Category classification (title-only keyword matching) ──────────────────
CATEGORY_RULES = [
    ("Food & Drink", [
        r"coffee", r"cafe\b", r"bakery", r"bagel", r"pizza", r"burger",
        r"ice\b.*\b(cream|first)", r"carvel", r"sweetgreen", r"matcha",
        r"candy", r"cookie", r"cone day", r"diner", r"happy hour",
        r"tasting", r"giveaway", r"butterbeer", r"rita.s italian",
        r"free cone", r"posh\b", r"ben\s*&\s*jerry", r"dough\b",
        r"saucer", r"taiyaki", r"donut", r"waffle", r"brunch",
    ]),
    ("Art & Museums", [
        r"museum", r"gallery", r"exhibit", r"biennial", r"sculpture",
        r"cinema", r"screening", r"new museum", r"bric", r"metrocard",
        r"whitney", r"\bfit\b", r"art\s*(x|house|\b)", r"painting",
        r"photo\b", r"portrait", r"installation",
    ]),
    ("Music & Entertainment", [
        r"movie", r"arcade", r"pac.?man", r"bts\b", r"k.?pop",
        r"spotify", r"hannah montana", r"analog sunday", r"concert",
        r"music", r"\bdj\b", r"karaoke", r"comedy", r"theater",
        r"theatre", r"melodies", r"meet\s*(and|&)\s*greet",
    ]),
    ("Sports & Fitness", [
        r"run club", r"fitness", r"yoga", r"pilates", r"hoops",
        r"skate", r"skating", r"rink", r"soccer", r"\bfc\b",
        r"yankees", r"islanders", r"knick", r"orange.?theory",
        r"play day", r"world cup", r"pre.?match", r"basketball",
        r"marathon", r"cycling", r"bike\b",
    ]),
    ("Shopping & Pop-Ups", [
        r"pop.?up", r"grand opening", r"re.?opening",
        r"uniqlo", r"century 21", r"gift card", r"mystery gift",
        r"stoney clover", r"muji", r"barnes.*noble", r"b&n",
        r"vannest", r"betsey johnson", r"lego\b",
        r"sample sale", r"flash sale",
    ]),
    ("Community & Culture", [
        r"volunteer", r"parade", r"festival", r"celebration",
        r"\beid\b", r"equinox", r"earth day", r"tartan", r"book fair",
        r"wikipedia", r"community", r"chaand raat", r"flower show",
        r"public tour", r"garden", r"governors island",
        r"latino", r"heritage", r"craft night", r"spring.*cutback",
    ]),
    ("Beauty & Wellness", [
        r"beauty", r"fragrance", r"skincare", r"wellness",
        r"luna daily", r"summer fridays", r"nudestix", r"kiehl",
        r"balmain", r"ouai", r"laneige", r"eilish",
        r"malin.goetz", r"spa\b", r"facial",
    ]),
    ("Kids & Family", [
        r"easter bunny", r"sesame street", r"hasbro", r"pout.?pout",
        r"dinosaur", r"lego.*build", r"play day", r"storytime",
        r"kids?\b", r"family\b", r"children",
    ]),
]


def categorize_event(title: str) -> list[str]:
    """Assign category tags to an event based on its title."""
    text = title.lower()
    cats = []
    for cat_name, patterns in CATEGORY_RULES:
        for pat in patterns:
            if re.search(pat, text):
                cats.append(cat_name)
                break
    return cats if cats else ["Other"]


def _strip_html(html: str, max_len: int = 300) -> str:
    """Strip HTML tags and return plain text excerpt."""
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    text = unescape(soup.get_text(separator=" ", strip=True))
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_len:
        text = text[:max_len].rsplit(" ", 1)[0] + "..."
    return text


def _extract_image(event: dict) -> str | None:
    """Extract the best image URL for an event."""
    # Body HTML contains the real CDN image URLs via data-src attributes.
    # assetUrl is a Squarespace static path that doesn't serve images directly.
    body = event.get("body", "")
    if body:
        soup = BeautifulSoup(body, "html.parser")
        img = soup.find("img")
        if img:
            url = img.get("data-src") or img.get("src")
            if url:
                return url

    return None


def _has_real_location(location: dict) -> bool:
    """Check if location has a real address (not just default Squarespace coords)."""
    if not location:
        return False
    addr1 = (location.get("addressLine1") or "").strip()
    return len(addr1) > 0


def _ms_to_datetime(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def fetch_events() -> list[dict]:
    """Fetch events from the API, using cache if fresh."""
    now = time.time()
    if _cache["events"] is not None and (now - _cache["fetched_at"]) < CACHE_TTL:
        return _cache["events"]

    resp = requests.get(EVENTS_URL, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    raw_events = data.get("upcoming") or data.get("items") or []
    events = []

    for raw in raw_events:
        location = raw.get("location") or {}
        has_location = _has_real_location(location)

        # Coordinates: the Squarespace default coords (40.7207559, -74.0007613)
        # are useless — nearly all events have the same default. We must geocode.
        lat = None
        lng = None

        # Build address string
        addr_parts = []
        if location.get("addressLine1"):
            addr_parts.append(location["addressLine1"].strip())
        if location.get("addressLine2"):
            addr_parts.append(location["addressLine2"].strip())
        address = ", ".join(addr_parts) if addr_parts else None

        start_dt = _ms_to_datetime(raw["startDate"])
        end_dt = _ms_to_datetime(raw["endDate"])

        event = {
            "id": raw.get("id", ""),
            "title": raw.get("title", "Untitled Event"),
            "slug": raw.get("urlId", ""),
            "event_url": f"{SITE_BASE}/events/{raw.get('urlId', '')}",
            "start_date": start_dt.isoformat(),
            "end_date": end_dt.isoformat(),
            "start_ts": raw["startDate"],
            "end_ts": raw["endDate"],
            "lat": lat,
            "lng": lng,
            "address": address,
            "location_unknown": not has_location,
            "description": _strip_html(raw.get("body", "")),
            "image_url": _extract_image(raw),
            "tags": raw.get("tags") or [],
            "categories": categorize_event(raw.get("title", "")),
        }
        events.append(event)

    _cache["events"] = events
    _cache["fetched_at"] = now
    return events
