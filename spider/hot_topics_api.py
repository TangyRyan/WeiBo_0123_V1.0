import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from flask import Blueprint, Flask, Response, jsonify, request

from backend.proxy import attach_proxy_to_media, rewrite_html_images, rewrite_markdown_images, rewrite_text_media_urls
from spider.config import get_env_int, get_env_str
from spider.crawler_core import CHINA_TZ, slugify_title
from spider.aicard_client import AICardCooldownError, AICardRateLimitError
from spider.aicard_service import ensure_aicard_snapshot
from spider.update_posts import ensure_topic_posts, load_archive, save_archive
from spider.hot_topics_ws import HotTopicsRepository, HotTopicsSnapshot
from backend.config import (
    ARCHIVE_DIR as DEFAULT_ARCHIVE_DIR,
    HOURLY_DIR as DEFAULT_HOURLY_DIR,
    POST_DIR as DEFAULT_POST_DIR,
    AICARD_RETENTION_DAYS,
)
from backend.storage import from_data_relative

bp = Blueprint("hot_topics_api", __name__)
LOG_LEVEL = getattr(logging, (get_env_str("WEIBO_API_LOG_LEVEL", "INFO") or "INFO").upper(), logging.INFO)
DEFAULT_LIMIT = get_env_int("WEIBO_API_DAILY_LIMIT", 30) or 30
MAX_LIMIT = get_env_int("WEIBO_API_MAX_LIMIT", 60) or 60
MAX_HOURLY_LIMIT = get_env_int("WEIBO_API_MAX_HOURLY_LIMIT", 50) or 50
MAX_POST_LIMIT = get_env_int("WEIBO_API_MAX_POST_LIMIT", 50) or 50

def _resolve_data_path(raw_value: Optional[str], default: Path) -> Path:
    if not raw_value:
        return default
    return from_data_relative(raw_value)


HOURLY_DIR = _resolve_data_path(get_env_str("WEIBO_HOURLY_DIR"), DEFAULT_HOURLY_DIR)
POSTS_DIR = _resolve_data_path(get_env_str("WEIBO_POSTS_DIR"), DEFAULT_POST_DIR)
ARCHIVE_DIR = DEFAULT_ARCHIVE_DIR


def _summarize_day_heat(date_str: str) -> Optional[Dict[str, Any]]:
    path = ARCHIVE_DIR / f"{date_str}.json"
    if not path.exists():
        return None
    try:
        archive = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        logging.warning("Failed to parse archive %s: %s", path, exc)
        return None
    if not isinstance(archive, dict) or not archive:
        return None
    total_heat = 0.0
    topic_count = 0
    for record in archive.values():
        if not isinstance(record, dict):
            continue
        hot_values = record.get("hot_values") or {}
        if isinstance(hot_values, dict) and hot_values:
            latest_key = sorted(hot_values.keys())[-1]
            try:
                total_heat += float(hot_values.get(latest_key) or 0.0)
            except (TypeError, ValueError):
                pass
        topic_count += 1
    return {"date": date_str, "total_heat": total_heat, "topic_count": topic_count}


def _collect_daily_heat(limit: int) -> Dict[str, Any]:
    limit = max(1, min(limit, MAX_LIMIT))
    summaries: List[Dict[str, Any]] = []
    today = datetime.now(tz=CHINA_TZ).date()
    lookback_days = max(90, limit * 3)
    for offset in range(lookback_days):
        if len(summaries) >= limit:
            break
        date_str = (today - timedelta(days=offset)).strftime("%Y-%m-%d")
        summary = _summarize_day_heat(date_str)
        if summary:
            summaries.append(summary)
    summaries.reverse()
    return {
        "generated_at": datetime.now(tz=CHINA_TZ).isoformat(timespec="seconds"),
        "data": summaries,
    }

OPENAPI_SPEC: Dict[str, Any] = {
    "openapi": "3.0.3",
    "info": {
        "title": "Weibo 热门话题接口",
        "version": "1.0.0",
        "description": (
            "基于 spider 目录内的爬虫与归档逻辑，为下游系统提供热门话题、帖子快照、AI Card 和日汇总等数据接口。"
        ),
        "contact": {"name": "Spider Service"},
    },
    "servers": [{"url": "/"}],
    "paths": {
        "/api/hot_topics/daily_heat": {
            "get": {
                "summary": "获取日热度汇总",
                "description": "返回最近若干天的热度聚合数据，可选择触发重新汇总。",
                "parameters": [
                    {
                        "name": "limit",
                        "in": "query",
                        "description": f"限定返回的天数，默认 {DEFAULT_LIMIT}，最大 {MAX_LIMIT}。",
                        "schema": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT},
                    },
                ],
                "responses": {
                    "200": {
                        "description": "日热度数据列表",
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/DailyHeatResponse"}}
                        },
                    }
                },
            }
        },
        "/api/hot_topics/hourly": {
            "get": {
                "summary": "获取指定小时热门话题",
                "description": "根据日期与小时返回归档话题列表，未指定则回退到最新归档。",
                "parameters": [
                    {
                        "name": "date",
                        "in": "query",
                        "required": False,
                        "description": "YYYY-MM-DD 格式的日期，缺省时使用最近日期。",
                        "schema": {"type": "string", "format": "date"},
                    },
                    {
                        "name": "hour",
                        "in": "query",
                        "required": False,
                        "description": "0-23 之间的小时，缺省时使用最近小时。",
                        "schema": {"type": "integer", "minimum": 0, "maximum": 23},
                    },
                    {
                        "name": "limit",
                        "in": "query",
                        "required": False,
                        "description": f"限制返回的话题数量，最大 {MAX_HOURLY_LIMIT}。",
                        "schema": {"type": "integer", "minimum": 1, "maximum": MAX_HOURLY_LIMIT},
                    },
                ],
                "responses": {
                    "200": {
                        "description": "小时级话题列表",
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/HourlyTopicsResponse"}}
                        },
                    },
                    "404": {
                        "description": "未找到对应时间段的数据",
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}
                        },
                    },
                },
            }
        },
        "/api/hot_topics/posts": {
            "get": {
                "summary": "获取话题帖子快照",
                "description": "根据归档记录刷新或返回指定话题的帖子数据，可选择触发重新抓取。",
                "parameters": [
                    {
                        "name": "date",
                        "in": "query",
                        "required": False,
                        "description": "YYYY-MM-DD 格式的归档日期。",
                        "schema": {"type": "string", "format": "date"},
                    },
                    {
                        "name": "title",
                        "in": "query",
                        "required": False,
                        "description": "话题标题，可与 slug 二选一提供。",
                        "schema": {"type": "string"},
                    },
                    {
                        "name": "slug",
                        "in": "query",
                        "required": False,
                        "description": "话题对应的 slug，可与 title 二选一提供。",
                        "schema": {"type": "string"},
                    },
                    {
                        "name": "limit",
                        "in": "query",
                        "required": False,
                        "description": f"限制返回的帖子数量，最大 {MAX_POST_LIMIT}。",
                        "schema": {"type": "integer", "minimum": 1, "maximum": MAX_POST_LIMIT},
                    },
                    {
                        "name": "refresh",
                        "in": "query",
                        "required": False,
                        "description": "为 1/true/yes 时会强制触发抓取并覆盖归档。",
                        "schema": {"type": "string"},
                    },
                ],
                "responses": {
                    "200": {
                        "description": "帖子快照与元数据",
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/TopicPostsResponse"}}
                        },
                    },
                    "400": {
                        "description": "参数缺失或无效",
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}
                        },
                    },
                    "404": {
                        "description": "未找到匹配的话题归档",
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}
                        },
                    },
                },
            }
        },
        "/api/hot_topics/aicard": {
            "get": {
                "summary": "获取 AI Card 快照",
                "description": "返回指定话题的 AI Card Markdown/HTML 以及相关元数据，必要时会补全本地快照。",
                "parameters": [
                    {
                        "name": "date",
                        "in": "query",
                        "required": False,
                        "description": "YYYY-MM-DD 格式的归档日期，用于定位快照目录。",
                        "schema": {"type": "string", "format": "date"},
                    },
                    {
                        "name": "hour",
                        "in": "query",
                        "required": False,
                        "description": "0-23 之间的小时，缺省时会尝试从归档记录推断。",
                        "schema": {"type": "integer", "minimum": 0, "maximum": 23},
                    },
                    {
                        "name": "title",
                        "in": "query",
                        "required": False,
                        "description": "话题标题，可与 slug 二选一提供。",
                        "schema": {"type": "string"},
                    },
                    {
                        "name": "slug",
                        "in": "query",
                        "required": False,
                        "description": "话题 slug，可与 title 二选一提供。",
                        "schema": {"type": "string"},
                    },
                ],
                "responses": {
                    "200": {
                        "description": "AI Card Markdown 与资源信息",
                        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/AiCardResponse"}}},
                    },
                    "400": {
                        "description": "参数缺失或无效",
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}
                        },
                    },
                    "404": {
                        "description": "未找到 AI Card 资源",
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}
                        },
                    },
                },
            }
        },
        "/api/hot_topics/daily_bundle": {
            "get": {
                "summary": "获取日汇总包",
                "description": "加载或动态生成包含小时榜与帖子快照的综合 JSON 包。",
                "parameters": [
                    {
                        "name": "date",
                        "in": "query",
                        "required": True,
                        "description": "YYYY-MM-DD 格式的日期。",
                        "schema": {"type": "string", "format": "date"},
                    },
                    {
                        "name": "include_posts",
                        "in": "query",
                        "required": False,
                        "description": "为 false/0/no 时只返回基础榜单，不携带帖子列表。",
                        "schema": {"type": "string"},
                    },
                ],
                "responses": {
                    "200": {
                        "description": "日汇总包数据",
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/DailyBundleResponse"}}
                        },
                    },
                    "400": {
                        "description": "日期参数缺失或格式错误",
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}
                        },
                    },
                    "404": {
                        "description": "未生成对应的汇总代码",
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}
                        },
                    },
                },
            }
        },
    },
    "components": {
        "schemas": {
            "ErrorResponse": {
                "type": "object",
                "properties": {"error": {"type": "string"}},
                "required": ["error"],
            },
            "DailyHeatEntry": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "format": "date"},
                    "total_heat": {"type": "integer"},
                    "topic_count": {"type": "integer"},
                    "top_topics": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string"},
                                "slug": {"type": "string"},
                                "heat": {"type": "integer"},
                                "rank": {"type": "integer"},
                            },
                        },
                    },
                },
            },
            "DailyHeatResponse": {
                "type": "object",
                "properties": {
                    "generated_at": {"type": "string", "format": "date-time"},
                    "requested_limit": {"type": "integer"},
                    "available_days": {"type": "integer"},
                    "data": {"type": "array", "items": {"$ref": "#/components/schemas/DailyHeatEntry"}},
                },
            },
            "HourlyTopic": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "slug": {"type": "string"},
                    "heat": {"type": "integer"},
                    "rank": {"type": "integer"},
                    "first_seen": {"type": "string", "format": "date-time"},
                    "last_seen": {"type": "string", "format": "date-time"},
                    "url": {"type": "string"},
                },
            },
            "HourlyTopicsResponse": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "format": "date"},
                    "hour": {"type": "integer"},
                    "limit": {"type": "integer"},
                    "topics": {"type": "array", "items": {"$ref": "#/components/schemas/HourlyTopic"}},
                    "source_path": {"type": "string"},
                },
            },
            "TopicPost": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "bid": {"type": "string"},
                    "url": {"type": "string"},
                    "created_at": {"type": "string"},
                    "user_name": {"type": "string"},
                    "text": {"type": "string"},
                    "reposts": {"type": "integer"},
                    "comments": {"type": "integer"},
                    "likes": {"type": "integer"},
                    "pics": {"type": "array", "items": {"type": "object"}},
                    "video": {"type": "object"},
                    "score": {"type": "number"},
                },
            },
            "TopicPostsResponse": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "format": "date"},
                    "title": {"type": "string"},
                    "slug": {"type": "string"},
                    "limit": {"type": "integer"},
                    "items": {"type": "array", "items": {"$ref": "#/components/schemas/TopicPost"}},
                    "source_path": {"type": "string"},
                },
            },
            "AiCardMedia": {
                "type": "object",
                "properties": {
                    "secure_url": {"type": "string"},
                    "alt": {"type": "string"},
                    "type": {"type": "string"},
                },
            },
            "AiCardResponse": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "format": "date"},
                    "hour": {"type": "integer"},
                    "slug": {"type": "string"},
                    "title": {"type": "string"},
                    "markdown": {"type": "string", "description": "Markdown 内容"},
                    "markdown_path": {"type": "string", "description": "Markdown 文件路径"},
                    "html": {"type": "string"},
                    "html_path": {"type": "string"},
                    "links": {"type": "array", "items": {"type": "string"}},
                    "media": {"type": "array", "items": {"$ref": "#/components/schemas/AiCardMedia"}},
                    "meta": {"type": "object"},
                    "fetched_at": {"type": "string"},
                },
            },
            "DailyBundleResponse": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "format": "date"},
                    "include_posts": {"type": "boolean"},
                    "source": {"type": "string"},
                    "total": {"type": "integer"},
                    "data": {"type": "array", "items": {"type": "object"}},
                },
            },
        }
    },
}

SWAGGER_UI_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8" />
    <title>Weibo 热门话题接口文档</title>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css" />
    <style>
      body { margin: 0; background-color: #f7f7f7; }
      #swagger-ui { margin: 0 auto; }
    </style>
  </head>
  <body>
    <div id="swagger-ui"></div>
    <script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
    <script>
      window.onload = () => {
        SwaggerUIBundle({
          url: "/api/docs/swagger.json",
          dom_id: "#swagger-ui",
          presets: [SwaggerUIBundle.presets.apis],
          layout: "BaseLayout",
          docExpansion: "none"
        });
      };
    </script>
  </body>
</html>
"""


def _resolve_limit(raw_value: Optional[str]) -> int:
    if raw_value is None:
        return DEFAULT_LIMIT
    try:
        limit = int(raw_value)
    except ValueError:
        return DEFAULT_LIMIT
    return max(1, min(limit, MAX_LIMIT))


@bp.get("/api/docs/swagger.json")
def swagger_spec() -> Any:
    return jsonify(OPENAPI_SPEC)


@bp.get("/api/docs")
def swagger_ui() -> Response:
    return Response(SWAGGER_UI_HTML, mimetype="text/html")


@bp.get("/api/hot_topics/daily_heat")
def daily_heat() -> Any:
    limit = _resolve_limit(request.args.get("limit"))
    payload = _collect_daily_heat(limit)
    payload["requested_limit"] = limit
    payload["available_days"] = len(payload.get("data", []))
    return jsonify(payload)


def _parse_hour(raw_hour: Optional[str]) -> Optional[int]:
    if raw_hour is None:
        return None
    try:
        hour = int(raw_hour)
    except ValueError:
        raise ValueError("hour must be an integer between 0 and 23") from None
    if hour < 0 or hour > 23:
        raise ValueError("hour must be an integer between 0 and 23")
    return hour


def _resolve_positive_limit(raw_value: Optional[str], *, maximum: int) -> Optional[int]:
    if raw_value is None:
        return None
    try:
        value = int(raw_value)
    except ValueError:
        return None
    value = max(1, value)
    if maximum is not None:
        value = min(value, maximum)
    return value


def _resolve_boolean(raw_value: Optional[str], default: bool = True) -> bool:
    if raw_value is None:
        return default
    lowered = raw_value.strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    return default


def _normalize_title(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if text.startswith("#") and text.endswith("#") and len(text) > 2:
        text = text[1:-1]
    text = text.strip()
    if not text:
        return None
    return re.sub(r"\s+", " ", text)


def _load_json(path: Path) -> Optional[Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        logging.error("Failed to read %s: %s", path, exc)
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        logging.error("Invalid JSON in %s: %s", path, exc)
        return None


def _coerce_topic_list(data: Any) -> List[Dict[str, Any]]:
    if isinstance(data, list):
        return [dict(item) for item in data if isinstance(item, dict)]
    return []


def _read_text_file(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        logging.error("Failed to read text file %s: %s", path, exc)
        return None


def _format_post_timestamp(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    local_now = datetime.now(tz=CHINA_TZ)
    dt: Optional[datetime] = None

    if text in {"刚刚", "刚才"}:
        dt = local_now

    if dt is None:
        match = re.fullmatch(r"(\d+)\s*分钟前", text)
        if match:
            dt = local_now - timedelta(minutes=int(match.group(1)))

    if dt is None:
        match = re.fullmatch(r"(\d+)\s*小时前", text)
        if match:
            dt = local_now - timedelta(hours=int(match.group(1)))

    if dt is None:
        match = re.fullmatch(r"(\d+)\s*天前", text)
        if match:
            dt = local_now - timedelta(days=int(match.group(1)))

    if dt is None:
        match = re.fullmatch(r"今天\s*(\d{1,2}):(\d{2})", text)
        if match:
            hour, minute = int(match.group(1)), int(match.group(2))
            dt = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)

    if dt is None:
        match = re.fullmatch(r"昨天\s*(\d{1,2}):(\d{2})", text)
        if match:
            hour, minute = int(match.group(1)), int(match.group(2))
            dt = (local_now - timedelta(days=1)).replace(hour=hour, minute=minute, second=0, microsecond=0)

    if dt is None:
        match = re.fullmatch(r"(\d{1,2})月(\d{1,2})日\s*(\d{1,2}):(\d{2})", text)
        if match:
            month = int(match.group(1))
            day = int(match.group(2))
            hour = int(match.group(3))
            minute = int(match.group(4))
            year = local_now.year
            candidate = datetime(year, month, day, hour, minute, tzinfo=CHINA_TZ)
            if candidate - local_now > timedelta(days=1):
                candidate = candidate.replace(year=year - 1)
            dt = candidate

    normalized = text.replace("Z", "+00:00")
    if dt is None:
        try:
            dt = datetime.fromisoformat(normalized)
        except ValueError:
            dt = None
    if dt is None:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M"):
            try:
                dt = datetime.strptime(normalized, fmt)
                break
            except ValueError:
                continue
    if dt is None:
        return text
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CHINA_TZ)
    else:
        dt = dt.astimezone(CHINA_TZ)
    return dt.strftime("%y-%m-%d %H:%M")


def _rewrite_post_media(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Proxy media URLs so the frontend only sees our own host."""
    copy = dict(entry)
    if "pics" in copy:
        copy["pics"] = attach_proxy_to_media(copy.get("pics"))
    if "video" in copy:
        copy["video"] = attach_proxy_to_media(copy.get("video"))
    if "media" in copy:
        copy["media"] = attach_proxy_to_media(copy.get("media"))
    return copy


def _load_post_payload(date: str, slug: str) -> Tuple[Optional[Dict[str, Any]], Optional[Path]]:
    path = POSTS_DIR / date / f"{slug}.json"
    if not path.exists():
        return None, None
    data = _load_json(path)
    if data is None:
        return None, None
    return data, path


def _ensure_posts_exist(date: str, title: str, archive: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    record = archive.get(title)
    if not record:
        logging.info("Archive for %s missing topic %s", date, title)
        return None
    try:
        updated_record = ensure_topic_posts(title, record, date)
    except Exception as exc:  # pylint: disable=broad-except
        logging.error("ensure_topic_posts failed for %s (%s): %s", title, date, exc)
        return None
    archive[title] = updated_record
    try:
        save_archive(date, archive)
    except Exception as exc:  # pylint: disable=broad-except
        logging.error("Failed to save archive for %s: %s", date, exc)
    return updated_record


def _locate_archive_record_by_slug(
    archive: Dict[str, Dict[str, Any]], slug: str, fallback_title: Optional[str]
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    if fallback_title and fallback_title in archive:
        return fallback_title, archive[fallback_title]
    for title, record in archive.items():
        if record.get("slug") == slug:
            return title, record
    return None, None


def _locate_archive_record_by_title(
    archive: Dict[str, Dict[str, Any]], title: str
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    if title in archive:
        return title, archive[title]
    normalized = _normalize_title(title)
    if not normalized:
        return None, None
    for key, record in archive.items():
        if _normalize_title(key) == normalized:
            return key, record
    return None, None


def _derive_hour_from_record(record: Optional[Dict[str, Any]]) -> Optional[int]:
    if not record:
        return None
    hours = record.get("appeared_hours")
    if isinstance(hours, list):
        for raw in reversed(hours):
            try:
                return int(raw)
            except (TypeError, ValueError):
                continue
    first_seen = record.get("first_seen")
    if isinstance(first_seen, str) and len(first_seen) >= 13:
        try:
            return int(first_seen[11:13])
        except ValueError:
            return None
    return None


def _within_retention(date_str: str, days: int) -> bool:
    try:
        target = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return False
    if days <= 0:
        return False
    age = (datetime.now(tz=CHINA_TZ).date() - target).days
    return age <= (days - 1)


HOT_TOPICS_REPO = HotTopicsRepository(HOURLY_DIR)


@bp.get("/api/hot_topics/hourly")
def hourly_topics() -> Any:
    date = request.args.get("date")
    raw_hour = request.args.get("hour")
    raw_limit = request.args.get("limit")

    try:
        hour = _parse_hour(raw_hour)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    limit = _resolve_positive_limit(raw_limit, maximum=MAX_HOURLY_LIMIT)
    snapshot = HOT_TOPICS_REPO.get_snapshot(date=date, hour=hour)
    if not snapshot:
        return jsonify({"error": "Hourly snapshot not available"}), 404

    topics = snapshot.topics[:limit] if limit is not None else snapshot.topics
    response = {
        "date": snapshot.ref.date,
        "hour": snapshot.ref.hour,
        "generated_at": snapshot.generated_at.isoformat(timespec="seconds"),
        "total": len(snapshot.topics),
        "topics": topics,
        "source_path": snapshot.ref.path.as_posix(),
    }
    if limit is not None:
        response["requested_limit"] = limit
    return jsonify(response)


@bp.get("/api/hot_topics/posts")
def topic_posts() -> Any:
    date = request.args.get("date")
    raw_hour = request.args.get("hour")
    slug = request.args.get("slug")
    title = request.args.get("title")
    raw_rank = request.args.get("rank")
    raw_limit = request.args.get("limit")

    try:
        hour = _parse_hour(raw_hour)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    limit = _resolve_positive_limit(raw_limit, maximum=MAX_POST_LIMIT)

    snapshot: Optional[HotTopicsSnapshot] = None
    if date or hour is not None:
        snapshot = HOT_TOPICS_REPO.get_snapshot(date=date, hour=hour)
    elif not slug and not title:
        snapshot = HOT_TOPICS_REPO.get_snapshot(date=None, hour=None)

    if not date and snapshot:
        date = snapshot.ref.date

    if not date:
        return jsonify({"error": "date is required when no hourly snapshot is available"}), 400

    if slug:
        slug = slug.strip()
    if title:
        title = title.strip()
        if not slug:
            slug = slugify_title(title)

    if raw_rank and not slug:
        try:
            rank = int(raw_rank)
        except ValueError:
            return jsonify({"error": "rank must be an integer"}), 400
        if rank <= 0:
            return jsonify({"error": "rank must be >= 1"}), 400
        if snapshot is None:
            snapshot = HOT_TOPICS_REPO.get_snapshot(date=date, hour=hour)
        if snapshot is None:
            return jsonify({"error": "Unable to resolve snapshot for supplied rank"}), 404
        if rank > len(snapshot.topics):
            return jsonify({"error": "rank exceeds available topics"}), 400
        topic_entry = snapshot.topics[rank - 1]
        title = topic_entry.get("title") or title
        slug = slugify_title(title or "")
        if hour is None:
            hour = snapshot.ref.hour

    if not slug:
        return jsonify({"error": "slug, title, or rank must be provided"}), 400

    payload, source_path = _load_post_payload(date, slug)
    archive: Optional[Dict[str, Dict[str, Any]]] = None
    record: Optional[Dict[str, Any]] = None

    if payload is None:
        try:
            archive = load_archive(date)
        except FileNotFoundError:
            archive = {}

        if slug and archive:
            title, record = _locate_archive_record_by_slug(archive, slug, title)

        if (not title or record is None) and slug and snapshot:
            for topic_entry in snapshot.topics:
                topic_title = topic_entry.get("title") or ""
                if slugify_title(topic_title) == slug:
                    title = title or topic_title
                    record = record or archive.get(topic_title)
                    break

        if title and archive is not None:
            record = record or {"slug": slug or slugify_title(title)}
            archive[title] = record
            refreshed = _ensure_posts_exist(date, title, archive)
            if refreshed:
                record = refreshed
                slug = record.get("slug") or slug
                payload, source_path = _load_post_payload(date, slug)
                if payload is None:
                    logging.warning("Post payload missing after refresh for %s (%s)", title, date)
        if payload is None:
            return jsonify({"error": "Topic posts not available"}), 404
    else:
        if not title:
            if archive is None:
                try:
                    archive = load_archive(date)
                except FileNotFoundError:
                    archive = {}
            if archive:
                title, _ = _locate_archive_record_by_slug(archive, slug, title)

    items_raw = payload.get("items")
    if not isinstance(items_raw, list):
        items_raw = []

    items: List[Dict[str, Any]] = []
    for entry in items_raw:
        if not isinstance(entry, dict):
            continue
        copy = _rewrite_post_media(entry)
        original_ts = copy.get("created_at") or copy.get("timestamp")
        formatted_ts = _format_post_timestamp(original_ts)
        if formatted_ts:
            copy["created_at"] = formatted_ts
        items.append(copy)

    response = {
        "date": date,
        "slug": slug,
        "title": title or payload.get("topic"),
        "fetched_at": payload.get("fetched_at"),
        "total": payload.get("total", len(items)),
        "items": items[:limit] if limit is not None else items,
        "source_path": source_path.as_posix() if source_path else None,
    }
    if limit is not None:
        response["requested_limit"] = limit
    return jsonify(response)


@bp.get("/api/hot_topics/aicard")
def topic_aicard() -> Any:
    date = request.args.get("date")
    raw_hour = request.args.get("hour")
    slug = request.args.get("slug")
    title = request.args.get("title")
    raw_rank = request.args.get("rank")
    requested_date = date
    requested_hour = raw_hour
    requested_slug = slug
    requested_title = title
    requested_rank = raw_rank
    resolved_by = None
    resolved_rank = None
    hour_provided = raw_hour is not None

    try:
        hour = _parse_hour(raw_hour)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    topic_snapshot: Optional[HotTopicsSnapshot] = None
    if raw_rank or not slug or not title or date is None or hour is None:
        topic_snapshot = HOT_TOPICS_REPO.get_snapshot(date=date, hour=hour)
        if topic_snapshot:
            if date is None:
                date = topic_snapshot.ref.date
            if hour is None:
                hour = topic_snapshot.ref.hour

    if not date:
        return jsonify({"error": "date is required or could not be inferred"}), 400

    if title:
        title = title.strip()
        if not title:
            title = None
        elif not slug:
            slug = slugify_title(title)

    if slug:
        slug = slug.strip()
        if not slug:
            slug = None

    # Keep API behavior aligned with /api/hot_topics/posts:
    # use rank as a fallback locator, not as an unconditional override.
    if raw_rank and not slug and not title:
        try:
            rank = int(raw_rank)
        except ValueError:
            return jsonify({"error": "rank must be an integer"}), 400
        if rank <= 0:
            return jsonify({"error": "rank must be >= 1"}), 400
        if topic_snapshot is None:
            topic_snapshot = HOT_TOPICS_REPO.get_snapshot(date=date, hour=hour)
        if topic_snapshot is None:
            return jsonify({"error": "Unable to resolve snapshot for supplied rank"}), 404
        if rank > len(topic_snapshot.topics):
            return jsonify({"error": "rank exceeds available topics"}), 400
        topic_entry = topic_snapshot.topics[rank - 1]
        resolved_title = (topic_entry.get("title") or "").strip()
        if not resolved_title:
            return jsonify({"error": "Unable to resolve topic title from rank"}), 404
        title = resolved_title
        entry_slug = (topic_entry.get("slug") or "").strip()
        slug = entry_slug or slugify_title(title)
        if hour is None:
            hour = topic_snapshot.ref.hour
        resolved_by = "rank"
        resolved_rank = rank
    else:
        if slug:
            resolved_by = resolved_by or "slug"
        if not resolved_by and title:
            resolved_by = "title"

    if topic_snapshot and not title and slug:
        for topic_entry in topic_snapshot.topics:
            topic_title = topic_entry.get("title") or ""
            if slugify_title(topic_title) == slug:
                title = topic_title
                if hour is None:
                    hour = topic_snapshot.ref.hour
                resolved_by = resolved_by or "slug"
                break

    archive: Optional[Dict[str, Dict[str, Any]]] = None
    record: Optional[Dict[str, Any]] = None

    if (not title or hour is None) and date:
        try:
            archive = load_archive(date)
        except FileNotFoundError:
            archive = {}
        if slug and archive:
            title, record = _locate_archive_record_by_slug(archive, slug, title)
        if title and not record and archive:
            title, record = _locate_archive_record_by_title(archive, title)
        if record and record.get("slug") and not slug:
            slug = record.get("slug") or slug
        if not title and archive:
            record = None
        if record and hour is None:
            hour = _derive_hour_from_record(record)

    # 即便带了 hour，也尝试加载归档，避免 record 为空导致后续 500
    if record is None and date:
        try:
            archive = archive or load_archive(date)
        except FileNotFoundError:
            archive = {}
        if slug and archive:
            title, record = _locate_archive_record_by_slug(archive, slug, title)
        if record is None and title and archive:
            title, record = _locate_archive_record_by_title(archive, title)
        if record and record.get("slug") and not slug:
            slug = record.get("slug") or slug

    record = record or {}

    if not title or not slug:
        return jsonify({"error": "Unable to resolve topic title or slug"}), 404

    if hour is None:
        return jsonify({"error": "hour must be provided or derivable from data"}), 400

    hour_key = f"{hour:02d}"
    aicard_info = record.get("aicard") or {}
    hours_map = aicard_info.get("hours", {})
    aicard_snapshot = hours_map.get(hour_key)
    if not aicard_snapshot and not hour_provided:
        aicard_snapshot = aicard_info.get("latest")

    if not aicard_snapshot:
        if not _within_retention(date, AICARD_RETENTION_DAYS):
            return jsonify({"error": "AI card not retained for this date"}), 404
        try:
            aicard_snapshot = ensure_aicard_snapshot(title, date, hour, slug=slug)
        except AICardCooldownError as exc:
            return jsonify({
                "error": "ai_card_cooldown",
                "message": str(exc),
                "retry_after": exc.retry_after,
                "level": exc.level,
            }), 429
        except AICardRateLimitError as exc:
            return jsonify({
                "error": "ai_card_rate_limited",
                "message": str(exc),
            }), 429
        if aicard_snapshot:
            hours_map[hour_key] = aicard_snapshot
            aicard_info["hours"] = hours_map
            aicard_info["latest"] = aicard_snapshot
            record["aicard"] = aicard_info
            if archive is not None and title:
                archive[title] = record
                save_archive(date, archive)
    if not aicard_snapshot:
        return jsonify({"error": "AI card not available"}), 404

    expected_slug = slug
    snapshot_slug = (aicard_snapshot.get("slug") or "").strip()
    if expected_slug and snapshot_slug and snapshot_slug != expected_slug:
        if not _within_retention(date, AICARD_RETENTION_DAYS):
            return jsonify({
                "error": "ai_card_slug_mismatch",
                "message": "AI card slug does not match requested slug",
                "expected_slug": expected_slug,
                "snapshot_slug": snapshot_slug,
            }), 409
        try:
            refreshed_snapshot = ensure_aicard_snapshot(title, date, hour, slug=expected_slug)
        except AICardCooldownError as exc:
            return jsonify({
                "error": "ai_card_cooldown",
                "message": str(exc),
                "retry_after": exc.retry_after,
                "level": exc.level,
            }), 429
        except AICardRateLimitError as exc:
            return jsonify({
                "error": "ai_card_rate_limited",
                "message": str(exc),
            }), 429
        if refreshed_snapshot:
            aicard_snapshot = refreshed_snapshot
            hours_map[hour_key] = aicard_snapshot
            aicard_info["hours"] = hours_map
            aicard_info["latest"] = aicard_snapshot
            record["aicard"] = aicard_info
            if archive is not None and title:
                archive[title] = record
                save_archive(date, archive)
        else:
            return jsonify({
                "error": "ai_card_slug_mismatch",
                "message": "AI card slug does not match requested slug",
                "expected_slug": expected_slug,
                "snapshot_slug": snapshot_slug,
            }), 409

    markdown_rel = aicard_snapshot.get("markdown_path") or aicard_info.get("markdown")
    markdown_abs = from_data_relative(markdown_rel) if markdown_rel else None
    markdown_content = _read_text_file(markdown_abs) if markdown_abs else None
    markdown_content = rewrite_markdown_images(markdown_content)
    markdown_content = rewrite_text_media_urls(markdown_content)

    raw_html_value = (
        aicard_snapshot.get("html_path")
        or aicard_snapshot.get("html")
        or aicard_info.get("html_path")
        or aicard_info.get("html")
    )
    html_path: Optional[str] = None
    html_content: Optional[str] = None
    if isinstance(raw_html_value, str) and raw_html_value.strip():
        stripped = raw_html_value.lstrip()
        if stripped.startswith("<"):
            html_content = raw_html_value
        else:
            html_path = raw_html_value
            html_abs = from_data_relative(html_path)
            html_content = _read_text_file(html_abs)
    html_content = rewrite_html_images(html_content)
    html_content = rewrite_text_media_urls(html_content)

    media_payload = attach_proxy_to_media(aicard_snapshot.get("media"))

    response: Dict[str, Any] = {
        "date": date,
        "hour": hour,
        "slug": aicard_snapshot.get("slug") or slug,
        "title": title,
        "markdown_path": markdown_rel,
        "markdown": markdown_content,
        "html_path": html_path,
        "html": html_content,
        "meta": aicard_snapshot.get("meta"),
        "links": aicard_snapshot.get("links"),
        "media": media_payload,
        "fetched_at": aicard_snapshot.get("fetched_at"),
        "resolved_rank": resolved_rank,
        "requested_date": requested_date,
        "requested_hour": requested_hour,
        "requested_rank": requested_rank,
        "requested_title": requested_title,
        "requested_slug": requested_slug,
    }
    if resolved_by:
        response["resolved_by"] = resolved_by

    if record:
        response["first_seen"] = record.get("first_seen")
        response["last_seen"] = record.get("last_seen")

    return jsonify(response)


@bp.get("/api/hot_topics/daily_bundle")
def daily_bundle() -> Any:
    date = request.args.get("date")
    if not date:
        return jsonify({"error": "date is required"}), 400
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "date must be formatted as YYYY-MM-DD"}), 400

    include_posts = _resolve_boolean(request.args.get("include_posts"), True)
    try:
        archive = load_archive(date)
    except FileNotFoundError:
        return jsonify({"error": "archive not found for date"}), 404

    topics: List[Dict[str, Any]] = []
    for title, record in archive.items():
        if not isinstance(record, dict):
            continue
        topic_entry = dict(record)
        slug = record.get("slug") or slugify_title(title)
        if include_posts:
            posts_payload, _ = _load_post_payload(date, slug)
            if posts_payload is None:
                refreshed = _ensure_posts_exist(date, title, archive)
                posts_payload = (refreshed or {}).get("latest_posts")
            topic_entry["latest_posts"] = posts_payload or {}
        else:
            topic_entry.pop("latest_posts", None)
        topics.append(topic_entry)

    response = {
        "date": date,
        "include_posts": include_posts,
        "source": "archive",
        "total": len(topics),
        "data": topics,
    }
    return jsonify(response)


def main() -> None:
    logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s [%(levelname)s] %(message)s")
    host = get_env_str("WEIBO_API_HOST", "0.0.0.0") or "0.0.0.0"
    port = get_env_int("WEIBO_API_PORT", 8767) or 8767
    app = Flask(__name__)
    app.register_blueprint(bp)
    app.run(host=host, port=port)


if __name__ == "__main__":
    main()
