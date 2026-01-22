from __future__ import annotations

from datetime import datetime
from typing import Optional

from backend.health.constants import DEFAULT_WINDOW_HOURS
from backend.health.features import build_event_detail
from backend.health.ingest import load_health_events
from backend.health.serializer import (
    acquire_lock,
    list_available_dates,
    load_event_detail as _load_event_detail,
    load_timeline as _load_timeline,
    write_event_detail,
    write_timeline,
)
from backend.health.timeline import build_timeline
from spider.crawler_core import CHINA_TZ


def refresh_health_snapshot(
    *,
    target_date: Optional[str] = None,
    hours: Optional[int] = None,
    now: Optional[datetime] = None,
) -> dict:
    """Ingest archives and regenerate health timeline + event details."""

    events = load_health_events(target_date=target_date, hours=hours, now=now)
    timeline_payload = build_timeline(events, now=now)
    storage_date = target_date or (now or datetime.now(tz=CHINA_TZ)).strftime("%Y-%m-%d")
    timeline_payload.date = storage_date

    with acquire_lock():
        write_timeline(storage_date, timeline_payload)
        for event in events:
            detail = build_event_detail(event)
            write_event_detail(storage_date, detail)
    return timeline_payload.to_dict()


def latest_timeline(date: Optional[str] = None) -> Optional[dict]:
    return _load_timeline(date)


def health_event_detail(event_id: str, date: Optional[str] = None) -> Optional[dict]:
    return _load_event_detail(event_id, date)


def available_dates() -> list[str]:
    return list_available_dates()


__all__ = [
    "refresh_health_snapshot",
    "latest_timeline",
    "health_event_detail",
    "available_dates",
]
