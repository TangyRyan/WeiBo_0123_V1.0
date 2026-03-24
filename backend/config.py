"""Backend-wide configuration derived from shared settings."""

from __future__ import annotations

import os
from pathlib import Path

from .settings import DATA_ROOT, get_env_bool, get_env_int, get_env_str


def _shift_time(value: str, minutes: int, *, fallback: str = "11:00") -> str:
    try:
        parts = value.split(":")
        if len(parts) != 2:
            return fallback
        hour = int(parts[0])
        minute = int(parts[1])
        total = (hour * 60 + minute + minutes) % (24 * 60)
        return f"{total // 60:02d}:{total % 60:02d}"
    except Exception:
        return fallback

# 路径统一：与 spider/daily_heat.py 中保持一致
ARCHIVE_DIR = DATA_ROOT / "hot_topics"
HOURLY_DIR = ARCHIVE_DIR / "hourly"
POST_DIR = DATA_ROOT / "posts"
AICARD_DIR = DATA_ROOT / "aicard"
RISK_DIR = DATA_ROOT / "risk_warnings"
HOTLIST_DIR = HOURLY_DIR  # 兼容旧接口，指向小时榜路径

for _path in [
    ARCHIVE_DIR,
    HOURLY_DIR,
    POST_DIR,
    AICARD_DIR,
    RISK_DIR,
]:
    _path.mkdir(parents=True, exist_ok=True)

# 外部 GitHub 数据源
GITHUB_RAW_BASE = (
    "https://raw.githubusercontent.com/lxw15337674/weibo-trending-hot-history/refs/heads/master/api"
)

# 调度配置
HOUR_CHECK_INTERVAL_MINUTES = get_env_int("HOUR_CHECK_INTERVAL_MINUTES", 5) or 5
DAILY_LLM_TIME = get_env_str("DAILY_LLM_TIME", "09:30") or "09:30"
DAILY_TOTALS_TIME = get_env_str("DAILY_TOTALS_TIME", _shift_time(DAILY_LLM_TIME, 15)) or _shift_time(
    DAILY_LLM_TIME, 15
)
SLIM_RETENTION_DAYS = max(1, get_env_int("WEIBO_SLIM_RETENTION_DAYS", 7) or 7)
DELETE_RETENTION_DAYS = max(
    SLIM_RETENTION_DAYS,
    get_env_int("WEIBO_DELETE_RETENTION_DAYS", 90) or 90,
)
SLIM_TIME = get_env_str("WEIBO_SLIM_TIME", _shift_time(DAILY_LLM_TIME, 90)) or _shift_time(
    DAILY_LLM_TIME, 90
)
HOURLY_CLEAN_TIME = get_env_str(
    "WEIBO_HOURLY_CLEAN_TIME",
    _shift_time(DAILY_LLM_TIME, 60, fallback="00:30"),
) or _shift_time(DAILY_LLM_TIME, 60, fallback="00:30")
MONITOR_ENABLED = get_env_bool("WEIBO_MONITOR_ENABLED", True)
_MONITOR_CRON_RAW = get_env_str("WEIBO_MONITOR_CRON", "") or ""
MONITOR_CRON = _MONITOR_CRON_RAW.strip() or None
LLM_ENABLED = get_env_bool("WEIBO_LLM_ENABLED", True)
LLM_ANALYSIS_WORKERS = max(1, get_env_int("LLM_ANALYSIS_WORKERS", 3) or 3)
LLM_ANALYSIS_TOP_K = max(1, get_env_int("LLM_ANALYSIS_TOP_K", 50) or 50)
HEALTH_TOPIC_ENABLED = get_env_bool("HEALTH_TOPIC_ENABLED", True)
HEALTH_TOPIC_INTERVAL_MINUTES = max(5, get_env_int("HEALTH_TOPIC_INTERVAL_MINUTES", 10) or 10)
# HEALTH_TOPIC_WINDOW_HOURS = max(1, get_env_int("HEALTH_TOPIC_WINDOW_HOURS", 48) or 48)

HEALTH_TOPIC_WINDOW_HOURS = max(1, get_env_int("HEALTH_TOPIC_WINDOW_HOURS", 240) or 240)

# 存储保留窗口（天）
FULL_RETENTION_DAYS = max(1, get_env_int("WEIBO_FULL_RETENTION_DAYS", 14) or 14)
LIGHT_RETENTION_DAYS = max(FULL_RETENTION_DAYS, get_env_int("WEIBO_LIGHT_RETENTION_DAYS", 90) or 90)
POST_RETENTION_DAYS = max(1, get_env_int("WEIBO_POST_RETENTION_DAYS", FULL_RETENTION_DAYS) or FULL_RETENTION_DAYS)
HOURLY_RETENTION_DAYS = max(1, get_env_int("WEIBO_HOURLY_RETENTION_DAYS", 1) or 1)
AICARD_RETENTION_DAYS = max(1, get_env_int("WEIBO_AICARD_RETENTION_DAYS", FULL_RETENTION_DAYS) or FULL_RETENTION_DAYS)
HEALTH_RETENTION_DAYS = max(1, get_env_int("WEIBO_HEALTH_RETENTION_DAYS", 30) or 30)
KNOWN_IDS_MAX = max(0, get_env_int("WEIBO_KNOWN_IDS_MAX", 200) or 200)
SLIM_POSTS_PER_EVENT = max(0, get_env_int("WEIBO_SLIM_POSTS_PER_EVENT", 4) or 4)
SLIM_POST_TEXT_LIMIT = max(0, get_env_int("WEIBO_SLIM_POST_TEXT_LIMIT", 500) or 500)

# 大模型
OPENAI_API_KEY = get_env_str("OPENAI_API_KEY", "")
OPENAI_MODEL = get_env_str("OPENAI_MODEL", "deepseek-r1-250120")
OPENAI_BASE_URL = get_env_str("OPENAI_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")

# 风险评分
RISK_WEIGHTS = {
    "negativity": 0.35,
    "growth": 0.25,
    "sensitivity": 0.20,
    "crowd": 0.20,
}

HIGH_SENSITIVE = {"时政", "社会", "财经", "军事", "教育"}
MEDIUM_SENSITIVE = {"科技", "健康", "文化", "能源", "交通", "农业", "公益"}
LOW_SENSITIVE = {
    "娱乐",
    "房产",
    "时尚",
    "动漫",
    "美食",
    "历史",
    "文学",
    "汽车",
    "旅行",
    "游戏",
    "体育",
    "未知",
}

REGION_LIST = [
    "北京",
    "天津",
    "河北",
    "山西",
    "内蒙古",
    "辽宁",
    "吉林",
    "黑龙江",
    "上海",
    "江苏",
    "浙江",
    "安徽",
    "福建",
    "江西",
    "山东",
    "河南",
    "湖北",
    "湖南",
    "广东",
    "广西",
    "海南",
    "重庆",
    "四川",
    "贵州",
    "云南",
    "西藏",
    "陕西",
    "甘肃",
    "青海",
    "宁夏",
    "新疆",
    "香港",
    "澳门",
    "台湾",
    "国外",
    "未知",
]

ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "*")

__all__ = [
    "ARCHIVE_DIR",
    "HOURLY_DIR",
    "HOTLIST_DIR",
    "POST_DIR",
    "AICARD_DIR",
    "RISK_DIR",
    "GITHUB_RAW_BASE",
    "HOUR_CHECK_INTERVAL_MINUTES",
    "MONITOR_ENABLED",
    "MONITOR_CRON",
    "LLM_ENABLED",
    "LLM_ANALYSIS_WORKERS",
    "LLM_ANALYSIS_TOP_K",
    "HEALTH_TOPIC_ENABLED",
    "HEALTH_TOPIC_INTERVAL_MINUTES",
    "HEALTH_TOPIC_WINDOW_HOURS",
    "DAILY_LLM_TIME",
    "DAILY_TOTALS_TIME",
    "SLIM_RETENTION_DAYS",
    "DELETE_RETENTION_DAYS",
    "SLIM_TIME",
    "HOURLY_CLEAN_TIME",
    "FULL_RETENTION_DAYS",
    "LIGHT_RETENTION_DAYS",
    "POST_RETENTION_DAYS",
    "HOURLY_RETENTION_DAYS",
    "AICARD_RETENTION_DAYS",
    "HEALTH_RETENTION_DAYS",
    "KNOWN_IDS_MAX",
    "SLIM_POSTS_PER_EVENT",
    "SLIM_POST_TEXT_LIMIT",
    "OPENAI_API_KEY",
    "OPENAI_MODEL",
    "OPENAI_BASE_URL",
    "RISK_WEIGHTS",
    "HIGH_SENSITIVE",
    "MEDIUM_SENSITIVE",
    "LOW_SENSITIVE",
    "REGION_LIST",
    "ALLOWED_ORIGINS",
]
