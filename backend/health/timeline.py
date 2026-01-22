from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Optional

from backend.health.models import HealthEvent, TimelineEvent, TimelinePayload, TimelinePoint, TimelineSummary
from spider.crawler_core import CHINA_TZ

STEP_SECONDS = 600  # 10 minutes
MAX_POINTS = 6 * 24  # 24h at 10-min granularity


def build_timeline(events: List[HealthEvent], *, now: Optional[datetime] = None) -> TimelinePayload:
    """Transform HealthEvent objects into timeline payload for the API layer."""

    updated_at = (now or datetime.now(tz=CHINA_TZ)).isoformat()
    major_counter: Dict[str, int] = defaultdict(int)
    timeline_events: List[TimelineEvent] = []

    for event in events:
        major_counter[event.category] += 1
        normalized_points = normalize_points(event.raw_points)
        point_count = len(normalized_points)
        display_points = normalized_points[-1:] if normalized_points else []
        if not display_points:
            fallback_ts = event.end_ts or event.start_ts
            display_points = [TimelinePoint(ts=fallback_ts, heat=event.heat_peak, rank=None)]
        timeline_events.append(
            TimelineEvent(
                event_id=event.event_id,
                title=event.title,
                category=event.category,
                start_ts=event.start_ts,
                end_ts=event.end_ts,
                heat_peak=event.heat_peak,
                sentiment=event.sentiment,
                region=event.region,
                point_count=point_count,
                points=display_points,
            )
        )

    summary = TimelineSummary(
        total_events=len(events),
        by_major=dict(sorted(major_counter.items(), key=lambda item: item[0])),
    )
    return TimelinePayload(updated_at=updated_at, summary=summary, events=timeline_events)


def normalize_points(points: List[TimelinePoint]) -> List[TimelinePoint]:
    if not points:
        return []

    buckets: Dict[int, Dict[str, List[float]]] = {}
    for point in points:
        bucket = point.ts - (point.ts % STEP_SECONDS)
        entry = buckets.setdefault(bucket, {"heat": [], "rank": []})
        entry["heat"].append(point.heat)
        if point.rank is not None:
            entry["rank"].append(point.rank)

    normalized: List[TimelinePoint] = []
    for ts in sorted(buckets.keys()):
        entry = buckets[ts]
        avg_heat = sum(entry["heat"]) / max(1, len(entry["heat"]))
        rank = min(entry["rank"]) if entry["rank"] else None
        normalized.append(TimelinePoint(ts=ts, heat=avg_heat, rank=rank))

    if len(normalized) > MAX_POINTS:
        step = max(1, len(normalized) // MAX_POINTS)
        normalized = normalized[::step]

    return normalized
