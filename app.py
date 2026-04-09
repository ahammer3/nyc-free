"""FastAPI server for NYC Free Events Map."""

from datetime import date, datetime, timezone

import uvicorn
from fastapi import FastAPI, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from scraper import fetch_events
from geocode import resolve_coordinates

app = FastAPI(title="NYC Free Events Map")
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def index():
    return FileResponse("static/index.html")


@app.get("/api/events")
async def get_events(date: str = Query(None, description="YYYY-MM-DD")):
    """Return events active on the given date (or today)."""
    if date:
        try:
            target = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return {"error": "Invalid date format. Use YYYY-MM-DD."}
    else:
        target = datetime.now(timezone.utc)

    target_start = target.replace(hour=0, minute=0, second=0, microsecond=0)
    target_end = target.replace(hour=23, minute=59, second=59, microsecond=0)

    events = fetch_events()
    events = resolve_coordinates(events)

    # Filter: event overlaps with the target date
    target_weekday = target.weekday()  # 0=Mon, 6=Sun

    filtered = []
    for e in events:
        start_ms = e["start_ts"]
        end_ms = e["end_ts"]
        # Event spans at least part of the target day
        if start_ms <= target_end.timestamp() * 1000 and end_ms >= target_start.timestamp() * 1000:
            # Check day-of-week restrictions for multi-day events
            active_days = e.get("active_days")
            is_active_today = active_days is None or target_weekday in active_days

            filtered.append({
                "id": e["id"],
                "title": e["title"],
                "lat": e["lat"],
                "lng": e["lng"],
                "address": e["address"],
                "start_date": e["start_date"],
                "end_date": e["end_date"],
                "description": e["description"],
                "image_url": e["image_url"],
                "event_url": e["event_url"],
                "location_unknown": e["location_unknown"],
                "tags": e["tags"],
                "categories": e["categories"],
                "is_multi_day": e.get("is_multi_day", False),
                "operating_hours": e.get("operating_hours"),
                "active_days": active_days,
                "is_active_today": is_active_today,
            })

    return filtered


@app.get("/api/dates")
async def get_dates():
    """Return all dates that have at least one event, for the date picker."""
    events = fetch_events()
    dates = set()
    for e in events:
        start = datetime.fromtimestamp(e["start_ts"] / 1000, tz=timezone.utc).date()
        end = datetime.fromtimestamp(e["end_ts"] / 1000, tz=timezone.utc).date()
        d = start
        while d <= end:
            dates.add(d.isoformat())
            d = d.fromordinal(d.toordinal() + 1)
    return sorted(dates)


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
