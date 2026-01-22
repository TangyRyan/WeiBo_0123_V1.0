import asyncio
import logging
import re
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence

from spider.config import get_env_float, get_env_int, get_env_str
from spider.crawler_core import CHINA_TZ, CrawlParams, crawl_topic, ensure_hashtag_format, slugify_title
from spider.weibo_topic_detail import WeiboPost, get_top_20_hot_posts
from backend.proxy import attach_proxy_to_media
from backend.config import ARCHIVE_DIR, POST_DIR, KNOWN_IDS_MAX, SLIM_POST_TEXT_LIMIT
from backend.storage import load_daily_archive, save_daily_archive, to_data_relative, write_json

# ------- CONFIG -------
_DEFAULT_TARGET_DATE = "2025-10-25"


def _parse_since(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        logging.warning("WEIBO_POST_SINCE 无法解析，已忽略：%s", value)
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=CHINA_TZ)
    return parsed


TARGET_DATE = get_env_str("WEIBO_POST_TARGET_DATE", _DEFAULT_TARGET_DATE) or _DEFAULT_TARGET_DATE
TOP_N = get_env_int("WEIBO_POST_TOP_N", 30) or 30
MAX_PAGES = get_env_int("WEIBO_POST_MAX_PAGES", 5) or 5
MIN_SCORE = get_env_float("WEIBO_POST_MIN_SCORE", 0.0) or 0.0
SINCE = _parse_since(get_env_str("WEIBO_POST_SINCE"))
MAX_TOPICS_PER_RUN = get_env_int("WEIBO_POST_MAX_TOPICS", None)
if MAX_TOPICS_PER_RUN is not None and MAX_TOPICS_PER_RUN <= 0:
    MAX_TOPICS_PER_RUN = None
LOG_LEVEL = getattr(logging, (get_env_str("WEIBO_POST_LOG_LEVEL", "INFO") or "INFO").upper(), logging.INFO)
_POST_TOPIC_DELAY_RAW = get_env_int("WEIBO_POST_TOPIC_DELAY_SECONDS", 180) or 180
POST_TOPIC_DELAY_SECONDS = _POST_TOPIC_DELAY_RAW if _POST_TOPIC_DELAY_RAW > 0 else 0


def ensure_dirs() -> None:
    POST_DIR.mkdir(parents=True, exist_ok=True)


def load_archive(date_str: str) -> Dict[str, Dict]:
    path = ARCHIVE_DIR / f"{date_str}.json"
    if not path.exists():
        raise FileNotFoundError(f"archive {path} not found, please run fetch_hot_topics.py first")
    return load_daily_archive(date_str)


def save_archive(date_str: str, data: Dict[str, Dict]) -> None:
    save_daily_archive(date_str, data)
    logging.info("Updated archive file %s", ARCHIVE_DIR / f"{date_str}.json")


def update_topic(title: str, record: Dict, date_str: str) -> Dict:
    slug = record.get("slug") or slugify_title(title)
    record["slug"] = slug
    skip_ids = record.get("known_ids") or []
    searches = [
        ("hashtag", ensure_hashtag_format(title)),
        ("keyword", title.strip()),
    ]
    result = None
    used_mode = None
    for mode, term in searches:
        if not term:
            continue
        params = CrawlParams(
            hashtag=term,
            top_n=TOP_N,
            max_pages=MAX_PAGES,
            min_score=MIN_SCORE,
            since=SINCE,
            skip_ids=skip_ids,
        )
        candidate = crawl_topic(params)
        if result is None:
            result = candidate
            used_mode = mode
        if candidate.get("items"):
            result = candidate
            used_mode = mode
            break

    if result is None:
        result = {
            "topic": ensure_hashtag_format(title),
            "fetched_at": None,
            "total": 0,
            "top_n": TOP_N,
            "items": [],
        }
        used_mode = "hashtag"
    else:
        result["topic"] = ensure_hashtag_format(title)
        if used_mode == "keyword" and not result.get("items"):
            logging.info("Keyword fallback returned no posts for %s", title)
        elif used_mode == "keyword":
            logging.info("Keyword fallback captured posts for %s", title)

    if not result.get("items"):
        detail_items = _fetch_posts_via_topic_detail(title, TOP_N)
        if detail_items:
            result = {
                "topic": ensure_hashtag_format(title),
                "fetched_at": datetime.now(tz=CHINA_TZ).isoformat(timespec="seconds"),
                "total": len(detail_items),
                "top_n": TOP_N,
                "items": detail_items,
            }
            logging.info("Topic detail fallback captured posts for %s", title)
        else:
            logging.warning("No posts captured for %s after all fallbacks", title)

    post_path = POST_DIR / date_str / f"{slug}.json"
    post_path.parent.mkdir(parents=True, exist_ok=True)
    slim_result = _slim_post_payload(result or {}, text_limit=SLIM_POST_TEXT_LIMIT)
    write_json(post_path, slim_result)

    record["last_post_refresh"] = date_str
    snapshot_rel = to_data_relative(post_path)
    record["post_output"] = snapshot_rel
    known_ids = [item.get("id") for item in slim_result.get("items", []) if item.get("id")]
    if KNOWN_IDS_MAX > 0:
        known_ids = known_ids[:KNOWN_IDS_MAX]
    record["known_ids"] = known_ids
    has_posts = bool(slim_result.get("items"))
    record["latest_posts"] = {
        "snapshot": snapshot_rel,
        "total": slim_result.get("total", 0),
        "top_n": slim_result.get("top_n", TOP_N),
        "fetched_at": slim_result.get("fetched_at"),
        "has_posts": has_posts,
    }
    record["needs_refresh"] = not has_posts
    record["last_post_total"] = slim_result.get("total", 0)
    return record


def refresh_posts_for_date(
    date_str: str,
    max_topics: Optional[int] = None,
) -> Dict[str, List[str]]:
    ensure_dirs()
    archive = load_archive(date_str)
    refreshed: List[str] = []
    skipped: List[str] = []
    failed: List[str] = []
    for title, record in archive.items():
        if max_topics is not None and len(refreshed) >= max_topics:
            skipped.append(title)
            continue
        if not record.get("needs_refresh"):
            skipped.append(title)
            continue
        attempted = False
        try:
            attempted = True
            archive[title] = update_topic(title, record, date_str)
            refreshed.append(title)
        except Exception as exc:  # pylint: disable=broad-except
            logging.error("Updating topic %s failed: %s", title, exc)
            failed.append(title)
        if attempted and POST_TOPIC_DELAY_SECONDS:
            logging.info("Sleeping %ss before next topic", POST_TOPIC_DELAY_SECONDS)
            time.sleep(POST_TOPIC_DELAY_SECONDS)
    save_archive(date_str, archive)
    return {"refreshed": refreshed, "skipped": skipped, "failed": failed}


def main() -> None:
    logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s [%(levelname)s] %(message)s")
    result = refresh_posts_for_date(TARGET_DATE, MAX_TOPICS_PER_RUN)
    logging.info(
        "Post refresh completed: refreshed=%s skipped=%s failed=%s",
        len(result.get("refreshed", [])),
        len(result.get("skipped", [])),
        len(result.get("failed", [])),
    )
    if result.get("refreshed"):
        logging.info("Refreshed topics: %s", ", ".join(result["refreshed"][:10]))


def _fetch_posts_via_topic_detail(title: str, limit: int) -> List[Dict]:
    async def runner() -> List[Dict]:
        try:
            posts = await get_top_20_hot_posts(title)
        except Exception as exc:  # pylint: disable=broad-except
            logging.error("Topic detail crawler failed for %s: %s", title, exc)
            return []
        return _convert_detail_posts(posts, limit)

    return _run_async(runner)


def _convert_detail_posts(posts: Sequence[WeiboPost], limit: int) -> List[Dict]:
    items: List[Dict] = []
    max_items = limit if limit > 0 else len(posts)
    reference_time = datetime.now(tz=CHINA_TZ)
    for index, post in enumerate(posts[:max_items]):
        detail_url = getattr(post, "detail_url", "") or ""
        item_id = _generate_detail_id(detail_url, index)
        forwards = getattr(post, "forwards_count", 0) or 0
        comments = getattr(post, "comments_count", 0) or 0
        likes = getattr(post, "likes_count", 0) or 0
        item = {
            "id": item_id,
            "bid": None,
            "url": detail_url or None,
            "created_at": _normalize_timestamp(getattr(post, "timestamp", "") or "", reference_time),
            "user_id": None,
            "user_name": getattr(post, "author", "") or None,
            "verified": None,
            "region": None,
            "source": getattr(post, "source", "") or "",
            "text": getattr(post, "content", "") or "",
            "text_raw": getattr(post, "content", "") or "",
            "reposts": forwards,
            "comments": comments,
            "likes": likes,
            "pics": list(getattr(post, "image_links", []) or []),
            "video": _build_video_payload(getattr(post, "video_link", "") or ""),
            "score": forwards * 0.6 + comments * 0.3 + likes * 0.1,
        }
        items.append(item)
    return items


def _slim_post_payload(payload: Dict[str, Any], *, text_limit: int = 0) -> Dict[str, Any]:
    items = payload.get("items") or []
    slimmed = [_slim_post_item(item, text_limit=text_limit) for item in items if isinstance(item, dict)]
    return {
        "topic": payload.get("topic"),
        "fetched_at": payload.get("fetched_at"),
        "total": payload.get("total", len(slimmed)),
        "top_n": payload.get("top_n", TOP_N),
        "items": slimmed,
    }


def _slim_post_item(item: Dict[str, Any], *, text_limit: int = 0) -> Dict[str, Any]:
    text = item.get("text") or item.get("content") or item.get("text_raw") or ""
    if text_limit and len(text) > text_limit:
        text = text[:text_limit]
    pics = item.get("pics") or item.get("image_links") or []
    proxied_pics = attach_proxy_to_media(pics, images_only=True)
    proxied_video = attach_proxy_to_media(item.get("video"), images_only=True)
    return {
        "id": item.get("id") or item.get("mid") or item.get("post_id"),
        "bid": item.get("bid"),
        "url": item.get("url"),
        "created_at": item.get("created_at") or item.get("timestamp"),
        "user_name": item.get("user_name") or item.get("author"),
        "text": text,
        "reposts": int(item.get("reposts") or item.get("forwards_count") or 0),
        "comments": int(item.get("comments") or item.get("comments_count") or 0),
        "likes": int(item.get("likes") or item.get("likes_count") or 0),
        "pics": proxied_pics,
        "video": proxied_video,
    }


def _build_video_payload(video_link: str) -> Optional[Dict[str, Any]]:
    if not video_link:
        return None
    return {
        "title": None,
        "cover": None,
        "duration": None,
        "streams": {"stream_url": video_link},
    }


def _normalize_timestamp(raw: str, reference: Optional[datetime] = None) -> Optional[str]:
    raw = raw.strip()
    if not raw:
        return None

    ref = reference or datetime.now(tz=CHINA_TZ)

    def _resolve_year_candidates(month: int, day: int, hour: int, minute: int) -> Optional[datetime]:
        year_order = (ref.year, ref.year - 1)
        for year in year_order:
            try:
                candidate = datetime(year, month, day, hour, minute, tzinfo=CHINA_TZ)
            except ValueError:
                continue
            if candidate <= ref + timedelta(hours=1):
                return candidate
        for year in year_order:
            try:
                return datetime(year, month, day, hour, minute, tzinfo=CHINA_TZ)
            except ValueError:
                continue
        return None

    if re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", raw):
        try:
            dt = datetime.fromisoformat(raw)
            return dt.isoformat(timespec="seconds")
        except ValueError:
            pass

    base_formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M",
    ]
    for fmt in base_formats:
        try:
            dt = datetime.strptime(raw, fmt).replace(tzinfo=CHINA_TZ)
            return dt.isoformat(timespec="seconds")
        except ValueError:
            continue

    if raw == "刚刚":
        return ref.replace(microsecond=0).isoformat(timespec="seconds")

    m = re.match(r"^今天\s*(\d{1,2}):(\d{1,2})$", raw)
    if m:
        hour, minute = map(int, m.groups())
        dt = ref.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return dt.isoformat(timespec="seconds")

    m = re.match(r"^昨天\s*(\d{1,2}):(\d{1,2})$", raw)
    if m:
        hour, minute = map(int, m.groups())
        dt = (ref - timedelta(days=1)).replace(hour=hour, minute=minute, second=0, microsecond=0)
        return dt.isoformat(timespec="seconds")

    m = re.match(r"^前天\s*(\d{1,2}):(\d{1,2})$", raw)
    if m:
        hour, minute = map(int, m.groups())
        dt = (ref - timedelta(days=2)).replace(hour=hour, minute=minute, second=0, microsecond=0)
        return dt.isoformat(timespec="seconds")

    m = re.match(r"^(\d{1,2})月(\d{1,2})日\s*(\d{1,2}):(\d{1,2})$", raw)
    if m:
        month, day, hour, minute = map(int, m.groups())
        dt = _resolve_year_candidates(month, day, hour, minute)
        if dt:
            return dt.replace(second=0, microsecond=0).isoformat(timespec="seconds")
        return raw

    m = re.match(r"^(\d{4})年(\d{1,2})月(\d{1,2})日\s*(\d{1,2}):(\d{1,2})$", raw)
    if m:
        year, month, day, hour, minute = map(int, m.groups())
        dt = datetime(year, month, day, hour, minute, tzinfo=CHINA_TZ)
        return dt.isoformat(timespec="seconds")

    m = re.match(r"^(\d{1,2})[/-](\d{1,2})\s*(\d{1,2}):(\d{1,2})$", raw)
    if m:
        month, day, hour, minute = map(int, m.groups())
        dt = _resolve_year_candidates(month, day, hour, minute)
        if dt:
            return dt.replace(second=0, microsecond=0).isoformat(timespec="seconds")
        return raw

    relative_patterns = [
        (r"^(\d+)\s*秒前$", "seconds"),
        (r"^(\d+)\s*分钟前$", "minutes"),
        (r"^(\d+)\s*小时前$", "hours"),
        (r"^(\d+)\s*天前$", "days"),
    ]
    for pattern, unit in relative_patterns:
        m = re.match(pattern, raw)
        if m:
            value = int(m.group(1))
            dt = ref - timedelta(**{unit: value})
            dt = dt.replace(microsecond=0)
            return dt.isoformat(timespec="seconds")

    return raw


def _generate_detail_id(detail_url: str, index: int) -> str:
    if detail_url:
        return f"detail-{abs(hash(detail_url))}"
    return f"detail-{index}"


def _run_async(func):
    try:
        return asyncio.run(func())
    except RuntimeError as exc:
        if "asyncio.run()" in str(exc):
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(func())
            finally:
                loop.close()
        raise


def ensure_topic_posts(title: str, record: Dict, date_str: str) -> Dict:
    """Public helper to guarantee latest_posts populated for the given topic."""
    return update_topic(title, record, date_str)


if __name__ == "__main__":
    main()
