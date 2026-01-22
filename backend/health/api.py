from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List

from flask import Blueprint, jsonify, request

from backend.health import available_dates, health_event_detail, latest_timeline
from backend.health.dataset_loader import dataset_date_range, load_dataset_detail, load_dataset_events, summarize_events
from spider.crawler_core import CHINA_TZ

bp = Blueprint("health_api", __name__, url_prefix="/api/health")


@bp.route("/timeline")
def health_timeline():
    date = request.args.get("date")
    hours = request.args.get("hours", type=int)
    range_start, range_end = dataset_date_range()
    dataset_events = load_dataset_events(hours=hours)
    if dataset_events:
        latest_event_date = max((ev.get("date") or "") for ev in dataset_events) if dataset_events else None
        base_date = range_end or latest_event_date or date
        return jsonify({
            "date": base_date,
            "start_date": range_start,
            "end_date": range_end,
            "updated_at": datetime.now(tz=CHINA_TZ).isoformat(),
            "summary": summarize_events(dataset_events),
            "events": dataset_events,
        })

    payload = latest_timeline(date)
    if not payload:
        return jsonify({"error": "health timeline not ready"}), 404

    events = payload.get("events") or []
    summary = payload.get("summary") or {}
    updated_at = payload.get("updated_at")
    base_date = payload.get("date") or date

    if hours and hours > 0:
        cutoff = datetime.now(tz=CHINA_TZ) - timedelta(hours=hours)
        cutoff_ts = int(cutoff.timestamp())
        events = [event for event in events if (event.get("end_ts") or 0) >= cutoff_ts]
        summary = _build_summary(events)

    return jsonify({
        "date": base_date,
        "start_date": range_start,
        "end_date": range_end or base_date,
        "updated_at": updated_at,
        "summary": summary,
        "events": events,
    })


@bp.route("/events/<event_id>")
def health_event(event_id: str):
    date = request.args.get("date")
    detail = load_dataset_detail(event_id)
    if detail:
        return jsonify(detail)
    detail = health_event_detail(event_id, date)
    if not detail:
        return jsonify({"error": "event not found"}), 404
    return jsonify(detail)


@bp.route("/dates")
def health_dates():
    return jsonify({"dates": available_dates()})


def _build_summary(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    counts: Dict[str, int] = {}
    for event in events:
        major = event.get("category") or "其他"
        counts[major] = counts.get(major, 0) + 1
    return {"total_events": len(events), "by_major": counts}
