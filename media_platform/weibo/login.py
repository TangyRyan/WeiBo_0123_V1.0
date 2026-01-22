"""Weibo login helper that outputs a Cookie header string."""

from __future__ import annotations

import argparse
import asyncio
import os
import shlex
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

from playwright.async_api import async_playwright


LOGIN_URL = (
    "https://passport.weibo.com/sso/signin?entry=miniblog&source=miniblog&disp=popup"
    "&url=https%3A%2F%2Fweibo.com%2Fnewlogin%3Ftabtype%3Dweibo%26gid%3D102803%26openLoginLayer%3D0"
    "%26url%3Dhttps%253A%252F%252Fweibo.com%252F"
)
LOGIN_COOKIE_KEYS = ("SSOLoginState", "SUB", "SUBP", "ALF")
DEFAULT_TIMEOUT_SECONDS = 180

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_USER_DATA_DIR = PROJECT_ROOT / "spider" / "browser_data_detail"
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"


@dataclass(frozen=True)
class LoginResult:
    cookie_str: str
    cookies: List[dict]


def _cookie_dict(cookies: Iterable[dict]) -> dict:
    result = {}
    for item in cookies:
        name = item.get("name")
        value = item.get("value")
        if name and value is not None:
            result[name] = str(value)
    return result


def _is_logged_in(cookies: Iterable[dict]) -> bool:
    cookie_map = _cookie_dict(cookies)
    return any(cookie_map.get(key) for key in LOGIN_COOKIE_KEYS)


def _filter_cookies(cookies: Iterable[dict], keywords: Sequence[str]) -> List[dict]:
    filtered: List[dict] = []
    for item in cookies:
        domain = (item.get("domain") or "").lower()
        if not domain or any(keyword in domain for keyword in keywords):
            filtered.append(item)
    return filtered


def build_cookie_string(cookies: Iterable[dict]) -> str:
    ordered: "OrderedDict[str, str]" = OrderedDict()
    for item in cookies:
        name = item.get("name")
        value = item.get("value")
        if not name or value is None:
            continue
        if name in ordered:
            ordered.pop(name)
        ordered[name] = str(value)
    return "; ".join(f"{name}={value}" for name, value in ordered.items())


async def _wait_for_login(context, timeout_seconds: int) -> List[dict]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        cookies = await context.cookies()
        if _is_logged_in(cookies):
            return cookies
        await asyncio.sleep(1)
    return []


async def login_and_get_cookie(
    *,
    user_data_dir: Path,
    headless: bool,
    timeout_seconds: int,
    domain_keywords: Optional[Sequence[str]] = None,
) -> LoginResult:
    """Open Weibo login page, wait for manual login, and return cookie string."""
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            headless=headless,
            args=["--disable-gpu", "--disable-dev-shm-usage", "--no-sandbox"],
        )
        try:
            cookies = await context.cookies()
            if not _is_logged_in(cookies):
                if headless:
                    raise RuntimeError(
                        "Headless mode requires existing login in the user data dir."
                    )
                page = await context.new_page()
                await page.goto(LOGIN_URL, wait_until="load", timeout=60000)
                cookies = await _wait_for_login(context, timeout_seconds)
                if not cookies:
                    raise RuntimeError("Login timed out. Please retry and scan/login in the browser.")

            keywords = domain_keywords or ("weibo", "sina")
            filtered = _filter_cookies(cookies, keywords)
            cookie_str = build_cookie_string(filtered)
            if not cookie_str:
                raise RuntimeError("No cookies captured. Please retry login.")

            return LoginResult(cookie_str=cookie_str, cookies=filtered)
        finally:
            await context.close()


def _write_env_file(path: Path, key: str, value: str) -> None:
    escaped = value.replace('"', '\\"')
    line = f'{key}="{escaped}"'
    if not path.exists():
        path.write_text(line + "\n", encoding="utf-8")
        return
    lines = path.read_text(encoding="utf-8").splitlines()
    updated = False
    for idx, raw in enumerate(lines):
        if raw.strip().startswith(f"{key}="):
            lines[idx] = line
            updated = True
            break
    if not updated:
        lines.append(line)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Weibo login helper (Playwright).")
    parser.add_argument(
        "--user-data-dir",
        default=str(DEFAULT_USER_DATA_DIR),
        help="Chromium persistent profile directory.",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run headless (only works if already logged in).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="Login wait timeout in seconds.",
    )
    parser.add_argument(
        "--env-name",
        default="WEIBO_COOKIE",
        help="Environment variable name to populate.",
    )
    parser.add_argument(
        "--write-env",
        action="store_true",
        help="Write the cookie into .env for future runs.",
    )
    parser.add_argument(
        "--env-path",
        default=str(DEFAULT_ENV_PATH),
        help="Path to .env if --write-env is set.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    result = asyncio.run(
        login_and_get_cookie(
            user_data_dir=Path(args.user_data_dir),
            headless=args.headless,
            timeout_seconds=args.timeout,
        )
    )

    os.environ[args.env_name] = result.cookie_str

    if args.write_env:
        _write_env_file(Path(args.env_path), args.env_name, result.cookie_str)

    print(f"{args.env_name}={result.cookie_str}")
    print(f"export {args.env_name}={shlex.quote(result.cookie_str)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
