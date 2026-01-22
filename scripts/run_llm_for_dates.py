from __future__ import annotations

import argparse
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.scheduler import daily_llm_update  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run daily LLM analysis for specific dates.")
    parser.add_argument("dates", nargs="+", help="Dates (YYYY-MM-DD) to analyze")
    parser.add_argument("--force", action="store_true", help="Force rerun even if already analyzed")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    for date_str in args.dates:
        daily_llm_update(target_date=date_str, force=args.force)


if __name__ == "__main__":
    main()
