"""Shared environment helpers and data-root configuration."""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"
_ENV_LOADED = False


def load_env(path: Optional[Path] = None) -> None:
    """Populate os.environ using a .env file, if present."""
    global _ENV_LOADED  # noqa: PLW0603
    if _ENV_LOADED:
        return
    target = path or DEFAULT_ENV_PATH
    if target.exists():
        for raw_line in target.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if not key or key in os.environ:
                continue
            os.environ[key] = value.strip().strip("\"'")
    _ENV_LOADED = True


def _ensure_env_loaded() -> None:
    if not _ENV_LOADED:
        load_env()


def get_env(name: str, default: Optional[str] = None) -> Optional[str]:
    """Return an environment variable with a default fallback."""
    _ensure_env_loaded()
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value


def get_env_str(name: str, default: Optional[str] = None) -> Optional[str]:
    return get_env(name, default)


def get_env_int(name: str, default: Optional[int] = None) -> Optional[int]:
    value = get_env(name)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def get_env_float(name: str, default: Optional[float] = None) -> Optional[float]:
    value = get_env(name)
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def get_env_bool(name: str, default: bool = False) -> bool:
    value = get_env(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def get_env_list(
    name: str,
    default: Optional[Sequence[str]] = None,
    *,
    separator: str = ",",
) -> List[str]:
    value = get_env(name)
    if value is None:
        return list(default) if default is not None else []
    return [item.strip() for item in value.split(separator) if item.strip()]


def _resolve_data_root(raw: Optional[str]) -> Path:
    base = Path(raw) if raw else PROJECT_ROOT / "data"
    if not base.is_absolute():
        base = (PROJECT_ROOT / base).resolve()
    return base


load_env()
DATA_ROOT = _resolve_data_root(os.environ.get("WEIBO_DATA_ROOT"))

__all__ = [
    "PROJECT_ROOT",
    "DEFAULT_ENV_PATH",
    "DATA_ROOT",
    "load_env",
    "get_env",
    "get_env_str",
    "get_env_bool",
    "get_env_float",
    "get_env_int",
    "get_env_list",
]
