"""Unified storage helpers for archives,小时榜、帖子与风险缓存。"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from .config import AICARD_DIR, ARCHIVE_DIR, HOURLY_DIR, HOTLIST_DIR, POST_DIR, RISK_DIR
from .settings import DATA_ROOT

DAILY_TOTALS_PATH = ARCHIVE_DIR / "daily_totals.json"


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def read_json(path: Path, default=None):
    try:
        if path.exists():
            with path.open("r", encoding="utf-8") as fp:
                return json.load(fp)
    except Exception:
        return default
    return default


def write_json(path: Path, data: Any) -> None:
    """Persist JSON with unified formatting (UTF-8, indent=2, trailing newline)."""
    _ensure_parent(path)
    last_error: Optional[PermissionError] = None
    # 先尝试原子替换；Windows 上目标被占用时可能短暂拒绝，需要多次重试。
    for attempt in range(8):
        tmp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with tmp_path.open("w", encoding="utf-8") as fp:
                json.dump(data, fp, ensure_ascii=False, indent=2)
                fp.write("\n")
            tmp_path.replace(path)
            return
        except PermissionError as exc:
            # Windows 上目标文件被占用时替换会偶发被拒绝，稍作重试即可恢复。
            last_error = exc
            time.sleep(0.2 * (2**attempt))
        finally:
            # best-effort 清理残留的临时文件，避免堆积
            try:
                tmp_path.unlink(missing_ok=True)
            except PermissionError:
                pass
    # 原子替换连续失败时，退化为直接写入（非原子但更不依赖删除权限）。
    try:
        with path.open("w", encoding="utf-8") as fp:
            json.dump(data, fp, ensure_ascii=False, indent=2)
            fp.write("\n")
        return
    except PermissionError:
        pass
    if last_error:
        raise last_error


def to_data_relative(path: Path) -> str:
    """路径统一保存为相对 DATA_ROOT 的形式。"""
    try:
        return path.relative_to(DATA_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def from_data_relative(raw: str) -> Path:
    candidate = Path(raw)
    if candidate.is_absolute():
        return candidate
    return DATA_ROOT / candidate


def get_daily_archive_path(date_str: str) -> Path:
    return ARCHIVE_DIR / f"{date_str}.json"


def load_daily_archive(date_str: str) -> Dict[str, Any]:
    return read_json(get_daily_archive_path(date_str), default={}) or {}


def save_daily_archive(date_str: str, data: Dict[str, Any]) -> None:
    write_json(get_daily_archive_path(date_str), data)


def load_daily_totals() -> Dict[str, Any]:
    """Return cached per-day totals for heat/risk."""
    payload = read_json(DAILY_TOTALS_PATH, default=None) or {}
    data = payload.get("data")
    if not isinstance(data, list):
        data = []
    return {
        "generated_until": payload.get("generated_until"),
        "data": data,
    }


def save_daily_totals(data: Dict[str, Any]) -> None:
    """Persist cached per-day totals for reuse."""
    write_json(DAILY_TOTALS_PATH, data)


def get_hour_hotlist_path(date_str: str, hour: str) -> Path:
    return HOURLY_DIR / date_str / f"{hour}.json"


def load_hour_hotlist(date_str: str, hour: str):
    return read_json(get_hour_hotlist_path(date_str, hour), default=None)


def save_hour_hotlist(date_str: str, hour: str, data: Any) -> None:
    write_json(get_hour_hotlist_path(date_str, hour), data)
    write_json(HOTLIST_DIR / "latest.json", {"date": date_str, "hour": hour, "data": data})


def save_risk_warnings(data: Any) -> None:
    write_json(RISK_DIR / "latest.json", data)


def load_risk_warnings():
    return read_json(RISK_DIR / "latest.json", default={"events": []}) or {"events": []}


def save_risk_archive(date_str: str, data: Any) -> None:
    """Persist a daily snapshot of risk warnings for historical lookup."""
    payload = dict(data or {})
    payload.setdefault("date", date_str)
    write_json(RISK_DIR / f"{date_str}.json", payload)


def load_risk_archive(date_str: str) -> Dict[str, Any]:
    """Load a daily risk snapshot; returns empty structure if missing."""
    return read_json(RISK_DIR / f"{date_str}.json", default={"events": []}) or {"events": []}


def get_post_snapshot_path(date_str: str, slug: str) -> Path:
    return POST_DIR / date_str / f"{slug}.json"


def load_post_snapshot(date_str: str, slug: str) -> Optional[Dict[str, Any]]:
    return read_json(get_post_snapshot_path(date_str, slug), default=None)


def get_aicard_hour_dir(date_str: str, hour: str) -> Path:
    return AICARD_DIR / "hourly" / date_str / hour


__all__ = [
    "ARCHIVE_DIR",
    "HOURLY_DIR",
    "POST_DIR",
    "AICARD_DIR",
    "RISK_DIR",
    "to_data_relative",
    "from_data_relative",
    "load_daily_archive",
    "save_daily_archive",
    "load_daily_totals",
    "save_daily_totals",
    "load_hour_hotlist",
    "save_hour_hotlist",
    "load_risk_warnings",
    "save_risk_warnings",
    "save_risk_archive",
    "load_risk_archive",
    "get_post_snapshot_path",
    "load_post_snapshot",
    "get_aicard_hour_dir",
]
