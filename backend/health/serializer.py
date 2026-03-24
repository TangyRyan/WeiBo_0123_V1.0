from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.health.models import EventDetail, TimelinePayload
from backend.settings import DATA_ROOT
from backend.storage import read_json, write_json

HEALTH_ROOT = DATA_ROOT / "health"
TIMELINE_DIR = HEALTH_ROOT / "timeline"
EVENT_DIR = HEALTH_ROOT / "events"
ARCHIVE_DIR = HEALTH_ROOT / "archive"
INDEX_PATH = HEALTH_ROOT / "index.json"
LOCK_PATH = HEALTH_ROOT / ".lock"


def ensure_directories() -> None:
    for path in (TIMELINE_DIR, EVENT_DIR, ARCHIVE_DIR):
        path.mkdir(parents=True, exist_ok=True)


def write_timeline(date_str: str, payload: TimelinePayload) -> None:
    ensure_directories()
    payload_dict = payload.to_dict()
    latest_path = TIMELINE_DIR / "latest.json"
    dated_path = TIMELINE_DIR / f"{date_str}.json"
    archive_path = ARCHIVE_DIR / date_str / "timeline.json"
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(latest_path, payload_dict)
    _atomic_write(dated_path, payload_dict)
    _atomic_write(archive_path, payload_dict)
    _update_index(date_str)


def write_event_detail(date_str: str, detail: EventDetail) -> None:
    ensure_directories()
    event_dir = EVENT_DIR / date_str
    event_dir.mkdir(parents=True, exist_ok=True)
    payload = detail.to_dict()
    existing = load_event_detail(detail.event_id, date_str) or load_best_event_detail(detail.event_id)
    if isinstance(existing, dict):
        payload = _merge_event_detail(existing, payload)
    target = event_dir / f"{detail.event_id}.json"
    archive_target = ARCHIVE_DIR / date_str / "events" / f"{detail.event_id}.json"
    archive_target.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(target, payload)
    _atomic_write(archive_target, payload)


def list_available_dates() -> List[str]:
    payload = read_json(INDEX_PATH, default={"dates": []}) or {"dates": []}
    dates = payload.get("dates") or []
    return [date for date in dates if isinstance(date, str)]


def load_timeline(date_str: Optional[str] = None) -> Optional[Dict]:
    ensure_directories()
    if date_str:
        dated_path = TIMELINE_DIR / f"{date_str}.json"
        if dated_path.exists():
            return read_json(dated_path, default=None)
        path = ARCHIVE_DIR / date_str / "timeline.json"
    else:
        path = TIMELINE_DIR / "latest.json"
    return read_json(path, default=None)


def load_event_detail(event_id: str, date_str: Optional[str] = None) -> Optional[Dict]:
    ensure_directories()
    if not date_str and event_id and len(event_id) >= 10 and event_id[4] == "-":
        date_str = event_id[:10]
    if not date_str:
        return None
    path = EVENT_DIR / date_str / f"{event_id}.json"
    if not path.exists():
        path = ARCHIVE_DIR / date_str / "events" / f"{event_id}.json"
    return read_json(path, default=None)


def load_best_event_detail(event_id: str) -> Optional[Dict]:
    """Load the richest known event detail across all date partitions."""
    ensure_directories()

    base = load_event_detail(event_id)
    best = base if isinstance(base, dict) else None
    best_score = _detail_score(best) if isinstance(best, dict) else -1
    ranked_paths: List[tuple[str, Path]] = []

    for path in EVENT_DIR.glob(f"*/{event_id}.json"):
        ranked_paths.append((path.parent.name, path))
    for path in ARCHIVE_DIR.glob(f"*/events/{event_id}.json"):
        ranked_paths.append((path.parent.parent.name, path))

    seen: set[str] = set()
    for _, path in sorted(ranked_paths, key=lambda item: item[0], reverse=True):
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        payload = read_json(path, default=None)
        if not isinstance(payload, dict):
            continue
        score = _detail_score(payload)
        if score > best_score:
            best = payload
            best_score = score
            if best_score >= 3:
                return best
    return best


@contextmanager
def acquire_lock(timeout: float = 10.0):
    """Simple file lock to guard scheduler writes."""

    start = time.time()
    while True:
        try:
            fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            break
        except FileExistsError:
            if (time.time() - start) >= timeout:
                raise TimeoutError("health serializer lock timed out")
            time.sleep(0.2)
    try:
        yield
    finally:
        try:
            os.remove(str(LOCK_PATH))
        except FileNotFoundError:
            pass


def _update_index(date_str: str) -> None:
    payload = read_json(INDEX_PATH, default={"dates": []}) or {"dates": []}
    dates = payload.get("dates") or []
    if date_str not in dates:
        dates.append(date_str)
    dates.sort(reverse=True)
    write_json(INDEX_PATH, {"dates": dates})


def _has_items(value: Any) -> bool:
    return isinstance(value, list) and bool(value)


def _detail_score(payload: Optional[Dict[str, Any]]) -> int:
    if not isinstance(payload, dict):
        return -1
    score = 0
    if _has_items(payload.get("sample_posts")):
        score += 1
    if _has_items(payload.get("wordcloud")):
        score += 1
    if _has_items(payload.get("tags")):
        score += 1
    return score


def _is_empty_tag_graph(value: Any) -> bool:
    if not isinstance(value, dict):
        return True
    nodes = value.get("nodes")
    edges = value.get("edges")
    has_nodes = isinstance(nodes, list) and bool(nodes)
    has_edges = isinstance(edges, list) and bool(edges)
    return not (has_nodes or has_edges)


def _merge_event_detail(existing: Dict[str, Any], fresh: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(fresh)
    for field in ("sample_posts", "wordcloud", "tags"):
        if _has_items(existing.get(field)) and not _has_items(merged.get(field)):
            merged[field] = existing.get(field)

    if _is_empty_tag_graph(merged.get("tag_graph")) and not _is_empty_tag_graph(existing.get("tag_graph")):
        merged["tag_graph"] = existing.get("tag_graph")

    if not str(merged.get("summary") or "").strip() and str(existing.get("summary") or "").strip():
        merged["summary"] = existing.get("summary")

    return merged


def _atomic_write(target: Path, data: Dict) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(target.suffix + ".tmp")
    write_json(temp, data)
    temp.replace(target)
