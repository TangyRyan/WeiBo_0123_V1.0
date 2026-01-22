from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, List, Optional

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


def _atomic_write(target: Path, data: Dict) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(target.suffix + ".tmp")
    write_json(temp, data)
    temp.replace(target)
