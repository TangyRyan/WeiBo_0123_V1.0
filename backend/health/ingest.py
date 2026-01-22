from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional

from backend.health.constants import DEFAULT_WINDOW_HOURS, HEALTH_CATEGORY_TREE
from backend.health.models import HealthEvent, TimelinePoint
from backend.storage import load_daily_archive
from spider.crawler_core import CHINA_TZ, slugify_title

HASHTAG_PATTERN = re.compile(r"#([^#]+)#")


def load_health_events(
    *,
    target_date: Optional[str] = None,
    hours: Optional[int] = None,
    now: Optional[datetime] = None,
) -> List[HealthEvent]:
    """Load health-topic events from archive JSON within a time window."""

    window_hours = max(1, hours or DEFAULT_WINDOW_HOURS)
    clock = now or datetime.now(tz=CHINA_TZ)
    if target_date:
        clock = datetime.strptime(target_date, "%Y-%m-%d").replace(tzinfo=CHINA_TZ) + timedelta(
            hours=23,
            minutes=59,
            seconds=59,
        )
    window_start = clock - timedelta(hours=window_hours)
    date_cursor = window_start.date()
    end_date = clock.date()

    events: List[HealthEvent] = []
    seen_ids: set[str] = set()

    while date_cursor <= end_date:
        date_str = date_cursor.strftime("%Y-%m-%d")
        archive = load_daily_archive(date_str)
        for name, payload in archive.items():
            event = _coerce_health_event(name, payload, date_str)
            if not event:
                continue
            if event.end_ts < int(window_start.timestamp()):
                continue
            if event.start_ts > int(clock.timestamp()):
                continue
            if event.event_id in seen_ids:
                continue
            seen_ids.add(event.event_id)
            events.append(event)
        date_cursor += timedelta(days=1)

    events.sort(key=lambda item: (item.start_ts, item.heat_peak), reverse=False)
    return events


def _coerce_health_event(name: str, data: Dict[str, Any], date_str: str) -> Optional[HealthEvent]:
    llm_block = data.get("llm") or {}
    topic_type = (llm_block.get("topic_type") or data.get("topic_type") or "").strip()
    if topic_type != "健康":
        return None
    major = llm_block.get("health_major") or data.get("health_major")
    minor = llm_block.get("health_minor") or data.get("health_minor")
    if major not in HEALTH_CATEGORY_TREE:
        return None
    valid_minors = HEALTH_CATEGORY_TREE.get(major, [])
    if minor not in valid_minors:
        minor = minor or "未细分"

    sentiment = _to_float(llm_block.get("sentiment"), default=0.0)
    region = (llm_block.get("region") or "未知").strip() or "未知"
    sentiment_vector = _extract_sentiment_vector(llm_block)

    start_ts = _extract_timestamp(data.get("first_seen"), date_str, default_hour=0)
    end_ts = _extract_timestamp(data.get("last_seen"), date_str, default_hour=23)

    raw_points = _build_raw_points(date_str, data, start_ts, end_ts)
    heat_peak = max((point.heat for point in raw_points), default=_coerce_heat(data))

    title = data.get("title") or name
    slug = data.get("slug") or slugify_title(name) or f"evt-{abs(hash(name)) % 10000}"
    event_id = f"{date_str}-{slug}"

    posts = _prepare_posts(data.get("posts") or [])
    tags = _extract_tags(data.get("tags"), posts)
    summary = data.get("description") or data.get("summary") or ""

    return HealthEvent(
        event_id=event_id,
        date=date_str,
        name=name,
        title=title,
        category=major,
        health_minor=minor,
        sentiment=sentiment,
        sentiment_vector=sentiment_vector,
        region=region,
        start_ts=start_ts,
        end_ts=end_ts,
        heat_peak=heat_peak,
        raw_points=raw_points,
        tags=tags,
        posts=posts,
        summary=summary,
        meta={"source_slug": slug},
    )


def _build_raw_points(
    date_str: str,
    data: Dict[str, Any],
    start_ts: int,
    end_ts: int,
) -> List[TimelinePoint]:
    hot_values = data.get("hot_values") or data.get("heat_values")
    if isinstance(hot_values, dict) and hot_values:
        points = []
        for key, value in hot_values.items():
            ts = _coerce_timeslot(key, date_str)
            if ts is None:
                continue
            points.append(
                TimelinePoint(
                    ts=ts,
                    heat=_to_float(value),
                    rank=None,
                )
            )
        if points:
            points.sort(key=lambda item: item.ts)
            return points

    appeared_hours = data.get("appeared_hours")
    if isinstance(appeared_hours, list) and appeared_hours:
        points = []
        for hour in appeared_hours:
            ts = _coerce_hour_slot(date_str, hour)
            if ts is None:
                continue
            points.append(TimelinePoint(ts=ts, heat=_coerce_heat(data), rank=None))
        if points:
            return points

    fallback_heat = _coerce_heat(data)
    fallback = [
        TimelinePoint(ts=start_ts, heat=fallback_heat, rank=None),
    ]
    if end_ts != start_ts:
        fallback.append(TimelinePoint(ts=end_ts, heat=fallback_heat, rank=None))
    return fallback


def _coerce_hour_slot(date_str: str, hour_value: Any) -> Optional[int]:
    try:
        hour = int(str(hour_value).strip()[:2])
    except Exception:
        return None
    if hour < 0 or hour > 23:
        return None
    base = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=CHINA_TZ)
    return int((base + timedelta(hours=hour)).timestamp())


def _coerce_timeslot(tag: Any, date_str: str) -> Optional[int]:
    if isinstance(tag, (int, float)):
        return int(tag)
    text = str(tag or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return int(datetime.strptime(text, fmt).timestamp())
        except ValueError:
            continue
    if len(text) in {4, 5}:
        text = text.zfill(4)
        hour = int(text[:2])
        minute = int(text[2:])
        base = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=CHINA_TZ)
        return int((base + timedelta(hours=hour, minutes=minute)).timestamp())
    return None


def _extract_tags(raw_tags: Any, posts: List[Dict[str, Any]]) -> List[str]:
    tags: List[str] = []
    if isinstance(raw_tags, list):
        tags.extend(str(tag).strip("# ").strip() for tag in raw_tags if tag)
    for post in posts:
        for match in HASHTAG_PATTERN.findall(post.get("content_text") or ""):
            candidate = match.strip()
            if candidate:
                tags.append(candidate)
    deduped: List[str] = []
    seen: set[str] = set()
    for tag in tags:
        normalized = tag.strip()
        if not normalized:
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)
    return deduped[:50]


def _prepare_posts(raw_posts: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    prepared: List[Dict[str, Any]] = []
    for post in raw_posts:
        if not isinstance(post, dict):
            continue
        prepared.append({
            "post_id": post.get("post_id"),
            "published_at": post.get("published_at"),
            "account_name": post.get("account_name"),
            "content_text": (post.get("content_text") or "")[:500],
            "reposts": post.get("reposts", 0),
            "comments": post.get("comments", 0),
            "likes": post.get("likes", 0),
        })
        if len(prepared) >= 50:
            break
    return prepared


def _extract_timestamp(value: Optional[str], date_str: str, *, default_hour: int) -> int:
    if value:
        try:
            return int(datetime.fromisoformat(value).timestamp())
        except ValueError:
            pass
    base = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=CHINA_TZ)
    base = base + timedelta(hours=default_hour)
    return int(base.timestamp())


def _coerce_heat(data: Dict[str, Any]) -> float:
    for key in ("hot", "heat", "hot_value", "readCount"):
        if key in data:
            return _to_float(data.get(key), default=0.0)
    return 0.0


def _to_float(value: Any, *, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().lower()
    if not text:
        return default
    multiplier = 1.0
    if text.endswith(("w", "万")):
        text = text[:-1]
        multiplier = 10000.0
    try:
        return float(text) * multiplier
    except ValueError:
        return default


def _extract_sentiment_vector(llm_block: Dict[str, Any]) -> Dict[str, float]:
    candidate = llm_block.get("sentiment_vector") or llm_block.get("emotions") or {}
    if not isinstance(candidate, dict):
        return {}
    vector: Dict[str, float] = {}
    for key, value in candidate.items():
        try:
            vector[str(key)] = float(value)
        except (TypeError, ValueError):
            continue
    return vector
