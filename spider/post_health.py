from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, Optional, Set

from backend.config import POST_DIR
from backend.storage import read_json, write_json
from spider.config import get_env_int, get_env_list
from spider.cookie_manager import refresh_cookie_async, refresh_cookie_sync
from spider.crawler_core import CHINA_TZ

LOGGER = logging.getLogger(__name__)

_DEFAULT_CHECK_HOURS = (6, 12, 18)
_DEFAULT_THRESHOLD = 10
_DEFAULT_COOLDOWN_SECONDS = 3600
_STATE_PATH = POST_DIR / "hourly_empty_alert_state.json"
_LOGIN_NOTIFY_LABEL = "hourly_posts_empty_login_refresh"
_DEFAULT_DAILY_THRESHOLD = 30
_DEFAULT_DAILY_COOLDOWN_SECONDS = 3600
_DAILY_STATE_PATH = POST_DIR / "daily_empty_alert_state.json"
_DAILY_LOGIN_NOTIFY_LABEL = "daily_posts_empty_login_refresh"


def _parse_check_hours() -> Set[int]:
    raw_hours = get_env_list(
        "WEIBO_POST_EMPTY_CHECK_HOURS",
        [str(h) for h in _DEFAULT_CHECK_HOURS],
    )
    parsed: Set[int] = set()
    for item in raw_hours:
        try:
            hour = int(item)
        except (TypeError, ValueError):
            continue
        if 0 <= hour <= 23:
            parsed.add(hour)
    if not parsed:
        parsed.update(_DEFAULT_CHECK_HOURS)
    return parsed


def _load_state() -> Dict:
    return read_json(_STATE_PATH, default={}) or {}


def _save_state(state: Dict) -> None:
    write_json(_STATE_PATH, state)


def _state_key(date_str: str, hour: int) -> str:
    return f"{date_str}:{hour:02d}"


def _safe_read_json(path: Path) -> Optional[Dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        LOGGER.warning("Failed to read hourly post payload %s: %s", path, exc)
        return None


def _count_empty_items(files: Iterable[Path]) -> tuple[int, int]:
    empty_count = 0
    total = 0
    for file_path in files:
        total += 1
        payload = _safe_read_json(file_path)
        if payload is None:
            continue
        items = payload.get("items")
        if not isinstance(items, list) or not items:
            empty_count += 1
    return empty_count, total


def _trigger_login(reason: str, notify_label: str, *, async_mode: bool, logger: logging.Logger) -> bool:
    if async_mode:
        return refresh_cookie_async(reason, notify_label=notify_label, logger_=logger)
    return bool(refresh_cookie_sync(reason, notify_label=notify_label, logger_=logger))


def check_hourly_posts_empty(
    date_str: str,
    hour: int,
    *,
    trigger: bool = True,
    async_mode: bool = True,
    logger: Optional[logging.Logger] = None,
) -> Dict[str, object]:
    """Check hourly posts payloads and optionally trigger login/email."""
    logger = logger or LOGGER
    check_hours = _parse_check_hours()
    threshold = get_env_int("WEIBO_POST_EMPTY_THRESHOLD", _DEFAULT_THRESHOLD)
    if threshold is None:
        threshold = _DEFAULT_THRESHOLD
    cooldown = get_env_int("WEIBO_POST_EMPTY_COOLDOWN_SECONDS", _DEFAULT_COOLDOWN_SECONDS)
    if cooldown is None:
        cooldown = _DEFAULT_COOLDOWN_SECONDS

    if threshold < 1:
        return {"status": "disabled", "threshold": threshold, "hour": hour}
    if hour not in check_hours:
        return {"status": "skip_hour", "hour": hour, "check_hours": sorted(check_hours)}

    date_dir = POST_DIR / date_str
    if not date_dir.exists():
        return {"status": "missing_dir", "path": str(date_dir)}

    files = sorted(date_dir.glob("*.json"))
    if not files:
        return {"status": "empty_dir", "path": str(date_dir)}

    empty_count, total_files = _count_empty_items(files)
    result = {
        "status": "ok",
        "date": date_str,
        "hour": hour,
        "empty": empty_count,
        "total": total_files,
        "threshold": threshold,
        "path": str(date_dir),
    }

    if empty_count < threshold or not trigger:
        if empty_count >= threshold and not trigger:
            result["status"] = "threshold_reached"
        return result

    state = _load_state()
    key = _state_key(date_str, hour)
    now_ts = time.time()
    last_ts = (state.get("last_trigger") or {}).get(key, 0)
    if last_ts and cooldown > 0 and (now_ts - float(last_ts)) < cooldown:
        result["status"] = "cooldown"
        result["cooldown_seconds"] = cooldown
        result["last_trigger_ts"] = last_ts
        return result

    reason = (
        f"hourly_posts_empty date={date_str} hour={hour:02d} "
        f"empty={empty_count} total={total_files} threshold={threshold}"
    )
    logger.warning("Hourly posts empty threshold reached: %s", reason)

    started = _trigger_login(reason, _LOGIN_NOTIFY_LABEL, async_mode=async_mode, logger=logger)

    state.setdefault("last_trigger", {})[key] = now_ts
    state["last_trigger_at"] = datetime.now(tz=CHINA_TZ).isoformat(timespec="seconds")
    _save_state(state)

    result["status"] = "triggered" if started else "trigger_failed"
    result["reason"] = reason
    return result


def _load_daily_state() -> Dict:
    return read_json(_DAILY_STATE_PATH, default={}) or {}


def _save_daily_state(state: Dict) -> None:
    write_json(_DAILY_STATE_PATH, state)


def check_daily_posts_empty(
    date_str: str,
    *,
    trigger: bool = True,
    async_mode: bool = True,
    logger: Optional[logging.Logger] = None,
) -> Dict[str, object]:
    """Check daily posts payloads and optionally trigger login/email."""
    logger = logger or LOGGER
    threshold = get_env_int("WEIBO_DAILY_POST_EMPTY_THRESHOLD", _DEFAULT_DAILY_THRESHOLD)
    if threshold is None:
        threshold = _DEFAULT_DAILY_THRESHOLD
    cooldown = get_env_int("WEIBO_DAILY_POST_EMPTY_COOLDOWN_SECONDS", _DEFAULT_DAILY_COOLDOWN_SECONDS)
    if cooldown is None:
        cooldown = _DEFAULT_DAILY_COOLDOWN_SECONDS

    if threshold < 1:
        return {"status": "disabled", "threshold": threshold}

    date_dir = POST_DIR / date_str
    if not date_dir.exists():
        return {"status": "missing_dir", "path": str(date_dir)}

    files = sorted(date_dir.glob("*.json"))
    if not files:
        return {"status": "empty_dir", "path": str(date_dir)}

    empty_count, total_files = _count_empty_items(files)
    result = {
        "status": "ok",
        "date": date_str,
        "empty": empty_count,
        "total": total_files,
        "threshold": threshold,
        "path": str(date_dir),
    }

    if empty_count <= threshold or not trigger:
        if empty_count > threshold and not trigger:
            result["status"] = "threshold_reached"
        return result

    state = _load_daily_state()
    now_ts = time.time()
    last_ts = (state.get("last_trigger") or {}).get(date_str, 0)
    if last_ts and cooldown > 0 and (now_ts - float(last_ts)) < cooldown:
        result["status"] = "cooldown"
        result["cooldown_seconds"] = cooldown
        result["last_trigger_ts"] = last_ts
        return result

    reason = (
        f"daily_posts_empty date={date_str} "
        f"empty={empty_count} total={total_files} threshold={threshold}"
    )
    logger.warning("Daily posts empty threshold reached: %s", reason)

    started = _trigger_login(reason, _DAILY_LOGIN_NOTIFY_LABEL, async_mode=async_mode, logger=logger)

    state.setdefault("last_trigger", {})[date_str] = now_ts
    state["last_trigger_at"] = datetime.now(tz=CHINA_TZ).isoformat(timespec="seconds")
    _save_daily_state(state)

    result["status"] = "triggered" if started else "trigger_failed"
    result["reason"] = reason
    return result
