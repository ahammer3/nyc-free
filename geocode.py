"""Geocoding for events — uses Nominatim with persistent JSON cache."""

import json
import os
import time
import requests

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
HUDSON_RIVER_LAT = 40.7280
HUDSON_RIVER_LNG = -74.0200
CACHE_FILE = os.path.join(os.path.dirname(__file__), "geocode_cache.json")

_geocode_cache: dict[str, list[float] | None] = {}
_last_request = 0.0


def _load_cache():
    global _geocode_cache
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE) as f:
            _geocode_cache = json.load(f)


def _save_cache():
    with open(CACHE_FILE, "w") as f:
        json.dump(_geocode_cache, f, indent=2)


def geocode_address(address: str) -> tuple[float, float] | None:
    """Geocode an address using Nominatim. Returns (lat, lng) or None."""
    if address in _geocode_cache:
        val = _geocode_cache[address]
        return tuple(val) if val else None

    # Rate limit: 1 request per second
    global _last_request
    elapsed = time.time() - _last_request
    if elapsed < 1.1:
        time.sleep(1.1 - elapsed)

    try:
        resp = requests.get(
            NOMINATIM_URL,
            params={"q": address, "format": "json", "limit": 1},
            headers={"User-Agent": "NYCFreeEventsMap/1.0"},
            timeout=10,
        )
        _last_request = time.time()
        resp.raise_for_status()
        results = resp.json()
        if results:
            coords = [float(results[0]["lat"]), float(results[0]["lon"])]
            _geocode_cache[address] = coords
            _save_cache()
            return tuple(coords)
    except Exception as e:
        print(f"  Geocode failed for '{address}': {e}")

    _geocode_cache[address] = None
    _save_cache()
    return None


def resolve_coordinates(events: list[dict]) -> list[dict]:
    """Ensure every event has lat/lng. Geocodes missing ones, assigns
    Hudson River coords for truly unknown locations."""
    _load_cache()

    needs_geocoding = [e for e in events if e["lat"] is None and e["address"]]
    if needs_geocoding:
        cached = sum(1 for e in needs_geocoding if e["address"] in _geocode_cache)
        to_fetch = len(needs_geocoding) - cached
        if to_fetch > 0:
            print(f"Geocoding {to_fetch} addresses (~{to_fetch}s)...")

    hudson_offset = 0
    for event in events:
        if event["lat"] is not None and event["lng"] is not None:
            continue

        # Try geocoding if we have an address
        if event["address"]:
            coords = geocode_address(event["address"])
            if coords:
                event["lat"], event["lng"] = coords
                event["location_unknown"] = False
                continue

        # No location — place in Hudson River, offset slightly for each
        event["lat"] = HUDSON_RIVER_LAT + (hudson_offset * 0.002)
        event["lng"] = HUDSON_RIVER_LNG
        event["location_unknown"] = True
        hudson_offset += 1

    return events
