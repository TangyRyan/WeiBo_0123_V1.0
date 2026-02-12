from __future__ import annotations

from datetime import date as date_type
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from backend.storage import load_daily_archive, load_daily_totals, save_daily_totals


DAILY_TOTAL_DAYS = 30
_daily_totals_cache: Dict[str, Any] | None = None


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
        if text.endswith(("w", "\u4e07")):
            text = text[:-1]
            multiplier = 10000.0
        try:
            return float(text) * multiplier
        except ValueError:
            return 0.0
    return 0.0


def extract_event_heat(event: Dict[str, Any]) -> float:
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


def _load_daily_totals_cache() -> Dict[str, Any]:
    global _daily_totals_cache
    if _daily_totals_cache is None:
        _daily_totals_cache = load_daily_totals()
    payload = _daily_totals_cache or {}
    data = payload.get("data")
    if not isinstance(data, list):
        payload["data"] = []
    return payload


def _persist_daily_totals_cache(payload: Dict[str, Any]) -> None:
    global _daily_totals_cache
    _daily_totals_cache = payload
    save_daily_totals(payload)


def _build_daily_totals_entry(date_str: str) -> Dict[str, Any]:
    """Aggregate heat/risk totals for a single day."""
    archive = load_daily_archive(date_str)
    heat_total = 0.0
    risk_total = 0.0
    if isinstance(archive, dict):
        for event in archive.values():
            if not isinstance(event, dict):
                continue
            heat_total += extract_event_heat(event)
            risk_raw = event.get("risk_score", 0.0)
            try:
                risk_total += float(risk_raw or 0.0)
            except (TypeError, ValueError):
                continue
    return {"date": date_str, "heat": heat_total, "risk": risk_total}


def _target_dates(days: int, today: Optional[date_type] = None) -> List[str]:
    end = today or datetime.now().date()
    start = end - timedelta(days=days - 1)
    return [(start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]


def resolve_daily_totals_window(days: int = DAILY_TOTAL_DAYS) -> List[Dict[str, Any]]:
    if days <= 0:
        return []
    target_dates = _target_dates(days)
    target_set = set(target_dates)
    cache_payload = _load_daily_totals_cache()
    cached_map: Dict[str, Dict[str, Any]] = {}
    for entry in cache_payload.get("data", []):
        if not isinstance(entry, dict):
            continue
        date_str = entry.get("date")
        if date_str not in target_set:
            continue
        cached_map[date_str] = {
            "date": date_str,
            "heat": float(entry.get("heat", 0.0) or 0.0),
            "risk": float(entry.get("risk", 0.0) or 0.0),
        }
    missing = [d for d in target_dates if d not in cached_map]
    if missing:
        for date_str in missing:
            cached_map[date_str] = _build_daily_totals_entry(date_str)
        ordered = [cached_map[d] for d in target_dates]
        payload = {"generated_until": target_dates[-1], "data": ordered}
        _persist_daily_totals_cache(payload)
        return ordered
    ordered = [cached_map[d] for d in target_dates]
    needs_trim = any(
        isinstance(entry, dict) and entry.get("date") not in target_set for entry in cache_payload.get("data", [])
    )
    if needs_trim:
        payload = {"generated_until": target_dates[-1], "data": ordered}
        _persist_daily_totals_cache(payload)
    return ordered


def refresh_daily_totals(
    days: int = DAILY_TOTAL_DAYS,
    *,
    today: Optional[date_type] = None,
) -> List[Dict[str, Any]]:
    if days <= 0:
        return []
    target_dates = _target_dates(days, today)
    ordered = [_build_daily_totals_entry(date_str) for date_str in target_dates]
    payload = {"generated_until": target_dates[-1], "data": ordered}
    _persist_daily_totals_cache(payload)
    return ordered
