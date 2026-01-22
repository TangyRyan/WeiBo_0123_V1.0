from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate health-topic timeline snapshot (ingest → timeline → features → serializer)."
    )
    parser.add_argument("--date", help="Target date (YYYY-MM-DD) for archive lookup", default=None)
    parser.add_argument("--hours", help="Time window in hours", type=int, default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    from backend.health import refresh_health_snapshot

    payload = refresh_health_snapshot(target_date=args.date, hours=args.hours)
    event_count = len(payload.get("events", [])) if isinstance(payload, dict) else 0
    print(f"Health timeline generated with {event_count} events")


if __name__ == "__main__":
    main()
