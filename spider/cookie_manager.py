from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

from spider.config import DEFAULT_ENV_PATH, get_env_int, get_env_str

logger = logging.getLogger(__name__)

ENV_COOKIE_KEY = "WEIBO_COOKIE"
_DEFAULT_REFRESH_COOLDOWN_SECONDS = 600

_REFRESH_LOCK = threading.Lock()
_REFRESH_IN_PROGRESS = False
_LAST_REFRESH_AT = 0.0


def _resolve_env_path() -> Path:
    raw = get_env_str("WEIBO_COOKIE_ENV_PATH") or get_env_str("RUN_SERVER_WEIBO_LOGIN_ENV_PATH")
    if raw:
        return Path(raw)
    return Path(DEFAULT_ENV_PATH)


def _read_env_value(path: Path, key: str) -> str:
    if not path.exists():
        return ""
    value = ""
    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() != key:
                continue
            value = v.strip().strip("\"'")
    except Exception as exc:
        logger.warning("Failed to read env %s from %s: %s", key, path, exc)
        return ""
    return value


def get_env_path() -> Path:
    return _resolve_env_path()


def get_cookie() -> str:
    env_path = _resolve_env_path()
    cookie = _read_env_value(env_path, ENV_COOKIE_KEY)
    if cookie:
        return cookie.strip()
    return (os.environ.get(ENV_COOKIE_KEY) or "").strip()


def _reserve_refresh_slot(now_ts: float, cooldown: int) -> bool:
    global _REFRESH_IN_PROGRESS, _LAST_REFRESH_AT  # noqa: PLW0603
    with _REFRESH_LOCK:
        if _REFRESH_IN_PROGRESS:
            return False
        if cooldown > 0 and _LAST_REFRESH_AT and (now_ts - _LAST_REFRESH_AT) < cooldown:
            return False
        _REFRESH_IN_PROGRESS = True
        _LAST_REFRESH_AT = now_ts
        return True


def _release_refresh_slot() -> None:
    global _REFRESH_IN_PROGRESS  # noqa: PLW0603
    with _REFRESH_LOCK:
        _REFRESH_IN_PROGRESS = False


def _run_async(coro):
    try:
        return asyncio.run(coro)
    except RuntimeError as exc:
        if "asyncio.run()" in str(exc):
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(coro)
            finally:
                loop.close()
        raise


def _refresh_cookie_task(
    reason: str,
    notify_label: str,
    *,
    env_path: Optional[Path],
    logger_: logging.Logger,
) -> Optional[str]:
    env_path = env_path or _resolve_env_path()
    try:
        from spider import weibo_topic_detail as weibo_login
    except Exception as exc:  # pragma: no cover - defensive guard
        logger_.exception("Failed to import weibo login module: %s", exc)
        return None

    weibo_login.ENV_PATH = env_path

    async def _login() -> Optional[list]:
        async with weibo_login.async_playwright() as playwright:
            return await weibo_login._login_and_update_cookies(
                playwright,
                notify_label=notify_label,
                notify_reason=reason,
            )

    try:
        cookies = _run_async(_login())
    except Exception as exc:
        logger_.exception("Weibo login flow failed: %s", exc)
        return None

    if not cookies:
        logger_.warning("Weibo login finished but no cookies returned.")
        return None

    cookie_str = weibo_login.build_cookie_string(cookies)
    if cookie_str:
        os.environ[ENV_COOKIE_KEY] = cookie_str
        try:
            weibo_login._write_env_file(env_path, ENV_COOKIE_KEY, cookie_str)
        except Exception:
            logger_.exception("Failed to write %s to %s", ENV_COOKIE_KEY, env_path)
    return cookie_str or None


def refresh_cookie_sync(
    reason: str,
    *,
    notify_label: str,
    env_path: Optional[Path] = None,
    logger_: Optional[logging.Logger] = None,
) -> Optional[str]:
    logger_ = logger_ or logger
    cooldown = get_env_int("WEIBO_COOKIE_REFRESH_COOLDOWN_SECONDS", _DEFAULT_REFRESH_COOLDOWN_SECONDS)
    if cooldown is None:
        cooldown = _DEFAULT_REFRESH_COOLDOWN_SECONDS
    now_ts = time.time()
    if not _reserve_refresh_slot(now_ts, cooldown):
        return None
    try:
        return _refresh_cookie_task(reason, notify_label, env_path=env_path, logger_=logger_)
    finally:
        _release_refresh_slot()


def refresh_cookie_async(
    reason: str,
    *,
    notify_label: str,
    env_path: Optional[Path] = None,
    logger_: Optional[logging.Logger] = None,
) -> bool:
    logger_ = logger_ or logger
    cooldown = get_env_int("WEIBO_COOKIE_REFRESH_COOLDOWN_SECONDS", _DEFAULT_REFRESH_COOLDOWN_SECONDS)
    if cooldown is None:
        cooldown = _DEFAULT_REFRESH_COOLDOWN_SECONDS
    now_ts = time.time()
    if not _reserve_refresh_slot(now_ts, cooldown):
        return False

    def _worker() -> None:
        try:
            _refresh_cookie_task(reason, notify_label, env_path=env_path, logger_=logger_)
        finally:
            _release_refresh_slot()

    thread = threading.Thread(target=_worker, name="weibo-login-refresh", daemon=True)
    thread.start()
    return True


__all__ = [
    "ENV_COOKIE_KEY",
    "get_cookie",
    "get_env_path",
    "refresh_cookie_sync",
    "refresh_cookie_async",
]
