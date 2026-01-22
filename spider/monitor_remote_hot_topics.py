"""Poll GitHub hourly trending snapshots, update archives, and refresh posts."""

import asyncio
import json
import logging
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

CURRENT_DIR = Path(__file__).resolve().parent
ROOT_DIR = CURRENT_DIR.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

from spider.config import get_env_bool, get_env_int, get_env_str
from spider.fetch_hot_topics import (
    CHINA_TZ,
    ensure_dirs,
    fetch_hour_topics,
    upsert_topic,
)
from spider.local_hot_topics import fetch_latest_topics_local
from spider.crawler_core import slugify_title
from spider.aicard_service import ensure_aicard_snapshot
from spider.aicard_client import AICardCooldownError, AICardRateLimitError
from spider.update_posts import (
    MAX_TOPICS_PER_RUN,
    ensure_topic_posts,
    refresh_posts_for_date,
)
from backend.config import HOURLY_DIR, POST_DIR
from backend.storage import (
    load_daily_archive,
    save_daily_archive,
    from_data_relative,
    write_json,
)


def _resolve_path(value: Optional[str], default: Path) -> Path:
    """Resolve paths from env, allowing relative inputs."""
    if not value:
        return default
    return from_data_relative(value)


POLL_INTERVAL_SECONDS = get_env_int("WEIBO_MONITOR_POLL_INTERVAL", 600) or 600
RECENT_RETRY_SECONDS = get_env_int("WEIBO_MONITOR_RECENT_RETRY", 60) or 60
MAX_LOOKBACK_DAYS = get_env_int("WEIBO_MONITOR_MAX_LOOKBACK_DAYS", 1) or 1
# Guard to force remote-only mode: when true, skip all local crawler fallbacks.
REMOTE_ONLY = get_env_bool("WEIBO_MONITOR_REMOTE_ONLY", False)
HOURLY_ARCHIVE_DIR = _resolve_path(
    get_env_str("WEIBO_MONITOR_HOURLY_DIR"),
    HOURLY_DIR,
)
CURRENT_ONLY = get_env_bool("WEIBO_MONITOR_CURRENT_ONLY", False)
_LOG_LEVEL_NAME = (get_env_str("WEIBO_MONITOR_LOG_LEVEL", "INFO") or "INFO").upper()
LOG_LEVEL = getattr(logging, _LOG_LEVEL_NAME, logging.INFO)
LOCAL_FALLBACK_THRESHOLD_MINUTES = (
    get_env_int("WEIBO_MONITOR_LOCAL_FALLBACK_MINUTES", 45) or 45
)
_HOT_TOPICS_LIMIT = get_env_int("WEIBO_HOT_TOPICS_LIMIT", 50) or 50
HOT_TOPICS_LIMIT = _HOT_TOPICS_LIMIT if _HOT_TOPICS_LIMIT > 0 else 50
HOURLY_POST_LIMIT = get_env_int("WEIBO_MONITOR_HOURLY_POST_LIMIT", 50) or 50
RATE_LIMIT_SLEEP_SECONDS = get_env_int("WEIBO_MONITOR_AICARD_SLEEP", 300) or 300
POST_REFRESH_INTERVAL_SECONDS = get_env_int("WEIBO_POST_REFRESH_INTERVAL_SECONDS", 14400) or 14400
POST_REFRESH_META_PATH = POST_DIR / "post_refresh_meta.json"


def ensure_hourly_dir() -> None:
    HOURLY_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)


def hour_path(date_str: str, hour: int) -> Path:
    return HOURLY_ARCHIVE_DIR / date_str / f"{hour:02d}.json"


def collect_pending_hours(now: datetime) -> List[Tuple[str, int]]:
    if CURRENT_ONLY:
        date_str = now.strftime("%Y-%m-%d")
        hour = now.hour
        if hour_path(date_str, hour).exists():
            return []
        return [(date_str, hour)]
    pending: List[Tuple[str, int]] = []
    for offset in range(MAX_LOOKBACK_DAYS + 1):
        target = now - timedelta(days=offset)
        date_str = target.strftime("%Y-%m-%d")
        max_hour = target.hour if offset == 0 else 23
        for hour in range(max_hour + 1):
            if not hour_path(date_str, hour).exists():
                pending.append((date_str, hour))
    return pending


def update_hourly_archive(date_str: str, hour: int, topics: List[dict]) -> None:
    target_dir = HOURLY_ARCHIVE_DIR / date_str
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{hour:02d}.json"
    write_json(path, topics)
    logging.info("Saved hourly snapshot %s", path)


def update_daily_archive(date_str: str, hour: int, topics: List[dict]) -> None:
    daily_data = load_daily_archive(date_str)
    new_titles = 0
    for topic in topics:
        title = (topic.get("title") or "").strip()
        if title and title not in daily_data:
            new_titles += 1
        record = upsert_topic(daily_data, topic, date_str, hour)
        if record:
            snapshot = ensure_aicard_snapshot(
                title,
                date_str,
                hour,
                slug=record.get("slug"),
                logger=logging.getLogger("aicard"),
            )
            if snapshot:
                aicard_field = record.setdefault("aicard", {})
                hours = aicard_field.setdefault("hours", {})
                hour_key = f"{hour:02d}"
                hours[hour_key] = snapshot
                aicard_field["latest"] = snapshot
                if snapshot.get("markdown_path"):
                    aicard_field["markdown"] = snapshot.get("markdown_path")
    save_daily_archive(date_str, daily_data)
    pending_refresh = sum(1 for item in daily_data.values() if item.get("needs_refresh"))
    logging.info(
        "Daily archive %s %02d synced: total=%s, new=%s, pending_refresh=%s",
        date_str,
        hour,
        len(daily_data),
        new_titles,
        pending_refresh,
    )


def process_hour(date_str: str, hour: int) -> bool:
    topics, local_used = fetch_topics_with_fallback(date_str, hour)
    if not topics:
        logging.warning("No topics available for %s %02d after fallback", date_str, hour)
        return False

    if HOT_TOPICS_LIMIT and len(topics) > HOT_TOPICS_LIMIT:
        logging.info(
            "Trimming topics for %s %02d: %s -> %s",
            date_str,
            hour,
            len(topics),
            HOT_TOPICS_LIMIT,
        )
        topics = topics[:HOT_TOPICS_LIMIT]

    if local_used:
        logging.info("Using local crawler data for %s %02d", date_str, hour)
    update_hourly_archive(date_str, hour, topics)
    try:
        update_daily_archive(date_str, hour, topics)
    except AICardCooldownError as exc:
        wait_seconds = max(exc.retry_after, 1)
        logging.warning(
            "AI Card cooldown (%s) for %s %02d; sleeping %s seconds before retrying",
            exc.level,
            date_str,
            hour,
            wait_seconds,
        )
        time.sleep(wait_seconds)
        return False
    except AICardRateLimitError as exc:
        logging.warning(
            "AI Card service returned HTTP 418 for %s %02d (%s); sleeping %s seconds before retrying",
            date_str,
            hour,
            exc,
            RATE_LIMIT_SLEEP_SECONDS,
        )
        time.sleep(RATE_LIMIT_SLEEP_SECONDS)
        return False
    _refresh_posts_if_needed(date_str)
    _collect_hourly_posts(date_str, hour, topics)
    logging.debug("Completed processing for %s %02d (local_used=%s)", date_str, hour, local_used)
    return True


def process_latest_hour(force: bool = False) -> bool:
    now = datetime.now(tz=CHINA_TZ)
    date_str = now.strftime("%Y-%m-%d")
    hour = now.hour
    if not force and hour_path(date_str, hour).exists():
        logging.debug("Current hour %s %02d already exists, skip immediate fetch", date_str, hour)
        return False
    logging.info("Immediate fetch for %s %02d", date_str, hour)
    return process_hour(date_str, hour)


def process_pending_hours() -> bool:
    now = datetime.now(tz=CHINA_TZ)
    pending = collect_pending_hours(now)
    if not pending:
        logging.debug("No pending hourly snapshots")
        return False

    processed_any = False
    for date_str, hour in pending:
        if process_hour(date_str, hour):
            processed_any = True
    return processed_any


def fetch_topics_with_fallback(date_str: str, hour: int) -> Tuple[List[dict], bool]:
    try:
        topics = fetch_hour_topics(date_str, hour)
    except requests.HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else None
        if status_code == 404:
            logging.info(
                "Remote %s %02d data not ready (HTTP 404), attempting local fallback",
                date_str,
                hour,
            )
        else:
            logging.error(
                "HTTP %s when fetching %s %02d data; attempting local fallback",
                status_code,
                date_str,
                hour,
            )
        if REMOTE_ONLY:
            logging.warning(
                "Remote-only mode: skip local fallback for %s %02d (status=%s)",
                date_str,
                hour,
                status_code,
            )
            return [], False
        return _maybe_fetch_local(date_str, hour, "remote fetch error")
    except (requests.RequestException, ValueError) as exc:
        logging.error(
            "Remote fetch failed for %s %02d (%s), attempting local fallback",
            date_str,
            hour,
            exc,
        )
        if REMOTE_ONLY:
            logging.warning(
                "Remote-only mode: skip local fallback for %s %02d after failure",
                date_str,
                hour,
            )
            return [], False
        return _maybe_fetch_local(date_str, hour, "remote failure")

    if topics:
        return topics, False

    logging.warning("Remote returned empty list for %s %02d, attempting local fallback", date_str, hour)
    if REMOTE_ONLY:
        logging.warning(
            "Remote-only mode: skip local fallback for %s %02d because payload empty",
            date_str,
            hour,
        )
        return [], False
    return _maybe_fetch_local(date_str, hour, "remote empty payload")


def fetch_local_topics_with_logging(date_str: str, hour: int, reason: str) -> List[dict]:
    logging.info("Triggering local crawler for %s %02d (%s)", date_str, hour, reason)
    try:
        topics = fetch_latest_topics_local()
    except Exception as exc:  # pylint: disable=broad-except
        logging.error("Local crawler failed for %s %02d: %s", date_str, hour, exc, exc_info=True)
        return []

    if not topics:
        logging.warning("Local crawler returned empty topics for %s %02d", date_str, hour)
        return []

    logging.info("Local crawler produced %s topics for %s %02d", len(topics), date_str, hour)
    return topics


def should_trigger_local(date_str: str, hour: int) -> bool:
    target = datetime.fromisoformat(f"{date_str}T{hour:02d}:00:00").replace(tzinfo=CHINA_TZ)
    now = datetime.now(tz=CHINA_TZ)
    if now < target:
        return False
    elapsed = now - target
    if elapsed >= timedelta(minutes=LOCAL_FALLBACK_THRESHOLD_MINUTES):
        return True
    if now.date() > target.date():
        return True
    if now.hour > hour:
        return True
    return False





def _maybe_fetch_local(date_str: str, hour: int, reason: str) -> Tuple[List[dict], bool]:
    if REMOTE_ONLY:
        logging.info(
            "Remote-only mode: local crawler disabled for %s %02d (%s)",
            date_str,
            hour,
            reason,
        )
        return [], False
    if not should_trigger_local(date_str, hour):
        logging.info(
            "Local fallback for %s %02d postponed: only %s minutes since top of hour",
            date_str,
            hour,
            LOCAL_FALLBACK_THRESHOLD_MINUTES,
        )
        return [], False
    topics = fetch_local_topics_with_logging(date_str, hour, reason)
    return topics, bool(topics)


def _refresh_posts_if_needed(date_str: str) -> None:
    now = datetime.now(tz=CHINA_TZ)
    if POST_REFRESH_INTERVAL_SECONDS > 0:
        meta = {}
        if POST_REFRESH_META_PATH.exists():
            try:
                meta = json.loads(POST_REFRESH_META_PATH.read_text(encoding="utf-8"))
            except Exception:
                meta = {}
        last_refresh_raw = meta.get("last_refresh_at")
        if isinstance(last_refresh_raw, str) and last_refresh_raw:
            try:
                last_refresh = datetime.fromisoformat(last_refresh_raw)
                if last_refresh.tzinfo is None:
                    last_refresh = last_refresh.replace(tzinfo=CHINA_TZ)
            except ValueError:
                last_refresh = None
            if last_refresh:
                elapsed = (now - last_refresh).total_seconds()
                if elapsed < POST_REFRESH_INTERVAL_SECONDS:
                    logging.info(
                        "Post refresh skipped %s: last run %ss ago (<%ss)",
                        date_str,
                        int(elapsed),
                        POST_REFRESH_INTERVAL_SECONDS,
                    )
                    return
    try:
        result = refresh_posts_for_date(date_str, MAX_TOPICS_PER_RUN)
    except FileNotFoundError:
        logging.warning("Daily archive %s missing; skip post refresh", date_str)
        return
    except Exception as exc:  # pylint: disable=broad-except
        logging.error("Post refresh failed for %s: %s", date_str, exc)
        write_json(
            POST_REFRESH_META_PATH,
            {
                "last_refresh_at": now.isoformat(timespec="seconds"),
                "last_refresh_date": date_str,
                "status": "failed",
                "error": str(exc),
            },
        )
        return

    refreshed = len(result.get("refreshed", []))
    skipped = len(result.get("skipped", []))
    failed = len(result.get("failed", []))
    write_json(
        POST_REFRESH_META_PATH,
        {
            "last_refresh_at": now.isoformat(timespec="seconds"),
            "last_refresh_date": date_str,
            "status": "ok",
            "refreshed": refreshed,
            "skipped": skipped,
            "failed": failed,
        },
    )
    logging.info(
        "Post refresh summary %s: refreshed=%s skipped=%s failed=%s",
        date_str,
        refreshed,
        skipped,
        failed,
    )


def _collect_hourly_posts(date_str: str, hour: int, topics: List[dict]) -> Dict[str, Dict[str, Any]]:
    daily_data = load_daily_archive(date_str)
    payload_map: Dict[str, Dict[str, Any]] = {}
    changed = False
    for index, topic in enumerate(topics):
        if index >= HOURLY_POST_LIMIT:
            break
        title = (topic.get("title") or "").strip()
        if not title:
            continue
        record = daily_data.get(title)
        if not record:
            logging.debug("Skip hourly post collection for %s: missing archive record", title)
            continue
        updated_record = ensure_topic_posts(title, record, date_str)
        daily_data[title] = updated_record
        payload = updated_record.get("latest_posts") or {}
        payload_map[title] = payload
        changed = True
    if changed:
        save_daily_archive(date_str, daily_data)
    return payload_map


async def run_loop() -> None:
    ensure_dirs()
    ensure_hourly_dir()
    await asyncio.to_thread(process_latest_hour, True)
    while True:
        processed = await asyncio.to_thread(process_pending_hours)
        sleep_for = RECENT_RETRY_SECONDS if processed else POLL_INTERVAL_SECONDS
        logging.info("Sleeping %s seconds before next check", sleep_for)
        await asyncio.sleep(sleep_for)


def configure_logging() -> None:
    logging.basicConfig(
        level=LOG_LEVEL,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )


def main() -> None:
    configure_logging()
    try:
        asyncio.run(run_loop())
    except KeyboardInterrupt:
        logging.info("Received stop signal, exiting monitor")


if __name__ == "__main__":
    main()
