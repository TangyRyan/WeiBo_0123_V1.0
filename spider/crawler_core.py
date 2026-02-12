import hashlib
import json
import logging
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import requests

from spider.cookie_manager import get_cookie, refresh_cookie_sync
from spider.notify_email import notify_cookie_invalid
from spider.rate_limiter import create_policy_from_env

try:
    from bs4 import BeautifulSoup  # type: ignore
except ImportError:  # pragma: no cover - optional dependency
    BeautifulSoup = None


BASE_URL = "https://m.weibo.cn/api/container/getIndex"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"
    ),
    "Referer": "https://m.weibo.cn/",
    "X-Requested-With": "XMLHttpRequest",
    "mweibo-pwa": "1",
    "Pragma": "no-cache",
    "X-XSRF-TOKEN": "d8443b",
    "sec-ch-ua-mobile": "?1",
}
REQUEST_TIMEOUT = 10
MAX_RETRIES = 3
RETRY_BACKOFF = 2
SLEEP_RANGE = (0.6, 1.2)

CHINA_TZ = timezone(timedelta(hours=8))

_CRAWLER_POLICY = create_policy_from_env(
    "CRAWLER",
    base_delay=3.0,
    jitter=0.3,
    max_backoff_attempts=3,
    soft_range=(300, 900),
    hard_range=(1800, 3600),
    cooldown_window=3600,
    soft_threshold=2,
)

_CONNECTION_NOTIFY_LABEL = "crawler_connection_error"
_COOKIE_NOTIFY_LABEL = "crawler_cookie_refresh"
_COOKIE_LABEL = "env"


def _apply_cookie(session: requests.Session, cookie: str) -> str:
    cookie = (cookie or "").strip()
    if cookie:
        session.headers["Cookie"] = cookie
    else:
        session.headers.pop("Cookie", None)
    return cookie


def _refresh_cookie(session: requests.Session, current_cookie: str, reason: str) -> str:
    new_cookie = refresh_cookie_sync(reason, notify_label=_COOKIE_NOTIFY_LABEL)
    if new_cookie:
        session.headers["Cookie"] = new_cookie
        logging.warning("Refreshed cookie (%s)", reason)
        return new_cookie
    if current_cookie:
        session.headers["Cookie"] = current_cookie
    return current_cookie


def _is_connection_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(
        token in text
        for token in (
            "ssl",
            "eof",
            "unexpected eof",
            "timed out",
            "timeout",
            "connection reset",
            "connection aborted",
            "connection closed",
            "remote end closed",
        )
    )


def _notify_connection_error(reason: str) -> None:
    try:
        notify_cookie_invalid(_CONNECTION_NOTIFY_LABEL, reason)
    except Exception:
        logging.exception("Failed to notify connection error.")


@dataclass
class CrawlParams:
    hashtag: str
    top_n: int = 30
    max_pages: int = 5
    min_score: float = 0.0
    since: Optional[datetime] = None
    skip_ids: Optional[Sequence[str]] = None


def slugify_title(title: str) -> str:
    slug = re.sub(r"[^0-9A-Za-z]+", "-", title).strip("-").lower()
    if slug:
        return slug
    digest = hashlib.md5(title.encode("utf-8"), usedforsecurity=False).hexdigest()[:8]
    return f"topic-{digest}"


def ensure_hashtag_format(title: str) -> str:
    stripped = title.strip()
    if not stripped.startswith("#"):
        stripped = f"#{stripped}"
    if not stripped.endswith("#"):
        stripped = f"{stripped}#"
    return stripped


def load_skip_ids(path: Optional[Path]) -> Set[str]:
    if not path or not path.exists():
        return set()
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return set()
    try:
        maybe = json.loads(text)
        if isinstance(maybe, list):
            return {str(item) for item in maybe}
    except json.JSONDecodeError:
        pass
    return {line.strip() for line in text.splitlines() if line.strip()}


def clean_html(raw_html: str) -> str:
    if not raw_html:
        return ""
    if BeautifulSoup:
        soup = BeautifulSoup(raw_html, "html.parser")
        for span in soup.find_all("span", class_="url-icon"):
            span.decompose()
        for tag in soup.find_all("a"):
            tag.replace_with(tag.get_text())
        for br in soup.find_all("br"):
            br.replace_with("\n")
        text = soup.get_text()
    else:  # pragma: no cover - fallback path
        text = raw_html
        text = text.replace("<br />", "\n").replace("<br/>", "\n").replace("<br>", "\n")
        text = re.sub(r"<span[^>]*url-icon[^>]*>.*?</span>", "", text, flags=re.IGNORECASE)
        text = re.sub(r"</?a[^>]*>", "", text, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", "", text)
    return unescape(text).strip()


def parse_created_at(raw: str) -> Optional[datetime]:
    if not raw:
        return None
    known_formats = [
        "%a %b %d %H:%M:%S %z %Y",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S",
    ]
    for fmt in known_formats:
        try:
            dt = datetime.strptime(raw, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=CHINA_TZ)
        except ValueError:
            continue
    return None


def ensure_contains_topic(mblog: Dict[str, Any], hashtag: str) -> bool:
    hashtag_lower = hashtag.lower()
    candidates: List[str] = [
        mblog.get("text", ""),
        mblog.get("raw_text", ""),
        (mblog.get("page_info") or {}).get("title", ""),
        (mblog.get("page_info") or {}).get("page_title", ""),
    ]
    return any(hashtag_lower in (text or "").lower() for text in candidates)


def extract_pics(mblog: Dict[str, Any]) -> List[str]:
    pics: List[str] = []
    if isinstance(mblog.get("pics"), list):
        for pic in mblog["pics"]:
            large = (pic or {}).get("large") or {}
            url = large.get("url") or pic.get("url")
            if url:
                pics.append(url)
    elif mblog.get("pic_ids") and mblog.get("pic_infos"):
        infos = mblog["pic_infos"]
        for pid in mblog["pic_ids"]:
            info = infos.get(pid) or {}
            large = info.get("large") or {}
            url = large.get("url") or info.get("original") or info.get("url")
            if url:
                pics.append(url)
    return pics


def extract_video(mblog: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    page_info = mblog.get("page_info") or {}
    if page_info.get("type") not in {"video", "live"}:
        return None
    media = page_info.get("media_info") or {}
    streams: Dict[str, str] = {}
    for key in ["stream_url_hd", "stream_url", "h265_mp4_hd", "h265_mp4_ld"]:
        url = media.get(key)
        if url:
            short = key.replace("stream_url_", "").replace("h265_", "")
            streams[short] = url
    if media.get("urls"):
        streams.update(media["urls"])
    return {
        "title": page_info.get("page_title") or page_info.get("title"),
        "cover": (page_info.get("page_pic") or {}).get("url"),
        "duration": media.get("duration"),
        "streams": streams or None,
    }


def calculate_score(mblog: Dict[str, Any]) -> float:
    likes = mblog.get("attitudes_count", 0) or 0
    comments = mblog.get("comments_count", 0) or 0
    reposts = mblog.get("reposts_count", 0) or 0
    return likes * 0.6 + comments * 0.3 + reposts * 0.1



def fetch_page(
    session: requests.Session, hashtag: str, page: int, cookie: str
) -> Tuple[Optional[Dict[str, Any]], Optional[str], str]:
    params = {"containerid": f"231522type=60&q={hashtag}&t=10"}
    if page > 1:
        params["page"] = page

    attempt = 0
    last_error: Optional[str] = None
    policy = _CRAWLER_POLICY
    active_cookie = cookie
    refreshed = False
    notified_connection_error = False
    while attempt < MAX_RETRIES:
        attempt += 1
        while True:
            in_cooldown, wait_cd = policy.in_cooldown()
            if not in_cooldown:
                break
            logging.warning(
                "Crawler cooldown (%s) active; sleeping %.1fs before retrying page %s",
                policy.cooldown_level,
                wait_cd,
                page,
            )
            time.sleep(wait_cd)
        try:
            response = session.get(BASE_URL, params=params, timeout=REQUEST_TIMEOUT)
            if response.status_code == 200:
                data = response.json()
                policy.record_success()
                if data.get("ok") == 1:
                    return data, None, active_cookie
                last_error = f"ok={data.get('ok')}"
                msg = str(data.get("msg") or data.get("message") or "").lower()
                if any(key in msg for key in ["login", "登录", "auth", "频繁"]):
                    if not refreshed:
                        active_cookie = _refresh_cookie(session, active_cookie, f"api_msg:{msg[:32]}")
                        refreshed = True
                        continue
                    return None, last_error, active_cookie
                logging.warning("page %s returned ok=%s (stopping)", page, data.get("ok"))
                return None, last_error, active_cookie
            if response.status_code in {403, 418}:
                last_error = f"http_{response.status_code}"
                logging.error("HTTP %s requires new cookie or lower frequency", response.status_code)
                if not refreshed:
                    active_cookie = _refresh_cookie(session, active_cookie, last_error)
                    refreshed = True
                wait_seconds = policy.next_delay()
                logging.warning(
                    "Crawler rate limit backoff sleeping %.1fs (attempt %s/%s)",
                    wait_seconds,
                    attempt,
                    MAX_RETRIES,
                )
                time.sleep(wait_seconds)
                cooldown = policy.record_failure()
                if cooldown:
                    logging.error(
                        "Crawler entered %s cooldown for %.0fs while fetching page %s",
                        cooldown.level,
                        cooldown.duration,
                        page,
                    )
                continue
            last_error = f"http_{response.status_code}"
            logging.warning("HTTP %s on page %s, retrying...", response.status_code, page)
        except requests.RequestException as exc:
            last_error = f"exception:{exc}"
            logging.warning("request error on page %s: %s", page, exc)
            if not notified_connection_error and _is_connection_error(exc):
                notified_connection_error = True
                _notify_connection_error(f"crawler:{_COOKIE_LABEL}:{exc}")
        delay = (RETRY_BACKOFF ** (attempt - 1)) + random.uniform(0, 0.5)
        time.sleep(delay)
    logging.error("page %s failed after max retries", page)
    return None, last_error or "max_retries", active_cookie



def crawl_topic(params: CrawlParams) -> Dict[str, Any]:
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    active_cookie = _apply_cookie(session, get_cookie())
    logging.info("Using cookie %s for topic %s", _COOKIE_LABEL, params.hashtag)
    skip_ids = set(params.skip_ids or [])
    seen_ids = set(skip_ids)
    collected: List[Dict[str, Any]] = []

    stats: Dict[str, int] = {
        "pages_requested": 0,
        "pages_succeeded": 0,
        "cards_seen": 0,
        "type9_seen": 0,
    }
    rejections: Dict[str, int] = {
        "missing_id": 0,
        "skip_list": 0,
        "duplicate": 0,
        "missing_hashtag": 0,
        "too_old": 0,
        "below_min_score": 0,
    }
    errors: List[str] = []

    for page in range(1, params.max_pages + 1):
        stats["pages_requested"] += 1
        if page > 1:
            time.sleep(random.uniform(*SLEEP_RANGE))
        data, error, active_cookie = fetch_page(session, params.hashtag, page, active_cookie)
        if error:
            errors.append(f"page_{page}:{error}")
        if not data:
            break
        stats["pages_succeeded"] += 1
        cards = data.get("data", {}).get("cards") or []
        stats["cards_seen"] += len(cards)
        if not cards:
            logging.info("page %s returned no cards", page)
            break
        for card in cards:
            if card.get("card_type") != 9:
                continue
            stats["type9_seen"] += 1
            mblog = card.get("mblog") or {}
            mid = str(mblog.get("id") or mblog.get("mid") or "")
            if not mid:
                rejections["missing_id"] += 1
                continue
            if mid in skip_ids:
                rejections["skip_list"] += 1
                continue
            if mid in seen_ids:
                rejections["duplicate"] += 1
                continue
            if not ensure_contains_topic(mblog, params.hashtag):
                rejections["missing_hashtag"] += 1
                continue
            created = parse_created_at(mblog.get("created_at", ""))
            if params.since and created and created < params.since:
                rejections["too_old"] += 1
                continue
            item = normalize_mblog(mblog, created)
            item["score"] = calculate_score(mblog)
            if item["score"] < params.min_score:
                rejections["below_min_score"] += 1
                continue
            collected.append(item)
            seen_ids.add(mid)
        cardlist = data.get("data", {}).get("cardlistInfo") or {}
        if not cardlist.get("page"):
            break

    collected.sort(key=lambda x: (x["score"], x.get("created_at") or ""), reverse=True)
    limited = collected[: params.top_n]
    result: Dict[str, Any] = {
        "topic": params.hashtag,
        "fetched_at": datetime.now(tz=CHINA_TZ).isoformat(timespec="seconds"),
        "total": len(collected),
        "top_n": params.top_n,
        "items": limited,
    }

    if not limited:
        reason_parts: List[str] = []
        if stats["pages_succeeded"] == 0:
            if errors:
                reason_parts.append("fetch_failed")
            else:
                reason_parts.append("no_response")
        elif stats["type9_seen"] == 0:
            reason_parts.append("no_topic_cards")
        else:
            if rejections["missing_hashtag"] and rejections["missing_hashtag"] == stats["type9_seen"]:
                reason_parts.append("all_posts_missing_hashtag")
            if rejections["too_old"] and rejections["too_old"] == stats["type9_seen"]:
                reason_parts.append("all_posts_before_since")
            if rejections["below_min_score"] and rejections["below_min_score"] == stats["type9_seen"]:
                reason_parts.append("all_posts_below_min_score")
            if rejections["skip_list"] and rejections["skip_list"] == stats["type9_seen"]:
                reason_parts.append("all_posts_in_skip_ids")
            if not reason_parts:
                reason_parts.append("filtered_out")
        if errors and stats["pages_succeeded"]:
            reason_parts.append(errors[-1])
        result["empty_reason"] = "; ".join(dict.fromkeys(reason_parts))
        result["empty_debug"] = {
            "stats": stats,
            "rejections": rejections,
            "errors": errors,
        }

    return result


def normalize_mblog(mblog: Dict[str, Any], created: Optional[datetime]) -> Dict[str, Any]:
    mid = str(mblog.get("id") or mblog.get("mid") or "")
    bid = mblog.get("bid") or ""
    user = mblog.get("user") or {}
    created_iso = created.isoformat(timespec="seconds") if created else None
    text_raw = mblog.get("text") or ""
    return {
        "id": mid,
        "bid": bid,
        "url": f"https://m.weibo.cn/status/{bid}" if bid else None,
        "created_at": created_iso,
        "user_id": user.get("id"),
        "user_name": user.get("screen_name"),
        "verified": user.get("verified"),
        "region": mblog.get("region_name"),
        "source": mblog.get("source"),
        "text": clean_html(text_raw),
        "text_raw": text_raw,
        "reposts": mblog.get("reposts_count"),
        "comments": mblog.get("comments_count"),
        "likes": mblog.get("attitudes_count"),
        "pics": extract_pics(mblog),
        "video": extract_video(mblog),
    }
