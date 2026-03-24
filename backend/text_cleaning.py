from __future__ import annotations

import re
from typing import Any

# 微博抓取文本常在末尾带“展开/展开c”，仅用于前端展示时替换为省略号。
_TAIL_EXPAND_PATTERN = re.compile(r"[\s\u00a0\u200b\ufeff]*展开c?\s*$")


def normalize_post_excerpt_tail(text: Any) -> Any:
    """Replace trailing '展开' / '展开c' marker with ellipsis for display."""
    if not isinstance(text, str):
        return text
    if not _TAIL_EXPAND_PATTERN.search(text):
        return text
    return _TAIL_EXPAND_PATTERN.sub("……", text)


def normalize_posts_for_display(posts: Any) -> Any:
    """Return a shallow-copied posts list with cleaned content_text."""
    if not isinstance(posts, list):
        return posts
    normalized = []
    for post in posts:
        if not isinstance(post, dict):
            normalized.append(post)
            continue
        item = dict(post)
        item["content_text"] = normalize_post_excerpt_tail(item.get("content_text"))
        normalized.append(item)
    return normalized
