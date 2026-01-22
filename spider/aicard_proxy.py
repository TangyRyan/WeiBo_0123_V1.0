"""集中处理 AI Card 的代理重写，确保 Markdown/HTML/媒体使用统一的代理前缀。"""

from __future__ import annotations

from typing import Any, Optional, Tuple

from backend.proxy import attach_proxy_to_media, rewrite_html_images, rewrite_markdown_images, rewrite_text_media_urls


def apply_proxy_to_card(
    markdown: Optional[str],
    html: Optional[str],
    media: Any,
    links: Any,
) -> Tuple[Optional[str], Optional[str], Any, Any]:
    """对 AI Card 的 Markdown、HTML、媒体与链接统一追加代理前缀。"""
    proxied_markdown = rewrite_markdown_images(markdown)
    if proxied_markdown is None:
        proxied_markdown = markdown
    proxied_markdown = rewrite_text_media_urls(proxied_markdown)
    if proxied_markdown is None:
        proxied_markdown = markdown

    proxied_html = rewrite_html_images(html)
    if proxied_html is None:
        proxied_html = html
    proxied_html = rewrite_text_media_urls(proxied_html)
    if proxied_html is None:
        proxied_html = html

    proxied_media = attach_proxy_to_media(media)
    proxied_links = attach_proxy_to_media(links, images_only=True)
    return proxied_markdown, proxied_html, proxied_media, proxied_links


__all__ = ["apply_proxy_to_card"]
