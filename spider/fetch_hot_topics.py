import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

import requests

from spider.aicard_client import AICardCooldownError, AICardRateLimitError
from spider.aicard_service import ensure_aicard_snapshot
from spider.config import get_env_list, get_env_str
from spider.crawler_core import slugify_title
from backend.config import ARCHIVE_DIR, POST_DIR
from backend.storage import load_daily_archive, save_daily_archive

CHINA_TZ = timezone(timedelta(hours=8))

# ------- 配置 -------
_DEFAULT_HOT_TOPIC_DATES = ["2025-10-26"]
_DEFAULT_HOT_TOPIC_HOURS = [15]


def _coerce_hours(values: Sequence[str], fallback: Sequence[int]) -> List[int]:
    hours: List[int] = []
    for raw in values:
        try:
            hour = int(raw)
        except (TypeError, ValueError):
            logging.warning("WEIBO_FETCH_HOURS 包含无效数字：%s", raw)
            continue
        if 0 <= hour <= 23:
            hours.append(hour)
        else:
            logging.warning("WEIBO_FETCH_HOURS 超出范围(0-23)：%s", raw)
    return list(hours) if hours else list(fallback)


HOT_TOPIC_DATES = get_env_list("WEIBO_FETCH_DATES", _DEFAULT_HOT_TOPIC_DATES)
HOT_TOPIC_HOURS = _coerce_hours(
    get_env_list("WEIBO_FETCH_HOURS", [str(hour) for hour in _DEFAULT_HOT_TOPIC_HOURS]),
    _DEFAULT_HOT_TOPIC_HOURS,
)
HOT_TOPIC_SOURCE = get_env_str(
    "WEIBO_FETCH_SOURCE",
    "https://raw.githubusercontent.com/lxw15337674/weibo-trending-hot-history/"
    "refs/heads/master/api/{date}/{hour}.json",
)
LOG_LEVEL = getattr(logging, (get_env_str("WEIBO_FETCH_LOG_LEVEL", "INFO") or "INFO").upper(), logging.INFO)

BASE_TOPIC_FIELDS = [
    "title",
    "category",
    "description",
    "url",
    "hot",
    "ads",
    "readCount",
    "discussCount",
    "origin",
    "appeared_hours",
    "first_seen",
    "last_seen",
    "last_post_refresh",
    "post_output",
    "known_ids",
    "needs_refresh",
    "slug",
    "aicard",
    "latest_posts",
    "last_post_total",
]

TOPIC_DEFAULTS: Dict[str, Any] = {
    "description": "",
    "appeared_hours": [],
    "known_ids": [],
    "needs_refresh": True,
    "aicard": {},
    "latest_posts": {},
    "last_post_total": 0,
    "ads": False,
}

def order_topic_fields(record: Dict[str, Any]) -> Dict[str, Any]:
    """Ensure topic dicts follow BASE_TOPIC_FIELDS ordering for stable JSON output."""
    ordered: Dict[str, Any] = {}
    for key in BASE_TOPIC_FIELDS:
        if key in record:
            ordered[key] = record[key]
    for key, value in record.items():
        if key not in ordered:
            ordered[key] = value
    return ordered


def ensure_dirs() -> None:
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    POST_DIR.mkdir(parents=True, exist_ok=True)


def fetch_hour_topics(date_str: str, hour: int) -> List[Dict]:
    url = HOT_TOPIC_SOURCE.format(date=date_str, hour=f"{hour:02d}")
    logging.info("获取热榜：%s", url)
    response = requests.get(url, timeout=10)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, list):
        raise ValueError(f"{url} 返回非列表数据")
    return data


def iso_time(date_str: str, hour: int) -> str:
    dt = datetime.fromisoformat(f"{date_str}T{hour:02d}:00:00")
    return dt.replace(tzinfo=CHINA_TZ).isoformat(timespec="seconds")


def normalize_topic_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """补齐 daily archive 需要的基础字段，避免后续文件结构不一致。"""
    if not record.get("description"):
        record["description"] = record.get("title") or ""
    for key, default in TOPIC_DEFAULTS.items():
        value = record.get(key)
        if value is None:
            record[key] = default() if callable(default) else default
        elif key in {"appeared_hours", "known_ids"} and not isinstance(value, list):
            record[key] = list(value) if value else []
        elif key in {"aicard", "latest_posts"} and not isinstance(value, dict):
            record[key] = {}
    record.setdefault("first_seen", record.get("last_seen"))
    record.setdefault("last_seen", record.get("first_seen"))
    return order_topic_fields(record)

def upsert_topic(record_map: Dict[str, Dict], topic: Dict, date_str: str, hour: int) -> Optional[Dict]:
    title = (topic.get("title") or "").strip()
    if not title:
        return None
    hour_str = f"{hour:02d}"
    seen_time = iso_time(date_str, hour)
    record = record_map.get(title)
    if not record:
        record = dict(topic)
        record["appeared_hours"] = [hour_str]
        record["first_seen"] = seen_time
        record["last_seen"] = seen_time
        record["last_post_refresh"] = None
        record["post_output"] = None
        record["known_ids"] = []
        record["needs_refresh"] = True
        record["slug"] = slugify_title(title)
        record_map[title] = normalize_topic_record(record)
        return record_map[title]

    # 更新已有事件
    record.update(topic)
    record.setdefault("appeared_hours", [])
    if hour_str not in record["appeared_hours"]:
        record["appeared_hours"].append(hour_str)
    record["last_seen"] = seen_time
    record.setdefault("first_seen", seen_time)
    record.setdefault("last_post_refresh", None)
    record.setdefault("known_ids", [])
    record["slug"] = slugify_title(title)
    if record.get("last_post_refresh") != date_str:
        record["needs_refresh"] = True
    return normalize_topic_record(record)


def process_day(date_str: str, hours: List[int]) -> None:
    daily_data = load_daily_archive(date_str)
    new_titles: List[str] = []
    for hour in hours:
        try:
            topics = fetch_hour_topics(date_str, hour)
        except Exception as exc:  # pylint: disable=broad-except
            logging.error("获取 %s %s 时段失败：%s", date_str, hour, exc)
            continue
        for topic in topics:
            title = (topic.get("title") or "").strip()
            if title and title not in daily_data:
                new_titles.append(title)
            record = upsert_topic(daily_data, topic, date_str, hour)
            if not record:
                continue
            snapshot = None
            try:
                snapshot = ensure_aicard_snapshot(
                    title,
                    date_str,
                    hour,
                    slug=record.get("slug"),
                    logger=logging.getLogger("aicard"),
                )
            except AICardCooldownError as exc:
                logging.warning(
                    "AI Card cooldown (%s) for %s %02d when fetching %s; skipping snapshot this pass (retry_after=%ss)",
                    exc.level,
                    date_str,
                    hour,
                    title,
                    exc.retry_after,
                )
            except AICardRateLimitError as exc:
                logging.warning(
                    "AI Card rate limited for %s %02d when fetching %s (%s); skipping snapshot this pass",
                    date_str,
                    hour,
                    title,
                    exc,
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
    try:
        logging.debug(
            "Daily archive refreshed for %s: total topics=%s",
            date_str,
            len(daily_data),
        )
    except Exception as exc:  # pylint: disable=broad-except
        logging.error("更新 %s 日热度汇总失败：%s", date_str, exc)
    if daily_data:
        logging.info(
            "日期 %s：共 %s 个话题，新增 %s 个，待更新 %s 个",
            date_str,
            len(daily_data),
            len(set(new_titles)),
            sum(1 for item in daily_data.values() if item.get("needs_refresh")),
        )


def main() -> None:
    logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s [%(levelname)s] %(message)s")
    ensure_dirs()
    for date_str in HOT_TOPIC_DATES:
        process_day(date_str, HOT_TOPIC_HOURS)


if __name__ == "__main__":
    main()
