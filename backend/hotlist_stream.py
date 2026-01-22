from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from spider.hot_topics_ws import DEFAULT_REFRESH_SECONDS, HotTopicsRepository

logger = logging.getLogger(__name__)


HotlistCallback = Callable[[Dict[str, Any]], None]


class HotTopicsHotlistStream:
    """Poll spider.hot_topics_ws snapshots and push updates to Flask-Sock clients."""

    def __init__(
        self,
        push_callback: HotlistCallback,
        *,
        refresh_interval: Optional[float] = None,
        limit: Optional[int] = None,
    ) -> None:
        self._push_callback = push_callback
        self._repository = HotTopicsRepository()
        self._refresh = max(1.0, float(refresh_interval or DEFAULT_REFRESH_SECONDS))
        self._limit = limit
        self._latest_message: Optional[Dict[str, Any]] = None
        self._last_version: Optional[Any] = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run_loop, name="hotlist-stream", daemon=True)
        self._thread.start()
        logger.info("HotTopicsHotlistStream started with refresh=%ss limit=%s", self._refresh, self._limit)

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread.is_alive():
            self._thread.join(timeout=1.0)

    def latest_payload(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            return json.loads(json.dumps(self._latest_message, ensure_ascii=False)) if self._latest_message else None

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                snapshot = self._repository.get_snapshot()
                if snapshot and snapshot.ref.version != self._last_version:
                    self._last_version = snapshot.ref.version
                    payload = snapshot.to_payload(limit=self._limit, message_type="update")
                    message = self._convert_payload(payload)
                    with self._lock:
                        self._latest_message = message
                    logger.debug(
                        "Broadcasting hotlist snapshot date=%s hour=%s size=%s",
                        message.get("date"),
                        message.get("hour"),
                        len(message.get("items") or []),
                    )
                    self._push_callback(message)
            except Exception as exc:  # pragma: no cover - defensive
                logger.exception("HotTopicsHotlistStream loop error: %s", exc)
            finally:
                time.sleep(self._refresh)

    @staticmethod
    def _convert_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
        items = []
        for topic in payload.get("topics", []):
            items.append({
                "rank": topic.get("rank"),
                "name": topic.get("name") or topic.get("title") or topic.get("topic") or "",
                "title": topic.get("title") or topic.get("name"),
                "category": topic.get("category"),
                "description": topic.get("description"),
                "url": topic.get("url"),
                "hot": topic.get("hot") or topic.get("heat") or topic.get("score"),
                "ads": topic.get("ads"),
                "readCount": topic.get("readCount"),
                "discussCount": topic.get("discussCount"),
                "origin": topic.get("origin"),
            })
        return {
            "type": payload.get("type", "update"),
            "date": payload.get("date"),
            "hour": payload.get("hour"),
            "generated_at": payload.get("generated_at"),
            "items": items,
            "total": payload.get("total"),
        }
