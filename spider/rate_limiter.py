"""Shared rate-limit helper with exponential backoff + cooldown windows."""

from __future__ import annotations

import os
import random
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional, Tuple


def _parse_float(value: Optional[str], default: float) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_int(value: Optional[str], default: int) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_range(value: Optional[str], default: Tuple[int, int]) -> Tuple[int, int]:
    if not value:
        return default
    parts = value.replace(" ", "").split(",")
    if len(parts) != 2:
        return default
    try:
        first = int(parts[0])
        second = int(parts[1])
    except ValueError:
        return default
    low, high = sorted((first, second))
    if low <= 0 or high <= 0:
        return default
    return low, high


def _env_name(scope: str, key: str) -> str:
    return f"RATE_LIMIT_{scope.upper()}_{key}"


def _env_float(scope: str, key: str, default: float) -> float:
    return _parse_float(os.getenv(_env_name(scope, key)), default)


def _env_int(scope: str, key: str, default: int) -> int:
    return _parse_int(os.getenv(_env_name(scope, key)), default)


def _env_range(scope: str, key: str, default: Tuple[int, int]) -> Tuple[int, int]:
    return _parse_range(os.getenv(_env_name(scope, key)), default)


@dataclass
class CooldownInfo:
    level: str
    duration: float


class RateLimitPolicy:
    """Exponential backoff with jitter + escalating cooldown windows."""

    def __init__(
        self,
        scope: str,
        *,
        base_delay: float = 5.0,
        jitter: float = 0.2,
        max_backoff_attempts: int = 3,
        soft_range: Tuple[int, int] = (300, 900),
        hard_range: Tuple[int, int] = (1800, 3600),
        cooldown_window: int = 3600,
        soft_threshold: int = 2,
    ) -> None:
        self.scope = scope
        self.base_delay = max(0.1, base_delay)
        self.jitter = max(0.0, jitter)
        self.max_backoff_attempts = max(1, max_backoff_attempts)
        self.soft_range = soft_range
        self.hard_range = hard_range
        self.cooldown_window = max(soft_range[0], cooldown_window)
        self.soft_threshold = max(1, soft_threshold)
        self.attempts = 0
        self.cooldown_until: Optional[float] = None
        self.cooldown_level: Optional[str] = None
        self.last_failure_ts: Optional[float] = None
        self._soft_hits: Deque[float] = deque()

    def _now(self) -> float:
        return time.monotonic()

    def describe(self) -> str:
        in_cooldown, remaining = self.in_cooldown()
        parts = [
            f"scope={self.scope}",
            f"attempts={self.attempts}",
        ]
        if in_cooldown and self.cooldown_level:
            parts.append(f"cooldown={self.cooldown_level}:{remaining:.0f}s")
        return " ".join(parts)

    def in_cooldown(self) -> Tuple[bool, float]:
        if self.cooldown_until is None:
            return False, 0.0
        remaining = self.cooldown_until - self._now()
        if remaining <= 0:
            self.cooldown_until = None
            self.cooldown_level = None
            return False, 0.0
        return True, remaining

    def next_delay(self) -> float:
        exp = min(self.attempts, self.max_backoff_attempts - 1)
        delay = self.base_delay * (2 ** exp)
        jitter_factor = 1.0
        if self.jitter:
            jitter_factor = random.uniform(1 - self.jitter, 1 + self.jitter)
        self.attempts += 1
        return max(delay * jitter_factor, 0.0)

    def record_failure(self) -> Optional[CooldownInfo]:
        self.last_failure_ts = self._now()
        if self.attempts < self.max_backoff_attempts:
            return None
        return self._enter_cooldown("soft")

    def record_success(self) -> None:
        self.attempts = 0
        self.last_failure_ts = None
        self._prune_soft_hits()

    def cooldown_remaining(self) -> float:
        in_cd, remaining = self.in_cooldown()
        return remaining if in_cd else 0.0

    def _enter_cooldown(self, level: str) -> CooldownInfo:
        now = self._now()
        if level == "soft":
            duration = random.uniform(*self.soft_range)
            self._soft_hits.append(now)
            self._prune_soft_hits()
            if len(self._soft_hits) >= self.soft_threshold:
                return self._enter_cooldown("hard")
        else:
            duration = random.uniform(*self.hard_range)
            self._soft_hits.clear()
        self.cooldown_until = now + duration
        self.cooldown_level = level
        self.attempts = 0
        return CooldownInfo(level, duration)

    def _prune_soft_hits(self) -> None:
        if not self._soft_hits:
            return
        threshold = self._now() - self.cooldown_window
        while self._soft_hits and self._soft_hits[0] < threshold:
            self._soft_hits.popleft()


def create_policy_from_env(
    scope: str,
    *,
    base_delay: float = 5.0,
    jitter: float = 0.2,
    max_backoff_attempts: int = 3,
    soft_range: Tuple[int, int] = (300, 900),
    hard_range: Tuple[int, int] = (1800, 3600),
    cooldown_window: int = 3600,
    soft_threshold: int = 2,
) -> RateLimitPolicy:
    return RateLimitPolicy(
        scope=scope,
        base_delay=_env_float(scope, "BASE_DELAY", base_delay),
        jitter=_env_float(scope, "JITTER", jitter),
        max_backoff_attempts=_env_int(scope, "BACKOFF_ATTEMPTS", max_backoff_attempts),
        soft_range=_env_range(scope, "SOFT_RANGE", soft_range),
        hard_range=_env_range(scope, "HARD_RANGE", hard_range),
        cooldown_window=_env_int(scope, "COOLDOWN_WINDOW", cooldown_window),
        soft_threshold=_env_int(scope, "SOFT_THRESHOLD", soft_threshold),
    )


__all__ = ["RateLimitPolicy", "CooldownInfo", "create_policy_from_env"]
