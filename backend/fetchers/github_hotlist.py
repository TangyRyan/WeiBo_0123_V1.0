
# -*- coding: utf-8 -*-
import requests
from typing import Optional
from ..config import GITHUB_RAW_BASE

UA = {"User-Agent": "weibo-monitor/1.0 (+github crawler)"}

#返回字符串
def build_url_for_hour(date_str: str, hour: str) -> str:
    # hour: "00"~"23"
    return f"{GITHUB_RAW_BASE}/{date_str}/{hour}.json"

def build_url_for_day_summary(date_str: str) -> str:
    return f"{GITHUB_RAW_BASE}/{date_str}/summary.json"

# 从指定的URL下载并解析JSON 数据
def fetch_json(url: str) -> Optional[dict]:
    try:
        r = requests.get(url, timeout=10, headers=UA)
        if r.status_code == 200:
            return r.json()
        return None
    except Exception:
        return None
