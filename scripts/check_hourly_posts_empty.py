from __future__ import annotations

import argparse
import json
import logging
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.append(ROOT)

from spider.post_health import check_hourly_posts_empty  # noqa: E402


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check posts payloads for empty items.")
    parser.add_argument("--date", required=True, help="Date in YYYY-MM-DD format.")
    parser.add_argument("--hour", required=True, type=int, help="Hour in 0-23.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", help="Only report counts; do not trigger login.")
    group.add_argument("--trigger", action="store_true", help="Trigger login/email if threshold reached.")
    return parser


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = _build_parser()
    args = parser.parse_args()

    hour = args.hour
    if hour < 0 or hour > 23:
        raise SystemExit("hour must be between 0 and 23")

    trigger = bool(args.trigger)
    result = check_hourly_posts_empty(
        args.date,
        hour,
        trigger=trigger,
        async_mode=False,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
