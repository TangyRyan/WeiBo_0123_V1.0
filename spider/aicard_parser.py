import html
import logging
import re
from dataclasses import dataclass
from typing import Any, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

_THINK_PATTERN = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)
_BLOCK_PATTERN = re.compile(r"```wbCustomBlock\s*({.*?})\s*```", re.IGNORECASE | re.DOTALL)
_MEDIA_PATTERN = re.compile(r"<media-block>.*?</media-block>", re.IGNORECASE | re.DOTALL)
_BR_PATTERN = re.compile(r"<br\s*/?>", re.IGNORECASE)
_BOLD_PATTERN = re.compile(r"\*\*(.+?)\*\*")
_HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.*)$")
_BLOCKQUOTE_PATTERN = re.compile(r"^>\s?(.*)$")

_DIV_PATTERN = re.compile(r"<div[^>]*?>.*?</div>", re.IGNORECASE | re.DOTALL)
_IMG_PATTERN = re.compile(r'<img[^>]*src="([^"]+)"[^>]*>', re.IGNORECASE)
_NICK_PATTERN = re.compile(
    r'<span[^>]*class="[^"]*nick[^"]*"[^>]*>(.*?)</span>', re.IGNORECASE | re.DOTALL
)

_IMAGE_HOSTS = ("wx1.sinaimg.cn", "wx2.sinaimg.cn", "wx3.sinaimg.cn", "wx4.sinaimg.cn")


@dataclass(slots=True)
class MediaAsset:
    original_url: str
    secure_url: str
    alt: Optional[str]
    width: Optional[int]
    height: Optional[int]
    media_type: str
    mirrors: List[str]
    user: Optional[str] = None


@dataclass(slots=True)
class ParsedCard:
    markdown: str
    html: str
    media: List[MediaAsset]
    links: List[str]


def render_aicard_markdown(
    raw_msg: str,
    multimodal: Optional[Sequence[Mapping[str, Any]]] = None,
    links: Optional[Sequence[str]] = None,
) -> ParsedCard:
    segments = _split_text_and_media(raw_msg or "")
    html_parts: List[str] = []
    markdown_parts: List[str] = []
    gathered_media: List[MediaAsset] = []

    for kind, payload in segments:
        if kind == "text":
            text_html, text_markdown = _render_text_segments(payload)
            html_parts.extend(text_html)
            markdown_parts.extend(text_markdown)
        elif kind == "media":
            assets = _convert_media(payload)
            if assets:
                gallery_html, gallery_markdown = _render_media_gallery(assets)
                html_parts.append(gallery_html)
                markdown_parts.extend(gallery_markdown)
                gathered_media.extend(assets)

    extra_media = _convert_media(multimodal or [])
    seen_urls = {asset.secure_url for asset in gathered_media if asset.secure_url}
    extra_media = [asset for asset in extra_media if asset.secure_url and asset.secure_url not in seen_urls]
    if extra_media:
        gallery_html, gallery_markdown = _render_media_gallery(extra_media)
        html_parts.append(gallery_html)
        markdown_parts.extend(gallery_markdown)
        gathered_media.extend(extra_media)

    html_output = "\n".join(html_parts)
    markdown_output = _join_markdown_lines(markdown_parts)
    return ParsedCard(markdown=markdown_output, html=html_output, media=gathered_media, links=list(links or []))


def _split_text_and_media(raw_msg: str) -> List[Tuple[str, Any]]:
    without_think = _THINK_PATTERN.sub("", raw_msg)
    segments: List[Tuple[str, Any]] = []
    cursor = 0
    for match in _MEDIA_PATTERN.finditer(without_think):
        start, end = match.span()
        if start > cursor:
            text_slice = without_think[cursor:start]
            stripped = _strip_markup(text_slice)
            if stripped.strip():
                segments.append(("text", stripped))
        block = match.group(0)
        candidates = _parse_media_block(block)
        if candidates:
            segments.append(("media", candidates))
        cursor = end
    if cursor < len(without_think):
        tail = _strip_markup(without_think[cursor:])
        if tail.strip():
            segments.append(("text", tail))
    if not segments:
        segments.append(("text", _strip_markup(without_think)))
    return segments


def _strip_markup(fragment: str) -> str:
    text = _BLOCK_PATTERN.sub("", fragment)
    text = _BR_PATTERN.sub("\n", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return html.unescape(text)


def _render_text_segments(text: str) -> Tuple[List[str], List[str]]:
    lines = [line.rstrip() for line in text.split("\n")]
    html_parts: List[str] = []
    markdown_lines: List[str] = []
    in_ul = False
    in_ol = False

    def close_lists() -> None:
        nonlocal in_ul, in_ol
        if in_ul:
            html_parts.append("</ul>")
            if markdown_lines and markdown_lines[-1] != "":
                markdown_lines.append("")
            in_ul = False
        if in_ol:
            html_parts.append("</ol>")
            if markdown_lines and markdown_lines[-1] != "":
                markdown_lines.append("")
            in_ol = False

    def ensure_blank_line() -> None:
        if markdown_lines and markdown_lines[-1] != "":
            markdown_lines.append("")

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            close_lists()
            ensure_blank_line()
            continue

        heading_match = _HEADING_PATTERN.match(line)
        if heading_match:
            close_lists()
            ensure_blank_line()
            level = min(len(heading_match.group(1)), 4)
            content = heading_match.group(2).strip()
            html_parts.append(f"<h{level}>{_apply_inline_markup(content)}</h{level}>")
            markdown_lines.append(f"{'#' * level} {content}".rstrip())
            markdown_lines.append("")
            continue

        ordered_match = re.match(r"^(\d+)\.\s+(.*)", line)
        if ordered_match:
            if not in_ol:
                close_lists()
                ensure_blank_line()
                html_parts.append("<ol>")
                in_ol = True
            content = ordered_match.group(2).strip()
            html_parts.append(f"<li>{_apply_inline_markup(content)}</li>")
            markdown_lines.append(f"{ordered_match.group(1)}. {content}")
            continue

        if line.startswith(("- ", "* ")):
            if not in_ul:
                close_lists()
                ensure_blank_line()
                html_parts.append("<ul>")
                in_ul = True
            content = line[2:].strip()
            html_parts.append(f"<li>{_apply_inline_markup(content)}</li>")
            markdown_lines.append(f"- {content}")
            continue

        blockquote_match = _BLOCKQUOTE_PATTERN.match(line)
        if blockquote_match:
            close_lists()
            ensure_blank_line()
            html_parts.append(
                f"<blockquote><p>{_apply_inline_markup(blockquote_match.group(1).strip())}</p></blockquote>"
            )
            markdown_lines.append(f"> {blockquote_match.group(1).strip()}")
            markdown_lines.append("")
            continue

        close_lists()
        ensure_blank_line()
        html_parts.append(f"<p>{_apply_inline_markup(line)}</p>")
        markdown_lines.append(line)
        markdown_lines.append("")

    close_lists()
    return html_parts, markdown_lines


def _join_markdown_lines(lines: Sequence[str]) -> str:
    cleaned: List[str] = []
    previous_blank = True
    for raw in lines:
        if raw is None:
            continue
        line = raw.rstrip()
        if not line:
            if not previous_blank and cleaned:
                cleaned.append("")
            previous_blank = True
            continue
        cleaned.append(line)
        previous_blank = False
    while cleaned and cleaned[-1] == "":
        cleaned.pop()
    return "\n".join(cleaned)


def _apply_inline_markup(text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        inner = match.group(1)
        return f"<strong>{_escape_inline(inner)}</strong>"

    escaped = _escape_inline(text)
    return _BOLD_PATTERN.sub(repl, escaped)


def _escape_inline(text: str) -> str:
    return html.escape(text, quote=False)


def _convert_media(multimodal: Sequence[Mapping[str, Any]]) -> List[MediaAsset]:
    assets: List[MediaAsset] = []
    for index, item in enumerate(multimodal):
        original = str(item.get("img") or item.get("image") or "").strip()
        img_pid = str(item.get("img_pid") or "").strip() or None
        secure = _ensure_https(original)
        if not secure and img_pid:
            secure = _build_pid_url(img_pid, index)
        if not secure:
            continue
        width = _try_int(item.get("w") or item.get("width"))
        height = _try_int(item.get("h") or item.get("height"))
        alt = _deduce_alt(item)
        mirrors: List[str] = []
        if img_pid:
            mirror = _build_pid_url(img_pid, index)
            if mirror and mirror != secure:
                mirrors.append(mirror)
        assets.append(
            MediaAsset(
                original_url=original or secure,
                secure_url=secure,
                alt=alt,
                width=width,
                height=height,
                media_type=str(item.get("type") or "image"),
                mirrors=mirrors,
                user=str(item.get("user_name") or "") or None,
            )
        )
    return assets


def _ensure_https(url: str) -> str:
    url = url.strip()
    if not url:
        return ""
    if url.startswith("https://"):
        return url
    if url.startswith("//"):
        return f"https:{url}"
    if url.startswith("http://"):
        return "https://" + url[len("http://") :]
    if url.startswith(("https:", "http:")):
        return "https:" + url.split(":", 1)[1]
    if url.startswith(("wx", "tvax")):
        return f"https://{url}"
    return url


def _build_pid_url(img_pid: str, seed: int) -> str:
    if not img_pid:
        return ""
    host = _IMAGE_HOSTS[seed % len(_IMAGE_HOSTS)]
    return f"https://{host}/large/{img_pid}.jpg"


def _try_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _deduce_alt(item: Mapping[str, Any]) -> Optional[str]:
    for field in ("user_name", "title", "type"):
        value = item.get(field)
        if value:
            return str(value)
    return None


def _render_media_gallery(assets: Sequence[MediaAsset]) -> Tuple[str, List[str]]:
    figures: List[str] = []
    markdown_lines: List[str] = []
    for asset in assets:
        attrs = ['loading="lazy"']
        if asset.width:
            attrs.append(f'data-width="{asset.width}"')
        if asset.height:
            attrs.append(f'data-height="{asset.height}"')
        alt = html.escape(asset.alt or asset.user or asset.media_type or "", quote=True)
        attrs.append(f'alt="{alt}"')
        if asset.mirrors:
            mirrors = " ".join(asset.mirrors)
            attrs.append(f'data-mirrors="{html.escape(mirrors, quote=True)}"')
        src = html.escape(asset.secure_url, quote=True)
        caption = asset.alt or asset.user
        figcaption = f"<figcaption>{html.escape(caption)}</figcaption>" if caption else ""
        figures.append(
            '<figure class="aicard-media">'
            f'<img src="{src}" {" ".join(attrs)}>'
            f"{figcaption}</figure>"
        )
        alt_text = asset.alt or asset.user or asset.media_type or "媒体"
        markdown_lines.append(f"![{_escape_markdown_alt(alt_text)}]({asset.secure_url or asset.original_url})")
        meta_parts: List[str] = []
        if asset.user:
            meta_parts.append(f"作者：{asset.user}")
        if asset.media_type and asset.media_type.lower() != "image":
            meta_parts.append(f"类型：{asset.media_type}")
        if asset.mirrors:
            meta_parts.append("备用：" + ", ".join(asset.mirrors))
        if meta_parts:
            markdown_lines.append("> " + " ｜ ".join(meta_parts))
        markdown_lines.append("")
    gallery_class = "aicard-media-gallery"
    if len(assets) == 1:
        gallery_class += " single"
    html_gallery = f"<div class=\"{gallery_class}\">\n" + "\n".join(figures) + "\n</div>"
    return html_gallery, markdown_lines


def _parse_media_block(block: str) -> List[Mapping[str, Any]]:
    candidates: List[Mapping[str, Any]] = []
    for div_match in _DIV_PATTERN.finditer(block):
        segment = div_match.group(0)
        img_match = _IMG_PATTERN.search(segment)
        if not img_match:
            continue
        src = html.unescape(img_match.group(1))
        width = _extract_attr(segment, "data-width") or _extract_attr(segment, "width")
        height = _extract_attr(segment, "data-height") or _extract_attr(segment, "height")
        media_type = _extract_attr(div_match.group(0), "data-type") or "image"
        img_pid = _extract_attr(segment, "data-pid") or _guess_pid_from_url(src)
        nick_match = _NICK_PATTERN.search(segment)
        nick = html.unescape(nick_match.group(1).strip()) if nick_match else None
        candidates.append(
            {
                "img": src,
                "img_pid": img_pid,
                "w": width,
                "h": height,
                "type": media_type,
                "user_name": nick,
            }
        )
    return candidates


def _extract_attr(fragment: str, attr: str) -> Optional[str]:
    match = re.search(rf'{attr}\s*=\s*"([^"]+)"', fragment, re.IGNORECASE)
    if match:
        return match.group(1)
    match = re.search(rf"{attr}\s*=\s*([^>\s]+)", fragment, re.IGNORECASE)
    if match:
        return match.group(1).strip("\"'")
    return None


def _guess_pid_from_url(url: str) -> Optional[str]:
    match = re.search(r"/([A-Za-z0-9]+)\.(?:jpg|jpeg|png|gif)", url)
    if match:
        return match.group(1)
    return None


def _escape_markdown_alt(text: str) -> str:
    if not text:
        return ""
    replacements = {
        "\\": "\\\\",
        "[": "\\[",
        "]": "\\]",
        "(": "\\(",
        ")": "\\)",
    }
    escaped = text
    for src, target in replacements.items():
        escaped = escaped.replace(src, target)
    return escaped


__all__ = ["render_aicard_markdown", "ParsedCard", "MediaAsset"]
