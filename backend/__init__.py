"""Backend package bootstrap."""

from __future__ import annotations

import logging

from .settings import DATA_ROOT, get_env_str, load_env

load_env()


def _configure_logging() -> None:
    """Ensure backend logging is initialized exactly once."""
    level_name = (get_env_str("WEIBO_BACKEND_LOG_LEVEL", "INFO") or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    root_logger = logging.getLogger()
    if not root_logger.handlers:
        logging.basicConfig(
            level=level,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        )
    else:
        root_logger.setLevel(level)
    logging.getLogger(__name__).debug("Backend logging initialized at %s", level_name)


_configure_logging()

__all__ = ["DATA_ROOT", "load_env"]
