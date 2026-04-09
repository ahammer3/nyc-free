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


def categorize_event(title: str):
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


def _extract_image(event: dict):
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


# Day-of-week mapping: name → Python weekday (0=Mon, 6=Sun)
_DAY_MAP = {
    "mon": 0, "monday": 0,
    "tue": 1, "tuesday": 1,
    "wed": 2, "wednesday": 2,
    "thu": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}


def _day_name_to_int(name: str):
    """Convert a day name (or abbreviation) to Python weekday int (0=Mon, 6=Sun)."""
    return _DAY_MAP.get(name.lower().rstrip("s"))


def _expand_day_range(start_day: str, end_day: str):
    """Expand 'Tuesday-Saturday' into a set of weekday ints."""
    s = _day_name_to_int(start_day)
    e = _day_name_to_int(end_day)
    if s is None or e is None:
        return set()
    days = set()
    i = s
    while True:
        days.add(i)
        if i == e:
            break
        i = (i + 1) % 7
    return days


def _extract_active_days(body_html: str):
    """Extract which days of the week an event is active from its description.

    Returns a set of Python weekday ints (0=Mon, 6=Sun), or None if
    no day restrictions were found (meaning assume all days).
    """
    if not body_html:
        return None

    text = BeautifulSoup(body_html, "html.parser").get_text(separator=" ", strip=True)
    text = unescape(re.sub(r"\s+", " ", text))

    # Check for 'daily' / 'everyday' / 'every day' → all days
    if re.search(r"(?i)\b(daily|every\s*day|everyday|7\s*days\s*a\s*week)\b", text):
        return None  # no restriction

    active_days = set()

    # Pattern 1: Day ranges like "Tuesday-Saturday", "Mon–Fri", "Thursday — Sunday"
    day_ranges = re.findall(
        r"(?i)\b(mon(?:day)?|tue(?:sday)?|wed(?:nesday)?|thu(?:rsday)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)"
        r"\s*[-–—]\s*"
        r"(mon(?:day)?|tue(?:sday)?|wed(?:nesday)?|thu(?:rsday)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)\b",
        text,
    )
    for start_d, end_d in day_ranges:
        active_days |= _expand_day_range(start_d, end_d)

    # Pattern 2: Standalone day names followed by times or colons
    # e.g. "Fridays: 9-10AM", "Saturdays 4-5PM", "Fridays 9–10AM | Saturdays 9–10AM"
    day_schedule = re.findall(
        r"(?i)\b(mon(?:day)?s?|tue(?:sday)?s?|wed(?:nesday)?s?|thu(?:rsday)?s?|fri(?:day)?s?|sat(?:urday)?s?|sun(?:day)?s?)"
        r"\s*[:|]\s*\d{1,2}",
        text,
    )
    for d in day_schedule:
        di = _day_name_to_int(d)
        if di is not None:
            active_days.add(di)

    # Pattern 3: "Every [day]" e.g. "Every Saturday"
    every_matches = re.findall(
        r"(?i)\bevery\s+(mon(?:day)?|tue(?:sday)?|wed(?:nesday)?|thu(?:rsday)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)\b",
        text,
    )
    for d in every_matches:
        di = _day_name_to_int(d)
        if di is not None:
            active_days.add(di)

    return active_days if active_days else None


def _parse_time_to_minutes(t: str):
    """Parse a time string like '11AM', '6:00PM', '3 PM' into minutes since midnight."""
    t = t.strip().lower().replace(".", "")
    m = re.match(r"(\d{1,2})\s*(?::(\d{2}))?\s*(am|pm)", t)
    if not m:
        return None
    hour = int(m.group(1))
    mins = int(m.group(2)) if m.group(2) else 0
    ampm = m.group(3)
    if ampm == "pm" and hour != 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    return hour * 60 + mins


def _extract_operating_hours(body_html: str):
    """Extract operating hours from event description HTML.

    Returns dict with 'open' and 'close' as minutes since midnight,
    representing the most common/general operating window.
    Returns None if no hours found.
    """
    if not body_html:
        return None

    text = BeautifulSoup(body_html, "html.parser").get_text(separator=" ", strip=True)
    text = unescape(re.sub(r"\s+", " ", text))

    # Match time ranges like "11AM-6PM", "10:00 am - 8:00 pm", "3 PM to 6 PM"
    ranges = re.findall(
        r"(\d{1,2}\s*(?::\d{2})?\s*(?:am|pm|a\.?m\.?|p\.?m\.?))"
        r"\s*[-\u2013\u2014]+\s*"
        r"(\d{1,2}\s*(?::\d{2})?\s*(?:am|pm|a\.?m\.?|p\.?m\.?))",
        text,
        re.IGNORECASE,
    )

    if not ranges:
        return None

    # Parse all ranges and find the widest window (most generous hours)
    earliest_open = None
    latest_close = None

    for open_str, close_str in ranges:
        open_mins = _parse_time_to_minutes(open_str)
        close_mins = _parse_time_to_minutes(close_str)
        if open_mins is None or close_mins is None:
            continue
        if close_mins <= open_mins:
            continue  # skip nonsensical ranges

        if earliest_open is None or open_mins < earliest_open:
            earliest_open = open_mins
        if latest_close is None or close_mins > latest_close:
            latest_close = close_mins

    if earliest_open is not None and latest_close is not None:
        return {"open": earliest_open, "close": latest_close}

    return None


def _ms_to_datetime(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def fetch_events():
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
        is_multi_day = start_dt.date() != end_dt.date()

        # For multi-day events, try to extract real operating hours and active days
        operating_hours = None
        active_days = None  # None = all days, else set of weekday ints
        if is_multi_day:
            body_html = raw.get("body", "")
            operating_hours = _extract_operating_hours(body_html)
            active_days_set = _extract_active_days(body_html)
            if active_days_set is not None:
                active_days = sorted(active_days_set)  # list for JSON

        event = {
            "id": raw.get("id", ""),
            "title": raw.get("title", "Untitled Event"),
            "slug": raw.get("urlId", ""),
            "event_url": f"{SITE_BASE}/events/{raw.get('urlId', '')}",
            "start_date": start_dt.isoformat(),
            "end_date": end_dt.isoformat(),
            "start_ts": raw["startDate"],
            "end_ts": raw["endDate"],
            "is_multi_day": is_multi_day,
            "operating_hours": operating_hours,
            "active_days": active_days,
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
