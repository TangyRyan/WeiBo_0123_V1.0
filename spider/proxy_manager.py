"""
Proxy manager: auto-fetch a fresh proxy IP from WEIBO_PROXY_API_URL
and write it back to WEIBO_HTTP_PROXY in the .env file.

Usage:
    from spider.proxy_manager import get_proxy_url, refresh_proxy_in_env, is_proxy_error

.env config:
    WEIBO_PROXY_API_URL=https://dps.kdlapi.com/api/getdps/?...
    WEIBO_HTTP_PROXY=http://user:pass@ip:port   # auto-managed
"""

from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path
from typing import Optional
from urllib import request as urllib_request

from spider.config import get_env_str

logger = logging.getLogger(__name__)

_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
_PROXY_KEY = "WEIBO_HTTP_PROXY"
_API_KEY = "WEIBO_PROXY_API_URL"

_lock = threading.Lock()
_last_refresh_at: float = 0.0
_MIN_REFRESH_INTERVAL = 30  # 两次刷新之间至少间隔 30 秒，防止频繁调用


# ---------- 公开接口 ----------

def get_proxy_url() -> str:
    """返回当前 .env 中的 WEIBO_HTTP_PROXY（每次从环境变量读取，刷新后立即生效）。"""
    return get_env_str(_PROXY_KEY, "").strip()


def refresh_proxy_in_env() -> str:
    """
    从 WEIBO_PROXY_API_URL 获取最新代理 IP，写回 .env 的 WEIBO_HTTP_PROXY。
    线程安全，并有最小刷新间隔保护。
    返回新的代理 URL，失败则返回空字符串。
    """
    global _last_refresh_at

    with _lock:
        now = time.time()
        if now - _last_refresh_at < _MIN_REFRESH_INTERVAL:
            current = get_proxy_url()
            logger.debug("Proxy refresh skipped (too frequent). Current: %s", current)
            return current

        api_url = get_env_str(_API_KEY, "").strip()
        if not api_url:
            logger.warning("WEIBO_PROXY_API_URL not set, cannot refresh proxy.")
            return get_proxy_url()

        try:
            proxy_url = _fetch_proxy_from_api(api_url)
        except Exception as exc:
            logger.warning("Failed to fetch proxy from API: %s", exc)
            return get_proxy_url()

        _write_env(_ENV_PATH, _PROXY_KEY, proxy_url)
        # 同步到 os.environ，让同进程其他地方立即可读
        import os
        os.environ[_PROXY_KEY] = proxy_url

        _last_refresh_at = now
        logger.info("Proxy refreshed: %s", _mask_proxy(proxy_url))

        # 代理 IP 变更后触发 cookie 重新登录（m.weibo.cn 会因 IP 变化要求重验证）
        try:
            from spider.cookie_manager import refresh_cookie_sync  # 懒导入避免循环
            logger.info("Proxy IP changed, triggering cookie re-login...")
            refresh_cookie_sync("proxy_ip_changed", notify_label="proxy_ip_changed")
        except Exception as exc:
            logger.warning("Cookie re-login after proxy refresh failed: %s", exc)

        return proxy_url


def is_proxy_error(exc: Exception) -> bool:
    """判断异常是否是代理失效导致的（460/407/连接失败等）。"""
    text = str(exc).lower()
    keywords = (
        "proxy",
        "tunnel connection failed",
        "407",
        "460",
        "cannot connect to proxy",
        "proxyerror",
    )
    return any(k in text for k in keywords)


# ---------- 内部实现 ----------

def _fetch_proxy_from_api(api_url: str) -> str:
    """
    调用代理 API，解析返回结果。
    支持两种格式：
      - ip:port:user:pass  （快代理/xkdaili 格式）
      - ip:port            （无认证格式）
    """
    req = urllib_request.Request(api_url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib_request.urlopen(req, timeout=10) as resp:
        raw = resp.read().decode("utf-8").strip()

    if not raw:
        raise ValueError("Proxy API returned empty response")

    parts = raw.split(":")
    if len(parts) == 4:
        ip, port, user, passwd = parts
        proxy_url = f"http://{user}:{passwd}@{ip}:{port}"
    elif len(parts) == 2:
        ip, port = parts
        proxy_url = f"http://{ip}:{port}"
    else:
        raise ValueError(f"Unexpected proxy API response format: {raw!r}")

    logger.debug("Fetched proxy from API: %s", _mask_proxy(proxy_url))
    return proxy_url


def _write_env(path: Path, key: str, value: str) -> None:
    """将 key=value 写入 .env 文件（无引号，代理 URL 不需要引号）。"""
    line = f"{key}={value}"
    if not path.exists():
        path.write_text(line + "\n", encoding="utf-8")
        return
    lines = path.read_text(encoding="utf-8").splitlines()
    updated = False
    for idx, raw in enumerate(lines):
        stripped = raw.strip()
        if stripped.startswith(f"{key}=") or stripped.startswith(f"{key} ="):
            lines[idx] = line
            updated = True
            break
    if not updated:
        lines.append(line)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _mask_proxy(url: str) -> str:
    """隐藏代理 URL 中的密码，用于日志输出。"""
    return re.sub(r"(://[^:]+:)[^@]+(@)", r"\1***\2", url)
