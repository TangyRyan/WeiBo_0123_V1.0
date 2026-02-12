from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List

import requests
from flask import Flask, Response, jsonify, render_template, request, stream_with_context
from flask_cors import CORS
from flask_sock import Sock

from backend.config import ALLOWED_ORIGINS, ARCHIVE_DIR, HOTLIST_DIR, DAILY_LLM_TIME, LIGHT_RETENTION_DAYS
from backend.daily_totals import extract_event_heat, resolve_daily_totals_window
from backend.hotlist_stream import HotTopicsHotlistStream
from backend.scheduler import daily_llm_update, set_push_callbacks, start_scheduler, top_risk_warnings
from backend.health.api import bp as health_bp
from backend.risk_model import risk_level_from_score, risk_level_label, risk_tier_segments
from backend.storage import (
    load_daily_archive,
    load_risk_archive,
    load_risk_warnings,
    read_json,
    save_risk_archive,
    save_risk_warnings,
    write_json,
)
from backend.proxy import is_allowed_image_host, normalize_image_url
from spider.hot_topics_api import bp as hot_topics_bp
from spider.crawler_core import slugify_title

BASE_DIR = Path(__file__).resolve().parent
app = Flask(__name__, static_folder=str(BASE_DIR / "static"), template_folder=str(BASE_DIR / "templates"))
CORS(app, resources={r"/api/*": {"origins": ALLOWED_ORIGINS}})
app.register_blueprint(hot_topics_bp)
app.register_blueprint(health_bp)
sock = Sock(app)

logger = logging.getLogger(__name__)

_hotlist_clients = set()
_risk_clients = set()
_hotlist_stream: HotTopicsHotlistStream | None = None
CENTRAL_CACHE_PATH = ARCHIVE_DIR / "central_data_cache.json"
CENTRAL_CACHE_MAX_DAYS = LIGHT_RETENTION_DAYS
_central_cache: Dict[str, Any] | None = None


def _latest_risk_payload(limit: int | None = 5) -> Dict[str, Any]:
    """Return the cached risk warnings sorted and capped for display."""
    payload = load_risk_warnings() or {}
    events = payload.get("events")
    if not events:
        today = datetime.now().strftime("%Y-%m-%d")
        archive_payload = load_risk_archive(today)
        archive_events = archive_payload.get("events")
        if archive_events:
            payload = archive_payload
            events = archive_events
        else:
            payload = top_risk_warnings()
            events = payload.get("events")
            if events:
                save_risk_archive(today, payload)
        if events:
            save_risk_warnings(payload)
    if limit is None or not isinstance(events, list):
        return payload
    def _score(ev: Dict[str, Any]) -> float:
        raw = ev.get("sort_key")
        if raw is None:
            raw = ev.get("risk_score", 0.0)
        try:
            return float(raw or 0.0)
        except (TypeError, ValueError):
            return 0.0
    sorted_events = sorted(
        events,
        key=_score,
        reverse=True,
    )
    return {**payload, "events": sorted_events[:limit]}


def _broadcast(clients, message: Dict[str, Any]) -> None:
    drop = []
    for ws in list(clients):
        try:
            ws.send(json.dumps(message, ensure_ascii=False))
        except Exception:
            drop.append(ws)
    for ws in drop:
        clients.discard(ws)


def push_hotlist(message: Dict[str, Any]) -> None:
    _broadcast(_hotlist_clients, message)


def push_risk(message: Dict[str, Any]) -> None:
    _broadcast(_risk_clients, message)


def _noop_hotlist_push(_: Dict[str, Any]) -> None:
    logger.debug("Scheduler hotlist push ignored (using spider.hot_topics_ws stream)")


set_push_callbacks(_noop_hotlist_push, push_risk)


def _ensure_hotlist_stream() -> None:
    global _hotlist_stream
    if _hotlist_stream is None:
        _hotlist_stream = HotTopicsHotlistStream(push_hotlist)


_ensure_hotlist_stream()


def _parse_date(raw: Any):
    """Best-effort parse of YYYY-MM-DD to date object."""
    if not raw:
        return None
    try:
        return datetime.strptime(str(raw)[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _load_central_cache_disk() -> Dict[str, Any] | None:
    return read_json(CENTRAL_CACHE_PATH, default=None)


def _persist_central_cache(payload: Dict[str, Any]) -> None:
    global _central_cache
    _central_cache = payload
    try:
        write_json(CENTRAL_CACHE_PATH, payload)
    except Exception:
        logger.exception("Persist central cache failed")


def _build_central_cache(days: int = CENTRAL_CACHE_MAX_DAYS) -> Dict[str, Any]:
    """Build a lightweight cache for central_data to avoid频繁全量解析."""
    end = datetime.now().date()
    out: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for i in range(days):
        d = (end - timedelta(days=i)).strftime("%Y-%m-%d")
        arc = load_daily_archive(d)
        if not isinstance(arc, dict) or not arc:
            continue
        for name, ev in arc.items():
            if name in seen:
                continue
            llm = ev.get("llm")
            if not llm:
                continue
            seen.add(name)
            try:
                risk_value = float(ev.get("risk_score", 0.0) or 0.0)
            except (TypeError, ValueError):
                risk_value = 0.0
            tiers = risk_tier_segments(risk_value)
            slug = ev.get("slug") or slugify_title(name) or f"evt-{abs(hash(name)) % 10000}"
            event_date = (ev.get("last_seen_at") or ev.get("last_seen") or f"{d}T00:00:00")[:10]
            out.append({
                "event_id": f"{d}-{slug}",
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
                "热度": extract_event_heat(ev),
            })
    payload = {
        "generated_until": end.strftime("%Y-%m-%d"),
        "max_days": days,
        "data": out,
    }
    _persist_central_cache(payload)
    return payload


def _resolve_central_data(days: int, force: bool = False) -> List[Dict[str, Any]]:
    """Return deduped event list for the requested window, backed by on-disk cache."""
    if days <= 0:
        return []
    today = datetime.now().date()
    payload: Dict[str, Any] | None = None if force else (_central_cache or _load_central_cache_disk())
    if payload:
        until = payload.get("generated_until")
        max_days = int(payload.get("max_days") or 0)
        data = payload.get("data")
        data_ok = isinstance(data, list)
        # å¦‚æžœæœ€æ–°æ—¥æœŸè·ç¦»å½“å‰æ—¥æœŸè¿‡è¿œï¼Œè®¤ä¸ºç¼“å­˜å·²è¿‡æœŸï¼ˆé˜²æ­¢ generated_until æ›´æ–°ä½†æ•°æ®æ²¡æœ‰è¦†ç›–è¿‘æœŸï¼‰
        latest_date = None
        if data_ok:
            for row in data:
                row_date = _parse_date(row.get("date"))
                if row_date and (latest_date is None or row_date > latest_date):
                    latest_date = row_date
        stale = latest_date is None or latest_date < (today - timedelta(days=2))
        if until != today.strftime("%Y-%m-%d") or max_days < days or not data_ok or stale:
            payload = None
    if payload is None:
        payload = _build_central_cache(max(days, CENTRAL_CACHE_MAX_DAYS))
    data = payload.get("data") or []
    start_date = today - timedelta(days=days - 1)
    filtered = []
    for row in data:
        row_date = _parse_date(row.get("date"))
        if row_date and start_date <= row_date <= today:
            filtered.append(row)
    return filtered


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/proxy/media")
@app.route("/api/proxy/image")  # legacy path for stored references
def proxy_media():
    raw_url = request.args.get("url", "")
    if not raw_url:
        return jsonify({"error": "missing url"}), 400

    target_url = normalize_image_url(raw_url)
    if not target_url or target_url.startswith(("/proxy/media", "/api/proxy/image")):
        return jsonify({"error": "invalid url"}), 400
    if not target_url.startswith(("http://", "https://")):
        return jsonify({"error": "invalid url"}), 400
    if not is_allowed_image_host(target_url):
        return jsonify({"error": "unsupported host"}), 400

    headers = {
        "Referer": "https://weibo.com/",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36"
        ),
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }

    try:
        resp = requests.get(target_url, headers=headers, stream=True, timeout=10)
    except requests.RequestException as exc:
        logger.error("Image proxy failed for %s: %s", target_url, exc)
        return jsonify({"error": "fetch_failed"}), 502

    excluded_headers = {
        "content-encoding",
        "content-length",
        "transfer-encoding",
        "connection",
        "content-disposition",
    }
    forward_headers = []
    content_type = resp.headers.get("Content-Type")
    if content_type:
        forward_headers.append(("Content-Type", content_type))
    for name, value in resp.headers.items():
        lname = name.lower()
        if lname in excluded_headers or lname == "content-type":
            continue
        forward_headers.append((name, value))

    return Response(
        stream_with_context(resp.iter_content(chunk_size=1024)),
        status=resp.status_code,
        headers=forward_headers,
    )


@app.route("/api/daily_30")
def daily_30():
    data = resolve_daily_totals_window()
    return jsonify({"data": data})


@app.route("/api/hotlist/current")
def hotlist_current():
    data = read_json(HOTLIST_DIR / "latest.json", default=None)
    return jsonify(data or {"date": None, "hour": None, "data": []})


@app.route("/api/risk/latest")
def risk_latest():
    return jsonify(_latest_risk_payload())


@app.route("/api/risk/archive")
def risk_archive():
    date = request.args.get("date")
    if not date:
        date = datetime.now().strftime("%Y-%m-%d")
    snapshot = load_risk_archive(date)
    events = snapshot.get("events")
    if not events:
        return jsonify({"error": "risk archive not found", "date": date}), 404
    response = dict(snapshot)
    response.setdefault("date", date)
    return jsonify(response)


@app.route("/api/risk/event")
def risk_event():
    name = request.args.get("name")
    date = request.args.get("date")
    if not name or not date:
        return jsonify({"error": "name and date are required"}), 400
    archive = load_daily_archive(date)
    event = archive.get(name)
    if not event:
        return jsonify({"error": "event not found"}), 404
    response = {
        "name": name,
        "date": date,
        "risk_score": event.get("risk_score"),
        "risk_dims": event.get("risk_dims", {}),
        "llm": event.get("llm", {}),
        "summary_html": event.get("summary_html"),
        "posts": event.get("posts") or [],
    }
    score = float(event.get("risk_score", 0.0))
    segments = risk_tier_segments(score)
    response.update({
        "risk_low": segments["low"],
        "risk_mid": segments["mid"],
        "risk_high": segments["high"],
        "risk_level": risk_level_from_score(score),
        "risk_level_label": risk_level_label(risk_level_from_score(score)),
    })
    return jsonify(response)


@app.route("/api/central_data")
def central_data():
    range_opt = request.args.get("range", "week")
    days = {"week": 7, "month": 30, "three_months": 90, "halfyear": 90, "three-months": 90}.get(range_opt, 7)
    force_refresh = str(request.args.get("refresh", "")).lower() in {"1", "true", "yes"}
    data = _resolve_central_data(days, force=force_refresh)
    return jsonify({"data": data})


@app.route("/api/admin/run_daily_llm", methods=["POST", "GET"])
def run_daily_llm():
    target_date = request.args.get("date")
    if target_date:
        try:
            datetime.strptime(target_date, "%Y-%m-%d")
        except ValueError:
            return jsonify({"error": "invalid date format, expected YYYY-MM-DD"}), 400
    logger.info("Manual daily LLM trigger invoked via /api/admin/run_daily_llm (date=%s)", target_date or "yesterday")
    daily_llm_update(target_date=target_date, force=True)
    return jsonify({
        "ok": True,
        "ran_at": datetime.now().isoformat(),
        "scheduled_time": DAILY_LLM_TIME,
        "target_date": target_date,
    })


@sock.route("/ws/hotlist")
def ws_hotlist(ws):
    _hotlist_clients.add(ws)
    initial_payload = _hotlist_stream.latest_payload() if _hotlist_stream else None
    if initial_payload:
        try:
            ws.send(json.dumps(initial_payload, ensure_ascii=False))
        except Exception:
            logger.exception("Failed to send initial hotlist snapshot")
    try:
        while True:
            ws.receive()
    except Exception:
        pass
    finally:
        _hotlist_clients.discard(ws)


@sock.route("/ws/risk_warnings")
def ws_risk(ws):
    _risk_clients.add(ws)
    initial_payload = _latest_risk_payload()
    if initial_payload:
        try:
            ws.send(json.dumps(initial_payload, ensure_ascii=False))
        except Exception:
            logger.exception("Failed to send initial risk snapshot")
    try:
        while True:
            ws.receive()
    except Exception:
        pass
    finally:
        _risk_clients.discard(ws)


def create_app():
    start_scheduler()
    return app


if __name__ == "__main__":
    start_scheduler()
    port = int(os.environ.get("PORT", "8766"))
    app.run(host="0.0.0.0", port=port)
