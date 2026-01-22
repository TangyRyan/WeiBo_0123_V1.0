#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.append(ROOT)

from backend.config import HEALTH_TOPIC_WINDOW_HOURS
from backend.health import refresh_health_snapshot
from backend.storage import load_daily_archive
from spider.crawler_core import CHINA_TZ

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.json$")


def _list_dates(root: Path) -> List[str]:
    if not root.exists():
        return []
    dates: List[str] = []
    for path in root.iterdir():
        if not path.is_file():
            continue
        if not DATE_RE.match(path.name):
            continue
        dates.append(path.stem)
    dates.sort()
    return dates


def _date_in_range(date_str: str, start: Optional[str], end: Optional[str]) -> bool:
    if start and date_str < start:
        return False
    if end and date_str > end:
        return False
    return True


def _has_health_event(date_str: str) -> bool:
    archive = load_daily_archive(date_str)
    for payload in archive.values():
        if not isinstance(payload, dict):
            continue
        llm = payload.get("llm") or {}
        topic_type = (llm.get("topic_type") or payload.get("topic_type") or "").strip()
        if topic_type == "健康":
            return True
    return False


def _iter_target_dates(
    dates: Iterable[str],
    *,
    start: Optional[str],
    end: Optional[str],
    skip_empty: bool,
) -> List[str]:
    selected: List[str] = []
    for date_str in dates:
        if not _date_in_range(date_str, start, end):
            continue
        if skip_empty and not _has_health_event(date_str):
            continue
        selected.append(date_str)
    return selected


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill health snapshots from archived hot_topics.")
    parser.add_argument(
        "--start-date",
        help="Start date YYYY-MM-DD (inclusive).",
    )
    parser.add_argument(
        "--end-date",
        help="End date YYYY-MM-DD (inclusive).",
    )
    parser.add_argument(
        "--hours",
        type=int,
        default=HEALTH_TOPIC_WINDOW_HOURS,
        help="Window hours to include when building snapshots.",
    )
    parser.add_argument(
        "--include-empty",
        action="store_true",
        help="Also write snapshots for dates with no health events.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    root = Path("data/hot_topics")
    all_dates = _list_dates(root)
    if not all_dates:
        print("No hot_topics archives found.")
        return 1

    target_dates = _iter_target_dates(
        all_dates,
        start=args.start_date,
        end=args.end_date,
        skip_empty=not args.include_empty,
    )

    if not target_dates:
        print("No dates matched (or all skipped due to no health events).")
        return 0

    print(f"Backfilling {len(target_dates)} days (hours={args.hours})...")
    for idx, date_str in enumerate(target_dates, 1):
        refresh_health_snapshot(target_date=date_str, hours=args.hours, now=datetime.now(tz=CHINA_TZ))
        print(f"[{idx}/{len(target_dates)}] ok: {date_str}")
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
