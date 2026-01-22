from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.settings import DATA_ROOT
from spider.crawler_core import CHINA_TZ

EVENTS_ROOT = DATA_ROOT / "health" / "events"
MIN_TIMELINE_START = "2025-09-01"


def _list_dataset_dirs() -> List[Path]:
    """列出 data/health/events 下所有符合日期格式的目录（升序）。"""
    if not EVENTS_ROOT.exists():
        return []
    dirs: List[Path] = []
    for path in EVENTS_ROOT.iterdir():
        if not path.is_dir():
            continue
        try:
            datetime.strptime(path.name, "%Y-%m-%d")
        except ValueError:
            continue
        dirs.append(path)
    dirs.sort(key=lambda p: p.name)
    return dirs


def dataset_date_range(default_start: str = MIN_TIMELINE_START) -> tuple[Optional[str], Optional[str]]:
    """返回固定起始日和 events 目录最新日期。"""
    dirs = _list_dataset_dirs()
    latest = dirs[-1].name if dirs else None
    return default_start, latest


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _coerce_ts(raw: Any) -> Optional[int]:
    try:
        value = int(raw)
        return value if value > 0 else None
    except Exception:
        return None


def _coerce_float(raw: Any, default: float = 0.0) -> float:
    try:
        return float(raw)
    except Exception:
        return default


def _extract_times(data: Dict[str, Any], points_raw: List[Dict[str, Any]]) -> tuple[Optional[int], Optional[int], Optional[int]]:
    """Normalize start/end timestamps and derive an anchor time for sorting/filtering."""
    start_ts = _coerce_ts(data.get("start_ts"))
    end_ts = _coerce_ts(data.get("end_ts"))

    if start_ts is None and points_raw:
        start_ts = _coerce_ts(points_raw[0].get("ts"))
    if end_ts is None and points_raw:
        end_ts = _coerce_ts(points_raw[-1].get("ts"))

    if start_ts is not None and end_ts is not None and end_ts < start_ts:
        start_ts, end_ts = end_ts, start_ts

    anchor_ts: Optional[int] = start_ts or end_ts
    if anchor_ts is None:
        for point in points_raw:
            ts = _coerce_ts(point.get("ts"))
            if ts is not None:
                anchor_ts = ts
                break
    return start_ts, end_ts, anchor_ts


def _normalize_points(raw_points: Any, start_ts: Optional[int], end_ts: Optional[int]) -> List[Dict[str, Any]]:
    if isinstance(raw_points, list) and raw_points:
        merged: Dict[int, Dict[str, Any]] = {}
        for point in raw_points:
            ts = _coerce_ts(point.get("ts"))
            if ts is None:
                continue
            entry = merged.setdefault(ts, {"ts": ts, "heat": None, "rank": None})
            heat_val = _coerce_float(point.get("heat"))
            if entry["heat"] is None or heat_val > entry["heat"]:
                entry["heat"] = heat_val
            rank_val = point.get("rank")
            if rank_val is not None:
                entry["rank"] = min(entry["rank"], rank_val) if entry["rank"] is not None else rank_val
        if merged:
            return [merged_ts[1] for merged_ts in sorted(merged.items(), key=lambda item: item[0])]
    # fallback：用起止时间构造一个点，避免前端空列表
    base_ts = end_ts or start_ts
    if base_ts is None:
        return []
    return [{"ts": base_ts, "heat": 0.0, "rank": None}]


def _collapse_points_for_timeline(
    points: List[Dict[str, Any]],
    *,
    start_ts: Optional[int],
    end_ts: Optional[int],
) -> List[Dict[str, Any]]:
    """
    前端 timeline.js 会按 points 每个节点渲染一条 entry。这里收敛为单点，避免同一事件被重复渲染。
    选择最后一个点作为展示锚点，若不存在则用 end_ts/start_ts 兜底。
    """
    if points:
        return [points[-1]]
    anchor = end_ts or start_ts
    if anchor is None:
        return []
    return [{"ts": anchor, "heat": 0.0, "rank": None}]


def _normalize_emotions(raw: Any) -> List[Dict[str, Any]]:
    if isinstance(raw, dict):
        return [{"name": k, "value": _coerce_float(v)} for k, v in raw.items()]
    if isinstance(raw, list):
        items: List[Dict[str, Any]] = []
        for item in raw:
            name = item.get("name")
            if not name:
                continue
            items.append({"name": name, "value": _coerce_float(item.get("value"))})
        return items
    return []


def _normalize_wordcloud(raw: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    items: List[Dict[str, Any]] = []
    for entry in raw:
        text = entry.get("text")
        if not text:
            continue
        items.append({"text": text, "weight": _coerce_float(entry.get("weight"))})
    return items


def _normalize_tag_graph(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        nodes = raw.get("nodes") or []
        edges = raw.get("edges") or []
        return {
            "nodes": nodes if isinstance(nodes, list) else [],
            "edges": edges if isinstance(edges, list) else [],
        }
    return {"nodes": [], "edges": []}

def load_dataset_events(hours: Optional[int] = None) -> List[Dict[str, Any]]:
    dataset_dirs = _list_dataset_dirs()
    if not dataset_dirs:
        return []

    cutoff_ts: Optional[int] = None
    if hours and hours > 0:
        cutoff_ts = int((datetime.now(tz=CHINA_TZ) - timedelta(hours=hours)).timestamp())

    # 事件去重：按“发生时间 + 标题”聚合，较新的目录会覆盖旧目录的同一事件
    dedup: Dict[str, Dict[str, Any]] = {}
    time_title_index: Dict[tuple[int, str], str] = {}

    for dataset_dir in dataset_dirs:
        for path in sorted(dataset_dir.glob("*.json")):
            data = _read_json(path)
            if not data or not data.get("date"):
                continue

            points_raw = data.get("points") or []
            start_ts, end_ts, anchor_ts = _extract_times(data, points_raw)
            if anchor_ts is None:
                continue

            if cutoff_ts is not None and anchor_ts < cutoff_ts:
                continue

            points = _normalize_points(points_raw, start_ts, end_ts)
            original_point_count = len(points)
            heat_peak = data.get("heat_peak")
            if heat_peak is None:
                heat_peak = max((p.get("heat") or 0.0 for p in points), default=0.0)

            event_id = data.get("event_id") or path.stem
            title = data.get("title") or data.get("summary") or path.stem
            time_title_key = (anchor_ts, title)

            previous = time_title_index.get(time_title_key)
            if previous and previous in dedup:
                dedup.pop(previous, None)

            time_title_index[time_title_key] = event_id
            dedup[event_id] = {
                "id": event_id,
                "event_id": event_id,
                "title": title,
                "category": data.get("category") or data.get("health_minor") or "???",
                "health_minor": data.get("health_minor") or "???",
                "sentiment": _coerce_float(data.get("sentiment")),
                "region": data.get("region") or "??",
                "date": data.get("date"),
                "start_ts": start_ts,
                "end_ts": end_ts,
                "heat_peak": _coerce_float(heat_peak),
                "point_count": original_point_count,
                "points": _collapse_points_for_timeline(points, start_ts=start_ts, end_ts=end_ts),
                "summary": data.get("summary") or "",
                "tags": data.get("tags") or [],
                "_anchor_ts": anchor_ts,
            }

    events = list(dedup.values())
    events.sort(key=lambda ev: (ev.get("_anchor_ts") or 0, ev.get("heat_peak") or 0))
    for ev in events:
        ev.pop("_anchor_ts", None)
    return events




def load_dataset_detail(event_id: str) -> Optional[Dict[str, Any]]:
    if not event_id:
        return None
    dataset_dirs = list(reversed(_list_dataset_dirs()))
    if not dataset_dirs:
        return None

    data: Optional[Dict[str, Any]] = None
    for dataset_dir in dataset_dirs:
        target = dataset_dir / f"{event_id}.json"
        if target.exists():
            data = _read_json(target)
            break
        # fallback：按 event_id 字段匹配
        for path in dataset_dir.glob("*.json"):
            candidate = _read_json(path)
            if candidate and candidate.get("event_id") == event_id:
                data = candidate
                break
        if data:
            break

    if not data:
        return None

    points_raw = data.get("points") or []
    start_ts, end_ts, _ = _extract_times(data, points_raw)
    points = _normalize_points(points_raw, start_ts, end_ts)

    emotions = _normalize_emotions(data.get("sentiment_vector"))
    tag_graph = _normalize_tag_graph(data.get("tag_graph"))
    wordcloud = _normalize_wordcloud(data.get("wordcloud"))

    return {
        "event_id": data.get("event_id") or event_id,
        "date": data.get("date"),
        "title": data.get("title") or data.get("summary") or event_id,
        "category": data.get("category") or data.get("health_minor") or "未分类",
        "health_minor": data.get("health_minor") or "未细分",
        "sentiment": _coerce_float(data.get("sentiment")),
        "sentiment_vector": emotions,
        "region": data.get("region") or "未知",
        "start_ts": start_ts,
        "end_ts": end_ts,
        "heat_peak": _coerce_float(data.get("heat_peak")),
        "point_count": len(points),
        "points": points,
        "summary": data.get("summary") or data.get("title") or "",
        "tags": data.get("tags") or [],
        "tag_graph": tag_graph,
        "wordcloud": wordcloud,
        "emotions": emotions,
        "sample_posts": data.get("sample_posts") or data.get("posts") or [],
    }


def summarize_events(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_major: Dict[str, int] = {}
    for ev in events:
        cat = ev.get("category") or "其他"
        by_major[cat] = by_major.get(cat, 0) + 1
    return {
        "total_events": len(events),
        "by_major": by_major,
    }
