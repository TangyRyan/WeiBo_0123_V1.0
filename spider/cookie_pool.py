"""Simple Cookie 池，支持多来源加载与失败切换。"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional

from spider.config import get_env_bool, get_env_str
from spider.notify_email import notify_cookie_invalid

logger = logging.getLogger(__name__)

POOL_PATH = Path(__file__).parent / "weibo_cookies_pool.json"
LEGACY_PATH = Path(__file__).parent / "weibo_cookies.json"

ENV_MULTI = "WEIBO_COOKIES"
ENV_SINGLE = "WEIBO_COOKIE"
ENV_POOL_ENABLED = "WEIBO_COOKIE_POOL_ENABLED"

# 失败后暂时冷却该 Cookie，避免持续命中风控
COOKIE_COOLDOWN_SECONDS = 600

# 最兜底的一份 Cookie（完全没有配置时才会用）
FALLBACK_COOKIE = "SCF=Ag66U6NXNgzpvI1h1GSjWh8w7HR4yV1THrr4GQFUaPUroylue-wKVTgdNYbxXPXaA4OykzyEAw1XrwEWSLHXGnc.;"


@dataclass(frozen=True)
class CookieChoice:
    cookie: str
    label: str


@dataclass
class _CookieEntry:
    value: str
    label: str
    last_bad: float = 0.0

    def available(self, now: float, cooldown: float) -> bool:
        return self.last_bad <= 0 or (now - self.last_bad) >= cooldown


class CookiePool:
    """轮询 Cookie 池，并在失败时切换。"""

    def __init__(self, entries: List[_CookieEntry], *, fallback: str, cooldown: float = COOKIE_COOLDOWN_SECONDS):
        self._entries = entries
        self._fallback = fallback.strip()
        self._cooldown = cooldown
        self._index: Optional[int] = 0 if entries else None

    def __len__(self) -> int:  # pragma: no cover - 简单辅助
        return len(self._entries)

    def current(self) -> CookieChoice:
        """获取当前可用的 Cookie，不会标记为失败。"""
        now = time.time()
        if not self._entries:
            return CookieChoice(cookie=self._fallback, label="fallback")

        if self._index is not None and self._entries[self._index].available(now, self._cooldown):
            entry = self._entries[self._index]
            return CookieChoice(cookie=entry.value, label=entry.label)

        idx = self._pick_available(start=self._index + 1 if self._index is not None else 0, now=now)
        self._index = idx
        entry = self._entries[idx]
        return CookieChoice(cookie=entry.value, label=entry.label)

    def mark_bad(self, choice: CookieChoice, reason: str = "") -> CookieChoice:
        """
        将当前 Cookie 标记为失效，并返回下一份。
        如果池为空，继续使用 fallback。
        """
        if not self._entries:
            return CookieChoice(cookie=self._fallback, label="fallback")

        idx = self._find_index(choice)
        if idx is None:
            # 未找到匹配，保持当前索引但仍尝试切换
            idx = self._index or 0

        now = time.time()
        entry = self._entries[idx]
        entry.last_bad = now
        self._index = idx
        if reason:
            logger.warning("Marking cookie %s as bad: %s", entry.label, reason)
        try:
            notify_cookie_invalid(entry.label, reason)
        except Exception:  # pragma: no cover - best-effort notify
            logger.exception("Failed to notify cookie invalid.")

        return self._rotate_from_current(now)

    def _rotate_from_current(self, now: float) -> CookieChoice:
        if not self._entries:
            return CookieChoice(cookie=self._fallback, label="fallback")
        start = (self._index or 0) + 1
        idx = self._pick_available(start=start, now=now)
        self._index = idx
        entry = self._entries[idx]
        return CookieChoice(cookie=entry.value, label=entry.label)

    def _pick_available(self, start: int, now: float) -> int:
        if not self._entries:
            return 0
        n = len(self._entries)
        for offset in range(n):
            idx = (start + offset) % n
            if self._entries[idx].available(now, self._cooldown):
                return idx
        # 如果都在冷却，返回 start 对应的索引（避免 None）
        return start % n

    def _find_index(self, choice: CookieChoice) -> Optional[int]:
        for idx, entry in enumerate(self._entries):
            if entry.value == choice.cookie:
                return idx
        return None


def _normalize_cookie_string(raw: str) -> str:
    cookie = (raw or "").strip()
    if cookie.endswith(";"):
        cookie = cookie[:-1].strip()
    return cookie


def _split_env_values(raw: str) -> List[str]:
    return [item.strip() for item in raw.replace("|", "\n").splitlines() if item.strip()]


def _cookie_dicts_to_string(items: Iterable[dict]) -> str:
    parts: List[str] = []
    for item in items:
        name = item.get("name")
        value = item.get("value")
        if name and value is not None:
            parts.append(f"{name}={value}")
    return "; ".join(parts)


def _coerce_cookie_strings(data: object) -> List[str]:
    """尽可能把文件/对象中的 cookie 结构转成字符串列表。"""
    if isinstance(data, str):
        return [data] if data.strip() else []
    if isinstance(data, dict):
        if "cookies" in data:
            return _coerce_cookie_strings(data["cookies"])
        if "name" in data and "value" in data:
            return [_cookie_dicts_to_string([data])]
        return []
    if isinstance(data, list):
        if not data:
            return []
        if all(isinstance(item, dict) and "name" in item for item in data):
            return [_cookie_dicts_to_string(data)]
        result: List[str] = []
        for item in data:
            result.extend(_coerce_cookie_strings(item))
        return result
    return []


def _load_json_cookies(path: Path) -> List[str]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - 容错日志
        logger.warning("Failed to read %s: %s", path, exc)
        return []
    return _coerce_cookie_strings(data)


def _build_pool_entries() -> List[_CookieEntry]:
    entries: List[_CookieEntry] = []
    seen: set[str] = set()

    pool_enabled = get_env_bool(ENV_POOL_ENABLED, True)
    if not pool_enabled:
        logger.warning("%s=0 -> cookie pool disabled, using single-cookie mode", ENV_POOL_ENABLED)
        env_single = get_env_str(ENV_SINGLE)
        if env_single:
            cookie = _normalize_cookie_string(env_single)
            if cookie and cookie not in seen:
                entries.append(_CookieEntry(value=cookie, label=ENV_SINGLE))
                seen.add(cookie)

        if not entries:
            # 仅在单 Cookie 模式且环境变量缺失时，回退读取旧文件
            cookies = _load_json_cookies(LEGACY_PATH)
            for idx, raw in enumerate(cookies, 1):
                cookie = _normalize_cookie_string(raw)
                if cookie and cookie not in seen:
                    entries.append(_CookieEntry(value=cookie, label=f"legacy[{idx}]"))
                    seen.add(cookie)

        if entries:
            labels = ", ".join(entry.label for entry in entries)
            logger.info("Loaded %s cookies (%s)", len(entries), labels)
        else:
            logger.warning("No cookies loaded from env/files; using fallback only.")
        return entries

    env_multi = get_env_str(ENV_MULTI)
    if env_multi:
        for idx, raw in enumerate(_split_env_values(env_multi), 1):
            cookie = _normalize_cookie_string(raw)
            if cookie and cookie not in seen:
                entries.append(_CookieEntry(value=cookie, label=f"{ENV_MULTI}[{idx}]"))
                seen.add(cookie)

    env_single = get_env_str(ENV_SINGLE)
    if env_single:
        cookie = _normalize_cookie_string(env_single)
        if cookie and cookie not in seen:
            entries.append(_CookieEntry(value=cookie, label=ENV_SINGLE))
            seen.add(cookie)

    for path, prefix in [(POOL_PATH, "pool"), (LEGACY_PATH, "legacy")]:
        cookies = _load_json_cookies(path)
        for idx, raw in enumerate(cookies, 1):
            cookie = _normalize_cookie_string(raw)
            if cookie and cookie not in seen:
                entries.append(_CookieEntry(value=cookie, label=f"{prefix}[{idx}]"))
                seen.add(cookie)

    if entries:
        labels = ", ".join(entry.label for entry in entries)
        logger.info("Loaded %s cookies (%s)", len(entries), labels)
    else:
        logger.warning("No cookies loaded from env/files; using fallback only.")
    return entries


_COOKIE_POOL: Optional[CookiePool] = None


def get_cookie_pool() -> CookiePool:
    global _COOKIE_POOL  # noqa: PLW0603
    if _COOKIE_POOL is None:
        entries = _build_pool_entries()
        _COOKIE_POOL = CookiePool(entries, fallback=FALLBACK_COOKIE, cooldown=COOKIE_COOLDOWN_SECONDS)
    return _COOKIE_POOL


__all__ = ["CookieChoice", "COOKIE_COOLDOWN_SECONDS", "FALLBACK_COOKIE", "get_cookie_pool"]
