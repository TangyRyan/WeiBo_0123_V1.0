import json
import re
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backend.proxy import (
    LEGACY_PROXY_PATH,
    PROXY_MEDIA_PATH,
    build_proxy_media_url,
    is_media_url,
)

URL_PATTERN_MD = re.compile(
    r"(?:https?:)?//[^\s)\"'>]+|/(?:proxy/media|api/proxy/image)\?[^\s)\"'>]+"
)
DATE_DIR_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}$")


def process_md(path: Path) -> int:
    text = path.read_text(encoding="utf-8")
    count = 0

    def repl(match: re.Match) -> str:
        nonlocal count
        url = match.group(0)
        if url.startswith((PROXY_MEDIA_PATH, LEGACY_PROXY_PATH)):
            upgraded = build_proxy_media_url(url, images_only=True)
            if upgraded != url:
                count += 1
            return upgraded
        if not is_media_url(url):
            return url
        proxied = build_proxy_media_url(url, images_only=True)
        if proxied != url:
            count += 1
            return proxied
        return url

    new_text = URL_PATTERN_MD.sub(repl, text)
    if count:
        path.write_text(new_text, encoding="utf-8")
    return count


def walk_json(value, counter):
    if isinstance(value, str):
        if value.startswith((PROXY_MEDIA_PATH, LEGACY_PROXY_PATH)):
            proxied = build_proxy_media_url(value, images_only=True)
            if proxied != value:
                counter[0] += 1
            return proxied
        if value.startswith(("http://", "https://", "//")):
            if not is_media_url(value):
                return value
            try:
                proxied = build_proxy_media_url(value, images_only=True)
            except Exception:
                return value
            if proxied != value:
                counter[0] += 1
                return proxied
        return value
    if isinstance(value, list):
        return [walk_json(v, counter) for v in value]
    if isinstance(value, dict):
        return {k: walk_json(v, counter) for k, v in value.items()}
    return value


def process_json(path: Path) -> int:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"跳过无法解析的 JSON: {path} ({exc})")
        return 0
    counter = [0]
    new_data = walk_json(data, counter)
    if counter[0]:
        path.write_text(
            json.dumps(new_data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return counter[0]


def collect_targets(paths):
    md_files = []
    json_files = []
    for raw in paths:
        p = Path(raw)
        if not p.exists():
            print(f"跳过不存在路径: {p}")
            continue
        if p.is_dir():
            if "aicard" in p.parts:
                md_files.extend(p.rglob("*.md"))
            if "posts" in p.parts:
                json_files.extend(p.rglob("*.json"))
        else:
            if p.suffix.lower() == ".md":
                md_files.append(p)
            if p.suffix.lower() == ".json":
                json_files.append(p)
    return sorted(md_files), sorted(json_files)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("用法: python scripts/proxyize_links.py <路径...>")
        return 1

    md_files, json_files = collect_targets(argv[1:])
    md_count = sum(process_md(f) for f in md_files)
    json_count = sum(process_json(f) for f in json_files)
    print(f"Markdown 替换 {md_count} 条，JSON 替换 {json_count} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
