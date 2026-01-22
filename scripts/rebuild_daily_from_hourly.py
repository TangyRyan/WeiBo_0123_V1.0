from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Iterable, List

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.config import ARCHIVE_DIR, HOURLY_DIR  # noqa: E402
from backend.storage import save_daily_archive  # noqa: E402
from spider.fetch_hot_topics import upsert_topic  # noqa: E402


def rebuild_for_date(date_str: str) -> None:
    date_dir = HOURLY_DIR / date_str
    if not date_dir.exists():
        raise FileNotFoundError(f"Hourly directory not found: {date_dir}")

    archive: dict = {}
    hours = sorted(hour_file for hour_file in date_dir.glob("*.json") if hour_file.stem.isdigit())
    if not hours:
        raise FileNotFoundError(f"No hourly snapshots found under {date_dir}")

    for hour_path in hours:
        hour = int(hour_path.stem)
        topics = json.loads(hour_path.read_text(encoding="utf-8"))
        for topic in topics:
            upsert_topic(archive, topic, date_str, hour)

    save_daily_archive(date_str, archive)
    print(f"Rebuilt daily archive for {date_str}: {len(archive)} topics -> {ARCHIVE_DIR / f'{date_str}.json'}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rebuild daily hot_topics archives from hourly JSON snapshots.")
    parser.add_argument("dates", nargs="+", help="Dates (YYYY-MM-DD) to rebuild")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    for date_str in args.dates:
        rebuild_for_date(date_str)


if __name__ == "__main__":
    main()
