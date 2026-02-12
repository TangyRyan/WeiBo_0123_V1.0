from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional
from urllib.parse import quote, urlparse

from .settings import get_env_str

logger = logging.getLogger(__name__)

# Proxy path: keep browsers on our own host to avoid hotlink/referer issues.
PROXY_MEDIA_PATH = "/proxy/media"
LEGACY_PROXY_PATH = "/api/proxy/image"
# 基础代理域名通过环境变量提供，避免在代码中硬编码
PROXY_MEDIA_BASE = get_env_str("WEIBO_PROXY_MEDIA_BASE") or ""

ALLOWED_IMAGE_HOST_SUFFIXES = (
    "sinaimg.cn",
    "sinaimg.com.cn",
    "weibo.cn",
    "weibo.com",
    "weibocdn.com",
    "baidu.com",
    "image.baidu.com",
    "bdimg.com",
)

# Markdown/HTML image matching with optional title.
_MD_IMG_PATTERN = re.compile(r"!\[(?P<alt>[^\]]*)\]\(\s*(?P<url>[^\s)]+)(?P<title>\s+\"[^\"]*\")?\s*\)")
_HTML_IMG_PATTERN = re.compile(r'(<img\b[^>]*?\bsrc\s*=\s*)(["\'])([^"\']+)(\2)', re.IGNORECASE)
_PLAIN_URL_PATTERN = re.compile(r"(?:https?:)?//[^\s)\"'>]+")
_MEDIA_URL_KEYS = {
    "url",
    "secure_url",
    "original_url",
    "thumb",
    "thumbnail",
    "large",
    "middle",
    "small",
    "bmiddle",
    "cover",
    "cover_image",
    "poster",
    "image",
    "pic",
    "gif",
    "video",
    "stream",
    "hd_url",
    "sd_url",
    "tiny",
    "wap360",
    "wap720",
    "source",
}
IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".bmp",
    ".webp",
    ".svg",
    ".jfif",
    ".pjpeg",
    ".pjp",
    ".heic",
    ".heif",
    ".avif",
}
VIDEO_EXTENSIONS = {
    ".mp4",
    ".m3u8",
    ".mov",
    ".avi",
    ".flv",
    ".mkv",
    ".webm",
}
MEDIA_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS


def _strip_text(value: Any) -> str:
    return str(value or "").strip()


def normalize_image_url(raw_url: str) -> str:
    """Normalize URLs so they can be validated or proxied."""
    url = _strip_text(raw_url)
    if not url:
        return ""
    if url.startswith((PROXY_MEDIA_PATH, LEGACY_PROXY_PATH)):
        return url
    if url.startswith("//"):
        url = "https:" + url
    parsed = urlparse(url)
    if not parsed.scheme and parsed.netloc:
        url = f"https://{url}"
    elif not parsed.scheme and not parsed.netloc and url:
        if not url.startswith(("http://", "https://")):
            url = "https://" + url.lstrip("/")
    return url


def _proxy_prefix() -> str:
    if PROXY_MEDIA_BASE:
        return PROXY_MEDIA_BASE.rstrip("/") + PROXY_MEDIA_PATH
    return PROXY_MEDIA_PATH


def _is_already_proxied(url: str) -> bool:
    prefix = _proxy_prefix()
    return url.startswith(prefix) or url.startswith(PROXY_MEDIA_PATH) or url.startswith(LEGACY_PROXY_PATH)


def is_allowed_image_host(url: str) -> bool:
    """Restrict to known Weibo/Baidu hosts so we are not an open proxy."""
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return False
    host = host.lower()
    return any(host == suffix or host.endswith(f".{suffix}") for suffix in ALLOWED_IMAGE_HOST_SUFFIXES)


def _should_proxy_image(normalized: str, images_only: bool) -> bool:
    if not images_only:
        return True
    try:
        path = urlparse(normalized).path.lower()
    except ValueError:
        return False
    return any(path.endswith(ext) for ext in IMAGE_EXTENSIONS)


def is_media_url(raw_url: str) -> bool:
    """Check if a URL looks like a media file (image/video) by extension."""
    try:
        normalized = normalize_image_url(raw_url)
    except ValueError:
        return False
    if not normalized or _is_already_proxied(normalized):
        return False
    try:
        path = urlparse(normalized).path.lower()
    except ValueError:
        return False
    return any(path.endswith(ext) for ext in MEDIA_EXTENSIONS)


def build_proxy_media_url(raw_url: str, *, images_only: bool = False) -> str:
    """Wrap external media URLs with the proxy path."""
    try:
        normalized = normalize_image_url(raw_url)
    except ValueError:
        return raw_url
    if not normalized or normalized.startswith("data:"):
        return normalized
    if PROXY_MEDIA_BASE and normalized.startswith((PROXY_MEDIA_PATH, LEGACY_PROXY_PATH)):
        return PROXY_MEDIA_BASE.rstrip("/") + normalized
    if _is_already_proxied(normalized):
        return normalized
    if not normalized.startswith(("http://", "https://")):
        return normalized
    if not is_allowed_image_host(normalized):
        return normalized
    if not _should_proxy_image(normalized, images_only):
        return normalized
    return f"{_proxy_prefix()}?url={quote(normalized, safe='')}"


# Backward compatible name used elsewhere.
build_proxy_image_url = build_proxy_media_url


def _rewrite_url(url: str, *, images_only: bool = False) -> str:
    return build_proxy_media_url(url, images_only=images_only)


def rewrite_markdown_images(content: Optional[str]) -> Optional[str]:
    """Rewrite Markdown image URLs to route through the proxy."""
    if not content:
        return content

    def _repl(match: re.Match[str]) -> str:
        alt_text = match.group("alt")
        url = match.group("url")
        title = match.group("title") or ""
        proxied = _rewrite_url(url)
        return f"![{alt_text}]({proxied}{title})"

    return _MD_IMG_PATTERN.sub(_repl, content)


def rewrite_html_images(content: Optional[str]) -> Optional[str]:
    """Rewrite <img> src attributes to route through the proxy."""
    if not content:
        return content

    def _repl(match: re.Match[str]) -> str:
        prefix, quote_char, url, _ = match.groups()
        proxied = _rewrite_url(url)
        return f"{prefix}{quote_char}{proxied}{quote_char}"

    return _HTML_IMG_PATTERN.sub(_repl, content)


def rewrite_text_media_urls(content: Optional[str]) -> Optional[str]:
    """Rewrite plain media URLs in text to route through the proxy."""
    if not content:
        return content

    def _repl(match: re.Match[str]) -> str:
        url = match.group(0)
        if not is_media_url(url):
            return url
        return build_proxy_media_url(url, images_only=False)

    return _PLAIN_URL_PATTERN.sub(_repl, content)


def _looks_like_media_url(value: str) -> bool:
    text = _strip_text(value)
    return bool(text) and text.startswith(("http://", "https://", "//"))


def _rewrite_media_mapping(data: Dict[str, Any], *, images_only: bool) -> Dict[str, Any]:
    rewritten: Dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, dict):
            rewritten[key] = _rewrite_media_mapping(value, images_only=images_only)
            continue
        if isinstance(value, list):
            rewritten[key] = [_rewrite_media_value(item, images_only=images_only) for item in value]
            continue
        if isinstance(value, tuple):
            rewritten[key] = tuple(_rewrite_media_value(item, images_only=images_only) for item in value)
            continue
        key_lower = key.lower()
        looks_like_url = isinstance(value, str) and _looks_like_media_url(value)
        if key_lower in _MEDIA_URL_KEYS or "url" in key_lower or looks_like_url:
            rewritten[key] = _rewrite_url(value, images_only=images_only) if isinstance(value, str) else value
        else:
            rewritten[key] = value
    return rewritten


def _rewrite_media_value(value: Any, *, images_only: bool) -> Any:
    if isinstance(value, dict):
        return _rewrite_media_mapping(value, images_only=images_only)
    if isinstance(value, list):
        return [_rewrite_media_value(item, images_only=images_only) for item in value]
    if isinstance(value, tuple):
        return tuple(_rewrite_media_value(item, images_only=images_only) for item in value)
    if isinstance(value, str) and _looks_like_media_url(value):
        return _rewrite_url(value, images_only=images_only)
    return value


def attach_proxy_to_media(media: Any, *, images_only: bool = False) -> Any:
    """Deep-copy media payloads and rewrite external URLs to our proxy.
    Frontend renders `/proxy/media?url=...` directly and does not need to know
    Sina/Baidu hosts.
    """
    if media is None:
        return None
    return _rewrite_media_value(media, images_only=images_only)


__all__ = [
    "ALLOWED_IMAGE_HOST_SUFFIXES",
    "PROXY_MEDIA_BASE",
    "IMAGE_EXTENSIONS",
    "VIDEO_EXTENSIONS",
    "MEDIA_EXTENSIONS",
    "normalize_image_url",
    "is_allowed_image_host",
    "is_media_url",
    "build_proxy_image_url",
    "build_proxy_media_url",
    "rewrite_markdown_images",
    "rewrite_html_images",
    "rewrite_text_media_urls",
    "attach_proxy_to_media",
    "PROXY_MEDIA_PATH",
    "LEGACY_PROXY_PATH",
]
