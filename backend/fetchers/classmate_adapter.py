# -*- coding: utf-8 -*-
"""Local-first adapter with HTTP fallback."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import requests

from backend.storage import from_data_relative, load_daily_archive, load_hour_hotlist, save_daily_archive
from backend.settings import get_env_int, get_env_str
from spider.crawler_core import CHINA_TZ
from spider.update_posts import ensure_topic_posts

PEER_HOST = get_env_str("WEIBO_API_HOST", "127.0.0.1") or "127.0.0.1"
PEER_PORT = get_env_int("WEIBO_API_PORT", 8767) or 8767
PEER_BASE_URL = f"http://{PEER_HOST}:{PEER_PORT}"
TIMEOUT = 20


def _normalize_hot_topics(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    normalized = []
    for idx, item in enumerate(items):
        title = item.get("title") or item.get("name") or item.get("word") or ""
        normalized.append({
            "rank": int(item.get("rank") or item.get("index") or idx + 1),
            "name": title,
            "hot": item.get("hot") or item.get("heat") or item.get("num") or 0,
        })
    return normalized


def _load_local_hotlist(date_str: str, hour: str) -> Optional[List[Dict[str, Any]]]:
    payload = load_hour_hotlist(date_str, hour)
    if not payload:
        return None
    if isinstance(payload, dict):
        payload = payload.get("topics") or payload.get("items") or []
    if not isinstance(payload, list):
        return None
    return _normalize_hot_topics(payload)


def get_hourly_hotlist_from_peer(date_str: str, hour: str) -> Optional[List[Dict[str, Any]]]:
    local = _load_local_hotlist(date_str, hour.zfill(2))
    if local:
        return local
    try:
        url = f"{PEER_BASE_URL}/api/hot_topics/hourly"
        resp = requests.get(url, params={"date": date_str, "hour": int(hour)}, timeout=TIMEOUT)
        if resp.status_code != 200:
            return None
        data = resp.json()
        topics = data.get("topics") or data.get("items") or []
        return _normalize_hot_topics(topics)
    except Exception:
        return None


def _coerce_media(item: Dict[str, Any]) -> List[str]:
    media: List[str] = []
    pics = item.get("pics") or item.get("image_links") or []
    if isinstance(pics, list):
        for pic in pics:
            if isinstance(pic, dict):
                url = pic.get("url") or (pic.get("large") or {}).get("url")
                if url:
                    media.append(url)
            elif isinstance(pic, str):
                media.append(pic)
    video = item.get("video")
    if isinstance(video, dict):
        streams = video.get("streams")
        if isinstance(streams, dict):
            for url in streams.values():
                if url:
                    media.append(url)
                    break
        url = video.get("url")
        if url:
            media.append(url)
    return media


def _normalize_posts(payload: Dict[str, Any], event_name: str, limit: int) -> List[Dict[str, Any]]:
    posts: List[Dict[str, Any]] = []
    items = payload.get("items") or []
    for idx, item in enumerate(items[:limit]):
        posts.append({
            "post_id": item.get("id") or item.get("post_id") or f"{hash(event_name)%10**6}_{idx}",
            "published_at": item.get("created_at") or item.get("timestamp") or item.get("published_at") or "",
            "account_name": item.get("user_name") or item.get("author") or item.get("account_name") or "未知用户",
            "content_text": (
                item.get("content_text")
                or item.get("text")
                or item.get("content")
                or item.get("text_raw")
                or ""
            ),
            "media": _coerce_media(item),
            "reposts": int(item.get("reposts") or item.get("forwards_count") or 0),
            "comments": int(item.get("comments") or item.get("comments_count") or 0),
            "likes": int(item.get("likes") or item.get("likes_count") or 0),
        })
    return posts


def _read_post_payload_from_path(raw_path: str) -> Optional[Dict[str, Any]]:
    try:
        path = from_data_relative(raw_path)
    except Exception:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _load_local_posts(event_name: str, limit: int) -> List[Dict[str, Any]]:
    today = datetime.now(tz=CHINA_TZ).date()
    for offset in range(0, 3):
        date_str = (today - timedelta(days=offset)).strftime("%Y-%m-%d")
        archive = load_daily_archive(date_str)
        record = archive.get(event_name)
        if not record:
            continue
        payload = record.get("latest_posts")
        if not payload:
            updated = ensure_topic_posts(event_name, record, date_str)
            archive[event_name] = updated
            save_daily_archive(date_str, archive)
            payload = updated.get("latest_posts")
        if isinstance(payload, dict):
            posts = _normalize_posts(payload, event_name, limit)
            if posts:
                return posts
            snapshot = payload.get("snapshot") or payload.get("post_output")
            if snapshot:
                snap_payload = _read_post_payload_from_path(snapshot)
                posts = _normalize_posts(snap_payload or {}, event_name, limit)
                if posts:
                    return posts
        post_path = record.get("post_output")
        if post_path:
            snap_payload = _read_post_payload_from_path(post_path)
            posts = _normalize_posts(snap_payload or {}, event_name, limit)
            if posts:
                return posts
    return []


def fetch_posts_for_event_from_peer(event_name: str, limit: int = 20) -> List[Dict[str, Any]]:
    local_posts = _load_local_posts(event_name, limit)
    if local_posts:
        return local_posts
    try:
        url = f"{PEER_BASE_URL}/api/hot_topics/posts"
        resp = requests.get(url, params={"title": event_name, "limit": limit}, timeout=TIMEOUT)
        if resp.status_code != 200:
            return []
        data = resp.json()
        payload = {"items": data.get("items", [])}
        return _normalize_posts(payload, event_name, limit)
    except Exception:
        return []
