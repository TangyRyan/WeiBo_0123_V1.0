#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.append(ROOT)

from spider.config import get_env_bool, get_env_list, get_env_str
from spider.cookie_manager import get_cookie, refresh_cookie_sync
from spider.notify_email import notify_cookie_invalid


def _flag(value: bool) -> str:
    return "yes" if value else "no"


def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnose cookie invalid email notification settings.")
    parser.add_argument(
        "--send-test",
        action="store_true",
        help="Trigger notify_cookie_invalid directly (will attempt to send email).",
    )
    parser.add_argument(
        "--refresh-cookie",
        action="store_true",
        help="Trigger login refresh flow (will attempt to send QR email).",
    )
    args = parser.parse_args()

    enabled = get_env_bool("WEIBO_EMAIL_NOTIFY_ENABLED", False)
    user_set = bool((get_env_str("WEIBO_EMAIL_USER") or "").strip())
    auth_set = bool((get_env_str("WEIBO_EMAIL_AUTH_CODE") or "").strip())
    to_list = get_env_list("WEIBO_EMAIL_TO")
    cookie_single = bool(get_cookie())

    print(f"email_notify_enabled={_flag(enabled)}")
    print(f"email_user_set={_flag(user_set)}")
    print(f"email_auth_code_set={_flag(auth_set)}")
    print(f"email_to_set={_flag(bool(to_list))}")
    print(f"weibo_cookie_set={_flag(cookie_single)}")

    if args.send_test:
        notify_cookie_invalid("manual_test", "manual_test")
        print("notify_cookie_invalid triggered (check logs)")

    if args.refresh_cookie:
        refreshed = refresh_cookie_sync("manual_test", notify_label="manual_refresh")
        print("refresh_cookie triggered" if refreshed else "refresh_cookie skipped/failed")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
