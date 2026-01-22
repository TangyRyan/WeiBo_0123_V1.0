from __future__ import annotations

import json
import logging
import re
import shutil
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from backend.config import (
    AICARD_DIR,
    ARCHIVE_DIR,
    DAILY_LLM_TIME,
    DELETE_RETENTION_DAYS,
    HEALTH_TOPIC_ENABLED,
    HEALTH_TOPIC_INTERVAL_MINUTES,
    HEALTH_TOPIC_WINDOW_HOURS,
    HOURLY_CLEAN_TIME,
    HOURLY_DIR,
    HOURLY_RETENTION_DAYS,
    HOUR_CHECK_INTERVAL_MINUTES,
    LIGHT_RETENTION_DAYS,
    LLM_ANALYSIS_TOP_K,
    LLM_ANALYSIS_WORKERS,
    LLM_ENABLED,
    MONITOR_ENABLED,
    MONITOR_CRON,
    POST_DIR,
    SLIM_RETENTION_DAYS,
    SLIM_TIME,
)
from backend.central_cache import update_central_cache_for_date
from backend.llm.analysis import call_openai
from backend.health import refresh_health_snapshot
from backend.risk_model import (
    calc_crowd,
    calc_growth,
    calc_negativity,
    calc_sensitivity,
    aggregate_score,
    risk_level_from_score,
    risk_level_label,
    risk_tier_segments,
)
from backend.storage import (
    load_daily_archive,
    load_post_snapshot,
    from_data_relative,
    save_daily_archive,
    save_hour_hotlist,
    save_risk_warnings,
    save_risk_archive,
)
from spider.crawler_core import CHINA_TZ, slugify_title
from spider.monitor_remote_hot_topics import collect_pending_hours, hour_path, process_hour

logger = logging.getLogger(__name__)

HOTLIST_PUSH = None
RISK_PUSH = None
_SCHEDULER: Optional[BackgroundScheduler] = None
ARCHIVE_LOCK = threading.Lock()
_DATE_TOKEN_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_HASHTAG_PATTERN = re.compile(r"#([^#]+)#")


def _parse_monitor_cron(cron_expr: str) -> Optional[CronTrigger]:
    if not cron_expr:
        return None
    parts = cron_expr.split()
    try:
        if len(parts) == 5:
            return CronTrigger.from_crontab(cron_expr, timezone="Asia/Shanghai")
        if len(parts) == 6:
            return CronTrigger(
                second=parts[0],
                minute=parts[1],
                hour=parts[2],
                day=parts[3],
                month=parts[4],
                day_of_week=parts[5],
                timezone="Asia/Shanghai",
            )
    except Exception:
        logger.exception("Invalid WEIBO_MONITOR_CRON=%s; fallback to interval", cron_expr)
        return None
    logger.error("Invalid WEIBO_MONITOR_CRON format: %s", cron_expr)
    return None


def _parse_date_token(token: str) -> Optional[date]:
    if not _DATE_TOKEN_PATTERN.match(token):
        return None
    try:
        return datetime.strptime(token, "%Y-%m-%d").date()
    except ValueError:
        return None


def set_push_callbacks(hotlist_push_cb, risk_push_cb) -> None:
    global HOTLIST_PUSH, RISK_PUSH
    HOTLIST_PUSH = hotlist_push_cb
    RISK_PUSH = risk_push_cb
    logger.debug("Push callbacks wired: hotlist=%s risk=%s", bool(HOTLIST_PUSH), bool(RISK_PUSH))


def _load_hourly_topics(date_str: str, hour: int) -> Optional[List[Dict[str, Any]]]:
    path = hour_path(date_str, hour)
    try:
        text = path.read_text(encoding="utf-8")
        return json.loads(text)
    except FileNotFoundError:
        return None
    except json.JSONDecodeError:
        return None


def _broadcast_latest_hotlist(date_str: str, hour: str, topics: List[Dict[str, Any]]) -> None:
    payload = {"date": date_str, "hour": hour, "items": topics}
    if HOTLIST_PUSH:
        HOTLIST_PUSH(payload)


def _sync_hotlist_snapshot(date_str: str, hour: int) -> None:
    topics = _load_hourly_topics(date_str, hour)
    if topics is None:
        logger.debug("No hourly topics to sync for %s %02d", date_str, hour)
        return
    hour_str = f"{hour:02d}"
    save_hour_hotlist(date_str, hour_str, topics)
    _broadcast_latest_hotlist(date_str, hour_str, topics)
    logger.info("Synced hotlist snapshot %s %s with %s topics", date_str, hour_str, len(topics))


def _monitor_tick() -> bool:
    now = datetime.now(tz=CHINA_TZ)
    pending = collect_pending_hours(now)
    if not pending:
        logger.debug("Monitor tick %s: no pending hours", now.isoformat())
        return False
    logger.info("Monitor tick %s: processing %s pending hours", now.isoformat(), len(pending))
    processed_any = False
    for date_str, hour in pending:
        if process_hour(date_str, hour):
            logger.info("Processed remote topics for %s %02d", date_str, hour)
            _sync_hotlist_snapshot(date_str, hour)
            processed_any = True
        else:
            logger.warning("Processing remote topics failed for %s %02d", date_str, hour)
    return processed_any


def _monitor_session() -> None:
    """Run a short-lived monitor loop after cron triggers (stop once hour changes or snapshot exists)."""
    start = datetime.now(tz=CHINA_TZ)
    start_date = start.strftime("%Y-%m-%d")
    start_hour = start.hour
    target_path = hour_path(start_date, start_hour)
    logger.info(
        "Monitor session started at %s for %s %02d (interval=%sm)",
        start.isoformat(timespec="seconds"),
        start_date,
        start_hour,
        HOUR_CHECK_INTERVAL_MINUTES,
    )
    while True:
        now = datetime.now(tz=CHINA_TZ)
        if now.date() != start.date() or now.hour != start_hour:
            logger.info(
                "Monitor session stopped: hour changed (%s -> %s)",
                start.isoformat(timespec="minutes"),
                now.isoformat(timespec="minutes"),
            )
            break
        if target_path.exists():
            logger.info("Monitor session completed: snapshot ready for %s %02d", start_date, start_hour)
            break
        processed = _monitor_tick()
        if processed and target_path.exists():
            logger.info("Monitor session completed: snapshot written for %s %02d", start_date, start_hour)
            break
        sleep_seconds = max(30, int(HOUR_CHECK_INTERVAL_MINUTES * 60))
        logger.info("Monitor session sleeping %ss before next attempt", sleep_seconds)
        time.sleep(sleep_seconds)


def _update_risk_snapshots(date_str: str, *, push: bool) -> Dict[str, Any]:
    """Recompute latest risk warnings, persist to disk, and optionally push via websocket."""
    warnings = top_risk_warnings()
    save_risk_warnings(warnings)
    save_risk_archive(date_str, warnings)
    if push and RISK_PUSH:
        RISK_PUSH(warnings)
    return warnings


def _health_topic_job() -> None:
    try:
        refresh_health_snapshot(hours=HEALTH_TOPIC_WINDOW_HOURS)
        logger.info(
            "Health topic snapshot refreshed (window=%sh)",
            HEALTH_TOPIC_WINDOW_HOURS,
        )
    except Exception:
        logger.exception("Health topic job failed")


def _coerce_media(item: Dict[str, Any]) -> List[str]:
    media: List[str] = []
    pics = item.get("pics") or item.get("image_links") or []
    if isinstance(pics, list):
        for pic in pics:
            if isinstance(pic, dict):
                url = pic.get("url") or pic.get("large", {}).get("url")
                if url:
                    media.append(url)
            elif isinstance(pic, str):
                media.append(pic)
    video = item.get("video")
    if isinstance(video, dict):
        streams = video.get("streams")
        if isinstance(streams, dict):
            for url in streams.values():
                if url:
                    media.append(url)
                    break
        url = video.get("url")
        if url:
            media.append(url)
    return media


def _normalize_posts(payload: Optional[Dict[str, Any]], fallback_slug: str) -> List[Dict[str, Any]]:
    if not payload:
        return []
    items = payload.get("items") or []
    normalized: List[Dict[str, Any]] = []
    for idx, item in enumerate(items):
        post_id = item.get("id") or item.get("post_id") or f"{fallback_slug}_{idx}"
        normalized.append({
            "post_id": post_id,
            "published_at": item.get("created_at") or item.get("timestamp") or item.get("published_at"),
            "account_name": item.get("user_name") or item.get("author") or item.get("account_name") or "未知用户",
            "content_text": (
                item.get("content_text")
                or item.get("text")
                or item.get("content")
                or item.get("text_raw")
                or ""
            ),
            "media": _coerce_media(item),
            "reposts": int(item.get("reposts") or item.get("forwards_count") or 0),
            "comments": int(item.get("comments") or item.get("comments_count") or 0),
            "likes": int(item.get("likes") or item.get("likes_count") or 0),
        })
    return normalized


def _read_post_payload_from_path(raw_path: str) -> Optional[Dict[str, Any]]:
    try:
        path = from_data_relative(raw_path)
    except Exception:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _load_posts_for_event(date_str: str, event: Dict[str, Any]) -> List[Dict[str, Any]]:
    slug = event.get("slug") or slugify_title(event.get("name") or "")
    payload = None
    latest = event.get("latest_posts")
    if isinstance(latest, dict):
        if latest.get("items"):
            payload = latest
        else:
            snapshot = latest.get("snapshot") or latest.get("post_output")
            if snapshot:
                payload = _read_post_payload_from_path(snapshot)
    if payload is None:
        post_path = event.get("post_output")
        if post_path:
            payload = _read_post_payload_from_path(post_path)
    if payload is None and slug:
        payload = load_post_snapshot(date_str, slug)
    return _normalize_posts(payload, slug or "event")


def _canonical_topic_name(raw_name: str, event: Dict[str, Any]) -> str:
    title = event.get("title") or event.get("name")
    candidate = title if isinstance(title, str) and title.strip() else raw_name
    text = str(candidate or "")
    return text.strip() or str(raw_name)


def _coerce_hot_number(raw: Any) -> float:
    if raw is None:
        return 0.0
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        text = raw.strip().lower().replace(",", "")
        if not text:
            return 0.0
        multiplier = 1.0
        if text.endswith("万") or text.endswith("w"):
            text = text[:-1]
            multiplier = 10000.0
        try:
            return float(text) * multiplier
        except ValueError:
            return 0.0
    return 0.0


def _extract_hot_score(event: Dict[str, Any]) -> float:
    hot_values = event.get("hot_values")
    if isinstance(hot_values, dict) and hot_values:
        latest_stamp = None
        latest_hot = 0.0
        for key, value in hot_values.items():
            stamp = str(key)
            if latest_stamp is None or stamp > latest_stamp:
                latest_stamp = stamp
                latest_hot = _coerce_hot_number(value)
        if latest_stamp is not None:
            return latest_hot
    for key in ("hot", "heat", "score"):
        if key in event:
            hot = _coerce_hot_number(event.get(key))
            if hot:
                return hot
    return 0.0


def _is_llm_succeeded(event: Dict[str, Any]) -> bool:
    llm_status = event.get("llm_status")
    if not isinstance(llm_status, dict):
        return False
    if llm_status.get("state") != "succeeded":
        return False
    return bool(event.get("llm"))


def _is_health_topic(event: Dict[str, Any]) -> bool:
    llm_block = event.get("llm") or {}
    if isinstance(llm_block, dict):
        if llm_block.get("topic_type") == "健康":
            return True
        if llm_block.get("health_major") or llm_block.get("health_minor"):
            return True
    return bool(event.get("health_major") or event.get("health_minor"))


def _extract_tags(existing: Any, posts: Any) -> List[str]:
    tags: List[str] = []
    if isinstance(existing, list):
        for tag in existing:
            text = str(tag or "").strip("# ").strip()
            if text:
                tags.append(text)
    if not tags and isinstance(posts, list):
        for post in posts:
            if not isinstance(post, dict):
                continue
            content = (
                post.get("content_text")
                or post.get("text")
                or post.get("content")
                or post.get("text_raw")
                or ""
            )
            for match in _HASHTAG_PATTERN.findall(str(content)):
                candidate = match.strip()
                if candidate:
                    tags.append(candidate)
    deduped: List[str] = []
    seen: set[str] = set()
    for tag in tags:
        normalized = tag.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)
    return deduped


def _build_slim_event(name: str, event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    slim: Dict[str, Any] = {}
    llm_block = event.get("llm") or {}

    if llm_block:
        slim["llm"] = llm_block
    if "llm_status" in event:
        slim["llm_status"] = event.get("llm_status")
    for key in (
        "risk_score",
        "risk_dims",
        "risk_low",
        "risk_mid",
        "risk_high",
        "risk_level",
        "risk_level_label",
        "last_content_update_date",
    ):
        if key in event:
            slim[key] = event.get(key)

    slim["name"] = event.get("name") or name
    title = event.get("title")
    if title:
        slim["title"] = title
    slug = event.get("slug") or slugify_title(title or event.get("name") or name)
    if slug:
        slim["slug"] = slug
    if "last_seen" in event:
        slim["last_seen"] = event.get("last_seen")
    if "last_seen_at" in event:
        slim["last_seen_at"] = event.get("last_seen_at")

    hot_values = event.get("hot_values")
    if isinstance(hot_values, dict) and hot_values:
        slim["hot_values"] = hot_values
    else:
        for key in ("hot", "heat", "score"):
            if key in event:
                slim[key] = event.get(key)

    if "summary_html" in event:
        slim["summary_html"] = event.get("summary_html")

    if _is_health_topic(event):
        health_major = event.get("health_major") or llm_block.get("health_major")
        health_minor = event.get("health_minor") or llm_block.get("health_minor")
        if health_major:
            slim["health_major"] = health_major
        if health_minor:
            slim["health_minor"] = health_minor
        if "summary" in event:
            slim["summary"] = event.get("summary")
        if "description" in event:
            slim["description"] = event.get("description")
        tags = _extract_tags(event.get("tags"), event.get("posts"))
        if tags:
            slim["tags"] = tags
        if "appeared_hours" in event:
            slim["appeared_hours"] = event.get("appeared_hours")
        if "first_seen" in event:
            slim["first_seen"] = event.get("first_seen")
        if "first_seen_at" in event:
            slim["first_seen_at"] = event.get("first_seen_at")
        if "last_seen" in event:
            slim["last_seen"] = event.get("last_seen")
        if "last_seen_at" in event:
            slim["last_seen_at"] = event.get("last_seen_at")

    return slim


def _cleanup_date_dirs(base_dir, cutoff_date: date, *, label: str) -> int:
    removed = 0
    if not base_dir.exists():
        return removed
    for entry in base_dir.iterdir():
        if not entry.is_dir():
            continue
        target_date = _parse_date_token(entry.name)
        if not target_date:
            continue
        if target_date > cutoff_date:
            continue
        try:
            shutil.rmtree(entry)
            removed += 1
        except Exception:
            logger.exception("Failed to remove %s directory %s", label, entry)
    return removed


def _slim_archive_file(date_str: str) -> Dict[str, int]:
    with ARCHIVE_LOCK:
        archive = load_daily_archive(date_str)
        if not isinstance(archive, dict) or not archive:
            return {"events_slimmed": 0, "events_skipped": 0, "changed": 0}
        changed = False
        slimmed = 0
        skipped = 0
        for name, event in archive.items():
            if not isinstance(event, dict):
                skipped += 1
                continue
            slim_event = _build_slim_event(name, event)
            if slim_event is None:
                skipped += 1
                continue
            if slim_event != event:
                archive[name] = slim_event
                slimmed += 1
                changed = True
        if changed:
            save_daily_archive(date_str, archive)
        return {"events_slimmed": slimmed, "events_skipped": skipped, "changed": int(changed)}


def run_archive_retention() -> None:
    today = datetime.now(tz=CHINA_TZ).date()
    slim_cutoff = today - timedelta(days=SLIM_RETENTION_DAYS)
    delete_cutoff = today - timedelta(days=DELETE_RETENTION_DAYS)
    deleted_files = 0
    slimmed_files = 0
    slimmed_events = 0
    skipped_events = 0

    for path in ARCHIVE_DIR.glob("*.json"):
        token = path.stem
        target_date = _parse_date_token(token)
        if not target_date:
            continue
        if target_date <= delete_cutoff:
            try:
                path.unlink()
                deleted_files += 1
            except Exception:
                logger.exception("Failed to delete archive %s", path)
            continue
        if target_date <= slim_cutoff:
            stats = _slim_archive_file(token)
            if stats["changed"]:
                slimmed_files += 1
            slimmed_events += stats["events_slimmed"]
            skipped_events += stats["events_skipped"]

    removed_posts = _cleanup_date_dirs(POST_DIR, slim_cutoff, label="posts")
    aicard_hourly = AICARD_DIR / "hourly"
    removed_aicard = _cleanup_date_dirs(aicard_hourly, slim_cutoff, label="aicard")

    logger.info(
        (
            "Archive retention finished: deleted_files=%s slimmed_files=%s "
            "events_slimmed=%s events_skipped=%s posts_dirs_removed=%s aicard_dirs_removed=%s "
            "(slim_cutoff=%s delete_cutoff=%s)"
        ),
        deleted_files,
        slimmed_files,
        slimmed_events,
        skipped_events,
        removed_posts,
        removed_aicard,
        slim_cutoff.isoformat(),
        delete_cutoff.isoformat(),
    )


def run_hourly_retention() -> None:
    try:
        today = datetime.now(tz=CHINA_TZ).date()
        retention_days = max(1, HOURLY_RETENTION_DAYS)
        cutoff = today - timedelta(days=retention_days)
        removed = _cleanup_date_dirs(HOURLY_DIR, cutoff, label="hourly")
        logger.info(
            "Hourly retention finished: removed_dirs=%s (cutoff=%s retention_days=%s)",
            removed,
            cutoff.isoformat(),
            retention_days,
        )
    except Exception:
        logger.exception("Hourly retention job failed")


def _now_iso() -> str:
    return datetime.now(tz=CHINA_TZ).isoformat()

#更新归档文件
def _mutate_event(
    date_str: str,
    archive: Dict[str, Any],
    name: str,
    reason: str,
    mutator: Callable[[Dict[str, Any]], None],
) -> None:
    with ARCHIVE_LOCK:
        event = archive.setdefault(name, {})
        mutator(event)
        archive[name] = event
        save_daily_archive(date_str, archive)
        logger.debug("Persisted archive %s/%s (%s)", date_str, name, reason)


def _set_llm_status(
    date_str: str,
    archive: Dict[str, Any],
    name: str,
    state: str,
    reason: str,
    **extra: Any,
) -> None:
    timestamp = _now_iso()
    payload = {"state": state, "updated_at": timestamp}
    if state == "processing":
        payload["started_at"] = timestamp
    payload.update(extra)

    def _apply(event: Dict[str, Any]) -> None:
        event["llm_status"] = payload

    _mutate_event(date_str, archive, name, reason, _apply)

#风险计算
def _persist_llm_success(
    date_str: str,
    today: str,
    archive: Dict[str, Any],
    name: str,
    posts: List[Dict[str, Any]],
    llm_res: Any,
) -> None:
    timestamp = _now_iso()

    def _apply(event: Dict[str, Any]) -> None:
        event["posts"] = posts
        llm_payload = {
            "sentiment": llm_res.sentiment,
            "region": llm_res.region,
            "topic_type": llm_res.topic_type,
            "source": llm_res.source,
        }
        if llm_res.health_major:
            llm_payload["health_major"] = llm_res.health_major
        if llm_res.health_minor:
            llm_payload["health_minor"] = llm_res.health_minor
        if llm_res.sentiment_vector:
            llm_payload["sentiment_vector"] = dict(llm_res.sentiment_vector)
        event["llm"] = llm_payload
        hot_values = event.get("hot_values") or {}
        current_hot = None
        prev_hot = None
        if hot_values:
            keys = sorted(hot_values.keys())
            current_hot = hot_values.get(keys[-1])
            if len(keys) >= 2:
                prev_hot = hot_values.get(keys[-2])
        growth = calc_growth(current_hot, prev_hot)
        negativity = calc_negativity(llm_res.sentiment)
        sensitivity = calc_sensitivity(llm_res.topic_type or "其他")
        crowd = calc_crowd(posts)
        event["risk_dims"] = {
            "negativity": negativity,
            "growth": growth,
            "sensitivity": sensitivity,
            "crowd": crowd,
        }
        event["risk_score"] = aggregate_score(event["risk_dims"])
        tiers = risk_tier_segments(event["risk_score"])
        event["risk_low"] = tiers["low"]
        event["risk_mid"] = tiers["mid"]
        event["risk_high"] = tiers["high"]
        event["risk_level"] = risk_level_from_score(event["risk_score"])
        event["risk_level_label"] = risk_level_label(event["risk_level"])
        event["last_content_update_date"] = today
        event["posts"] = posts[:20]  # 限制写回归档的贴文数量，避免文件过大
        event["llm_status"] = {
            "state": "succeeded",
            "updated_at": timestamp,
            "source": llm_res.source,
        }

    _mutate_event(date_str, archive, name, f"{name}:done", _apply)


def _process_event_llm(
    name: str,
    date_str: str,
    today: str,
    archive: Dict[str, Any],
) -> Dict[str, Any]:
    event = archive.get(name)
    if event is None:
        logger.warning("Event %s missing from archive %s", name, date_str)
        return {"status": "missing", "name": name}
    posts = _load_posts_for_event(date_str, event)
    if not posts:
        logger.debug("Skipping %s: no posts available for analysis", name)
        _set_llm_status(
            date_str,
            archive,
            name,
            "skipped",
            f"{name}:no_posts",
            detail="no_posts",
        )
        return {"status": "no_posts", "name": name}
    _set_llm_status(date_str, archive, name, "processing", f"{name}:processing")
    try:
        llm_res = call_openai(posts, name)
    except Exception as exc:  # pragma: no cover - network failures
        _set_llm_status(
            date_str,
            archive,
            name,
            "error",
            f"{name}:error",
            error=str(exc),
        )
        logger.exception("LLM analysis failed for %s: %s", name, exc)
        return {"status": "error", "name": name, "error": str(exc)}
    _persist_llm_success(date_str, today, archive, name, posts, llm_res)

    logger.info(
        "Updated %s risk metrics: sentiment=%.2f region=%s topic=%s score=%.2f",
        name,
        llm_res.sentiment,
        llm_res.region,
        llm_res.topic_type,
        archive.get(name, {}).get("risk_score"),
    )
    return {"status": "refreshed", "name": name}


def daily_llm_update(*, target_date: Optional[str] = None, force: bool = False) -> None:
    if target_date:
        target_str = target_date
    else:
        target_str = (datetime.now(tz=CHINA_TZ) - timedelta(days=1)).strftime("%Y-%m-%d")
    today_marker = datetime.now(tz=CHINA_TZ).strftime("%Y-%m-%d")
    archive = load_daily_archive(target_str)
    if not archive:
        logger.warning("Daily LLM update skipped: no archive for %s", target_str)
        return
    logger.info("Starting daily LLM update for %s (force=%s)", target_str, force)
    changed = False
    refreshed = 0
    skipped_recent = 0
    skipped_without_posts = 0
    errors = 0
    dedup_candidates: Dict[str, Dict[str, Any]] = {}
    for raw_name, event in archive.items():
        if (not force) and event.get("last_content_update_date") == today_marker:
            skipped_recent += 1
            continue
        canonical = _canonical_topic_name(raw_name, event)
        hot_score = _extract_hot_score(event)
        existing = dedup_candidates.get(canonical)
        if not existing or hot_score > existing["hot"]:
            dedup_candidates[canonical] = {
                "archive_name": raw_name,
                "hot": hot_score,
            }

    ranked_candidates = sorted(
        dedup_candidates.values(),
        key=lambda item: item["hot"],
        reverse=True,
    )
    total_candidates = len(ranked_candidates)
    if LLM_ANALYSIS_TOP_K and LLM_ANALYSIS_TOP_K > 0:
        ranked_candidates = ranked_candidates[:LLM_ANALYSIS_TOP_K]
    pending_names: List[str] = [item["archive_name"] for item in ranked_candidates]

    if pending_names:
        worker_count = max(1, min(LLM_ANALYSIS_WORKERS, len(pending_names)))
        logger.info(
            "Dispatching %s/%s deduplicated events for %s with %s workers (top_k=%s)",
            len(pending_names),
            total_candidates,
            target_str,
            worker_count,
            LLM_ANALYSIS_TOP_K,
        )
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {
                executor.submit(_process_event_llm, name, target_str, today_marker, archive): name
                for name in pending_names
            }
            for future in as_completed(futures):
                name = futures[future]
                try:
                    result = future.result()
                except Exception as exc:  # pragma: no cover - unexpected
                    errors += 1
                    logger.exception("LLM worker crashed for %s: %s", name, exc)
                    continue
                status = result.get("status")
                if status == "refreshed":
                    refreshed += 1
                    changed = True
                elif status == "no_posts":
                    skipped_without_posts += 1
                elif status in {"error", "missing"}:
                    errors += 1

    else:
        logger.info(
            "No pending events for %s (skipped_recent=%s)",
            target_str,
            skipped_recent,
        )

    if changed:
        save_daily_archive(target_str, archive)
        warnings = _update_risk_snapshots(target_str, push=True)
        logger.info(
            (
                "Daily LLM update finished for %s: refreshed=%s skipped_recent=%s "
                "skipped_no_posts=%s errors=%s warnings=%s"
            ),
            target_str,
            refreshed,
            skipped_recent,
            skipped_without_posts,
            errors,
            len((warnings or {}).get("events", [])),
        )
    else:
        warnings = _update_risk_snapshots(target_str, push=False)
        logger.info(
            (
                "Daily LLM update finished for %s with no changes "
                "(skipped_recent=%s skipped_no_posts=%s errors=%s warnings=%s)"
            ),
            target_str,
            skipped_recent,
            skipped_without_posts,
            errors,
            len((warnings or {}).get("events", [])),
        )
    try:
        update_central_cache_for_date(target_str, archive, max_days=LIGHT_RETENTION_DAYS)
    except Exception:
        logger.exception("Failed to update central cache for %s", target_str)


def top_risk_warnings(window_days: int = 7, top_k: int = 5) -> Dict[str, Any]:
    today = datetime.now(tz=CHINA_TZ).date()
    events: List[Dict[str, Any]] = []
    for i in range(1, window_days + 1):
        date_str = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        archive = load_daily_archive(date_str)
        for name, event in archive.items():
            seen_at = event.get("last_seen_at", f"{date_str}T00:00:00")[:10]
            recency = (today - datetime.strptime(seen_at, "%Y-%m-%d").date()).days
            score = float(event.get("risk_score", 0.0))
            events.append({
                "name": name,
                "date": seen_at,
                "risk_score": score,
                "risk_low": event.get("risk_low", 0.0),
                "risk_mid": event.get("risk_mid", 0.0),
                "risk_high": event.get("risk_high", 0.0),
                "risk_level": event.get("risk_level") or risk_level_from_score(score),
                "risk_level_label": event.get("risk_level_label") or risk_level_label(event.get("risk_level") or risk_level_from_score(score)),
                "llm": event.get("llm", {}),
                "risk_dims": event.get("risk_dims", {}),
                "sort_key": score - recency * 5.0,
            })
    events.sort(key=lambda item: item["sort_key"], reverse=True)
    return {"generated_at": datetime.now(tz=CHINA_TZ).isoformat(), "events": events[:top_k]}


def start_scheduler() -> BackgroundScheduler:
    global _SCHEDULER
    if _SCHEDULER:
        return _SCHEDULER
    scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
    if MONITOR_ENABLED:
        monitor_trigger = _parse_monitor_cron(MONITOR_CRON) if MONITOR_CRON else None
        if monitor_trigger:
            scheduler.add_job(
                _monitor_session,
                monitor_trigger,
                id="hot_topics_monitor",
                max_instances=1,
            )
            logger.info("Hot topics monitor scheduled via cron: %s", MONITOR_CRON)
        else:
            scheduler.add_job(
                _monitor_tick,
                "interval",
                minutes=HOUR_CHECK_INTERVAL_MINUTES,
                id="hot_topics_monitor",
                next_run_time=datetime.now(tz=CHINA_TZ),  # run once ASAP without blocking app startup
            )
    else:
        logger.info("Hot topics monitor disabled via WEIBO_MONITOR_ENABLED")
    if LLM_ENABLED:
        hh, mm = DAILY_LLM_TIME.split(":")
        scheduler.add_job(
            daily_llm_update,
            CronTrigger(hour=int(hh), minute=int(mm), timezone="Asia/Shanghai"),
            id="daily_llm",
        )
    else:
        logger.info("Daily LLM job disabled via WEIBO_LLM_ENABLED")
    try:
        slim_hh, slim_mm = SLIM_TIME.split(":")
        scheduler.add_job(
            run_archive_retention,
            CronTrigger(hour=int(slim_hh), minute=int(slim_mm), timezone="Asia/Shanghai"),
            id="archive_retention",
        )
    except Exception:
        logger.exception("Invalid WEIBO_SLIM_TIME=%s; retention job skipped", SLIM_TIME)
    try:
        clean_hh, clean_mm = HOURLY_CLEAN_TIME.split(":")
        scheduler.add_job(
            run_hourly_retention,
            CronTrigger(hour=int(clean_hh), minute=int(clean_mm), timezone="Asia/Shanghai"),
            id="hourly_retention",
        )
    except Exception:
        logger.exception(
            "Invalid WEIBO_HOURLY_CLEAN_TIME=%s; hourly retention job skipped",
            HOURLY_CLEAN_TIME,
        )
    if HEALTH_TOPIC_ENABLED:
        scheduler.add_job(
            _health_topic_job,
            "interval",
            minutes=HEALTH_TOPIC_INTERVAL_MINUTES,
            id="health_topic",
        )
        _health_topic_job()
    else:
        logger.info("Health topic job disabled via HEALTH_TOPIC_ENABLED")
    scheduler.start()
    _SCHEDULER = scheduler
    logger.info(
        (
            "Scheduler started (monitor_enabled=%s llm_enabled=%s health_enabled=%s "
            "interval=%sm monitor_cron=%s scheduled_llm=%s slim_time=%s hourly_clean_time=%s health_interval=%sm)"
        ),
        MONITOR_ENABLED,
        LLM_ENABLED,
        HEALTH_TOPIC_ENABLED,
        HOUR_CHECK_INTERVAL_MINUTES,
        MONITOR_CRON,
        DAILY_LLM_TIME,
        SLIM_TIME,
        HOURLY_CLEAN_TIME,
        HEALTH_TOPIC_INTERVAL_MINUTES,
    )
    return scheduler
