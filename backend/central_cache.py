from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.config import ARCHIVE_DIR
from backend.risk_model import risk_level_from_score, risk_level_label, risk_tier_segments
from backend.storage import load_daily_archive, read_json, write_json
from spider.crawler_core import slugify_title


CENTRAL_CACHE_PATH = ARCHIVE_DIR / "central_data_cache.json"


def _coerce_hot_number(raw: Any) -> float:
    if raw is None:
        return 0.0
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        text = raw.strip().lower().replace(",", "")
        if not text:
            return 0.0
        multiplier = 1.0
        if text.endswith(("w", "万")):
            text = text[:-1]
            multiplier = 10000.0
        try:
            return float(text) * multiplier
        except ValueError:
            return 0.0
    return 0.0


def _extract_event_heat(event: Dict[str, Any]) -> float:
    hot_values = event.get("hot_values")
    if isinstance(hot_values, dict) and hot_values:
        try:
            latest_key = max(hot_values.keys())
            return _coerce_hot_number(hot_values.get(latest_key))
        except Exception:
            pass
    for key in ("hot", "heat", "score"):
        if key in event:
            value = _coerce_hot_number(event.get(key))
            if value:
                return value
    return 0.0


def _event_date(event: Dict[str, Any], fallback_date: str) -> str:
    raw = event.get("last_seen_at") or event.get("last_seen") or fallback_date
    return str(raw or fallback_date)[:10]


def _build_cache_entry(name: str, event: Dict[str, Any], date_str: str) -> Optional[Dict[str, Any]]:
    llm = event.get("llm")
    if not llm:
        return None
    try:
        risk_value = float(event.get("risk_score", 0.0) or 0.0)
    except (TypeError, ValueError):
        risk_value = 0.0
    tiers = risk_tier_segments(risk_value)
    slug = event.get("slug") or slugify_title(name) or f"evt-{abs(hash(name)) % 10000}"
    event_date = _event_date(event, f"{date_str}T00:00:00")
    return {
        "event_id": f"{date_str}-{slug}",
        "name": name,
        "date": event_date,
        "领域": llm.get("topic_type") or "其他",
        "地区": llm.get("region") or "国外",
        "情绪": float(llm.get("sentiment", 0.0) or 0.0),
        "风险": risk_value,
        "风险值": risk_value,
        "risk_low": tiers["low"],
        "risk_mid": tiers["mid"],
        "risk_high": tiers["high"],
        "risk_level": risk_level_from_score(risk_value),
        "risk_level_label": risk_level_label(risk_level_from_score(risk_value)),
        "热度": _extract_event_heat(event),
    }


def load_central_cache(path: Path = CENTRAL_CACHE_PATH) -> Optional[Dict[str, Any]]:
    payload = read_json(path, default=None)
    return payload if isinstance(payload, dict) else None


def build_central_cache(
    days: int,
    *,
    today: Optional[datetime] = None,
    path: Path = CENTRAL_CACHE_PATH,
) -> Dict[str, Any]:
    now = (today or datetime.now()).date()
    out: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for i in range(days):
        date_str = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        archive = load_daily_archive(date_str)
        if not isinstance(archive, dict) or not archive:
            continue
        for name, event in archive.items():
            if name in seen:
                continue
            entry = _build_cache_entry(name, event, date_str)
            if not entry:
                continue
            seen.add(name)
            out.append(entry)
    payload = {
        "generated_until": now.strftime("%Y-%m-%d"),
        "max_days": days,
        "data": out,
    }
    write_json(path, payload)
    return payload


def update_central_cache_for_date(
    date_str: str,
    archive: Dict[str, Any],
    *,
    max_days: int,
    path: Path = CENTRAL_CACHE_PATH,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    payload = load_central_cache(path) or {}
    data = payload.get("data")
    if not isinstance(data, list):
        data = []
    index: Dict[str, Dict[str, Any]] = {}
    for row in data:
        if not isinstance(row, dict):
            continue
        name = row.get("name")
        if not name:
            continue
        index[name] = row
    for name, event in archive.items():
        entry = _build_cache_entry(name, event, date_str)
        if not entry:
            continue
        index[name] = entry
    today_date = (now or datetime.now()).date()
    start_date = today_date - timedelta(days=max_days - 1)
    filtered = []
    for row in index.values():
        row_date = row.get("date")
        try:
            parsed = datetime.strptime(str(row_date)[:10], "%Y-%m-%d").date()
        except (TypeError, ValueError):
            continue
        if parsed < start_date:
            continue
        filtered.append(row)
    payload = {
        "generated_until": today_date.strftime("%Y-%m-%d"),
        "max_days": max_days,
        "data": filtered,
    }
    write_json(path, payload)
    return payload


__all__ = [
    "CENTRAL_CACHE_PATH",
    "load_central_cache",
    "build_central_cache",
    "update_central_cache_for_date",
]
