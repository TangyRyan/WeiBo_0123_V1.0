"""Convenience launcher for the unified backend + scheduler."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
from pathlib import Path

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.append(ROOT)

from spider.config import get_env_bool, get_env_float, get_env_int, get_env_str  # noqa: E402


_logger = logging.getLogger("launcher")
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

_WEIBO_STARTUP_NOTIFY_LABEL = "server_startup_login_refresh"


def _should_weibo_login() -> bool:
    if not get_env_bool("RUN_SERVER_WEIBO_LOGIN", True):
        _logger.info("RUN_SERVER_WEIBO_LOGIN=0 -> skipping media_platform login")
        return False
    if get_env_str("WEIBO_COOKIES"):
        _logger.warning("WEIBO_COOKIES is set but ignored; use WEIBO_COOKIE only.")
    if get_env_str("WEIBO_COOKIE"):
        return False
    return True


def _ensure_weibo_cookie() -> None:
    if not _should_weibo_login():
        return
    try:
        from spider import weibo_topic_detail as weibo_login  # noqa: WPS433
    except Exception as exc:  # pragma: no cover - defensive guard
        _logger.exception("Failed to import spider.weibo_topic_detail: %s", exc)
        raise

    user_data_dir = Path(
        get_env_str("RUN_SERVER_WEIBO_LOGIN_USER_DATA_DIR", str(weibo_login.USER_DATA_DIR))
        or str(weibo_login.USER_DATA_DIR)
    )
    headless = get_env_bool("RUN_SERVER_WEIBO_LOGIN_HEADLESS", True)
    timeout_seconds = (
        get_env_int("RUN_SERVER_WEIBO_LOGIN_TIMEOUT", weibo_login.LOGIN_TIMEOUT_SECONDS)
        or weibo_login.LOGIN_TIMEOUT_SECONDS
    )
    env_path = Path(
        get_env_str("RUN_SERVER_WEIBO_LOGIN_ENV_PATH", str(weibo_login.ENV_PATH))
        or str(weibo_login.ENV_PATH)
    )

    if not os.environ.get("WEIBO_COOKIE_ENV_PATH"):
        os.environ["WEIBO_COOKIE_ENV_PATH"] = str(env_path)

    weibo_login.USER_DATA_DIR = user_data_dir
    weibo_login.LOGIN_HEADLESS = headless
    weibo_login.LOGIN_TIMEOUT_SECONDS = timeout_seconds
    weibo_login.ENV_PATH = env_path

    _logger.info("Launching QR login flow for WEIBO_COOKIE (headless=%s)", headless)

    async def _run_login():
        async with weibo_login.async_playwright() as playwright:
            return await weibo_login._login_and_update_cookies(
                playwright,
                notify_label=_WEIBO_STARTUP_NOTIFY_LABEL,
                notify_reason="startup_missing_weibo_cookie",
            )

    cookies = asyncio.run(_run_login())
    if not cookies:
        raise RuntimeError("Weibo login failed; no cookies returned.")

    os.environ["WEIBO_COOKIE"] = weibo_login.build_cookie_string(cookies)
    _logger.info("Weibo login completed; WEIBO_COOKIE updated")


def _start_monitor_thread() -> threading.Thread | None:
    """Start spider/monitor_remote_hot_topics in a background thread if enabled."""
    if not get_env_bool("RUN_SERVER_EMBED_MONITOR", True):
        _logger.warning("RUN_SERVER_EMBED_MONITOR=0 -> skipping hot topics monitor task")
        return None
    from spider.monitor_remote_hot_topics import (  # noqa: E402
        configure_logging as configure_monitor_logging,
        run_loop as monitor_run_loop,
    )

    def _worker() -> None:
        configure_monitor_logging()
        worker_logger = logging.getLogger("launcher.monitor")
        worker_logger.info("Starting hot topics monitor loop (spider/monitor_remote_hot_topics.py)")
        try:
            asyncio.run(monitor_run_loop())
        except Exception:  # pragma: no cover - defensive guard
            worker_logger.exception("Hot topics monitor loop exited unexpectedly")

    thread = threading.Thread(target=_worker, name="hot-topics-monitor", daemon=True)
    thread.start()
    return thread


def _start_ws_thread() -> threading.Thread | None:
    """Start spider/hot_topics_ws in a background thread if enabled."""
    if not get_env_bool("RUN_SERVER_EMBED_WS", True):
        _logger.warning("RUN_SERVER_EMBED_WS=0 -> skipping standalone hot-topics WebSocket service")
        return None
    from spider.hot_topics_ws import (  # noqa: E402
        DEFAULT_REFRESH_SECONDS,
        configure_logging as configure_ws_logging,
        start_hot_topics_ws,
    )

    host = get_env_str("HOT_TOPICS_WS_HOST", "0.0.0.0") or "0.0.0.0"
    port = get_env_int("HOT_TOPICS_WS_PORT", 8765) or 8765
    refresh = get_env_float("HOT_TOPICS_WS_REFRESH", DEFAULT_REFRESH_SECONDS) or DEFAULT_REFRESH_SECONDS
    auto_port = get_env_bool("HOT_TOPICS_WS_AUTO_PORT", False)
    log_level_name = (get_env_str("HOT_TOPICS_WS_LOG", "INFO") or "INFO").upper()
    log_level = getattr(logging, log_level_name, logging.INFO)

    def _worker() -> None:
        configure_ws_logging(log_level)
        worker_logger = logging.getLogger("launcher.hot_topics_ws")
        worker_logger.info(
            "Starting hot-topics WebSocket service host=%s port=%s refresh=%.1fs auto_port=%s",
            host,
            port,
            refresh,
            auto_port,
        )
        try:
            asyncio.run(
                start_hot_topics_ws(
                    host=host,
                    port=port,
                    refresh_interval=refresh,
                    auto_port=auto_port,
                )
            )
        except Exception:  # pragma: no cover - defensive guard
            worker_logger.exception("Hot-topics WebSocket service exited unexpectedly")

    thread = threading.Thread(target=_worker, name="hot-topics-ws", daemon=True)
    thread.start()
    return thread


def _launch_background_services() -> None:
    """Spin up monitor + WS services so run_server.py brings up everything."""
    threads = [_start_monitor_thread(), _start_ws_thread()]
    alive = [t.name for t in threads if t is not None]
    if alive:
        _logger.info("Background tasks started: %s", ", ".join(alive))
    else:
        _logger.warning("No background tasks started; check RUN_SERVER_EMBED_* settings")


def _create_app():
    from backend.app import create_app  # noqa: E402

    return create_app()


_ensure_weibo_cookie()
app = _create_app()
_launch_background_services()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8766"))
    app.run(host="0.0.0.0", port=port, debug=True)
