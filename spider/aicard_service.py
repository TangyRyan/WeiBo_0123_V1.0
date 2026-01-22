import logging
from dataclasses import asdict
from pathlib import Path
from typing import Dict, List, Optional

from spider.aicard_client import AICardCooldownError, AICardError, AICardRateLimitError, fetch_ai_card
from spider.aicard_parser import render_aicard_markdown
from spider.aicard_proxy import apply_proxy_to_card
from spider.crawler_core import ensure_hashtag_format, slugify_title
from backend.config import AICARD_DIR
from backend.storage import to_data_relative

BASE_DIR = AICARD_DIR
HOURLY_DIR_NAME = "hourly"


def _relative_to_repo(path: Path) -> str:
    return to_data_relative(path)


def ensure_aicard_snapshot(
    title: str,
    date_str: str,
    hour: int,
    *,
    slug: Optional[str] = None,
    base_dir: Path = BASE_DIR,
    logger: Optional[logging.Logger] = None,
) -> Optional[Dict[str, any]]:
    """生成 AI Card Markdown，并返回可直接写入归档的元数据。"""
    logger = logger or logging.getLogger(__name__)
    normalized_slug = slug or slugify_title(title)
    target_dir = base_dir / HOURLY_DIR_NAME / date_str / f"{hour:02d}"
    markdown_path = target_dir / f"{normalized_slug}.md"

    query = ensure_hashtag_format(title)
    try:
        result = fetch_ai_card(query)
    except AICardCooldownError:
        raise
    except AICardRateLimitError:
        raise
    except AICardError as exc:
        logger.warning("AI Card 获取失败：%s (%s)", title, exc)
        return None

    multimodal_data: List[Dict] = []
    card_multimodal = result.response.get("card_multimodal")
    if isinstance(card_multimodal, dict):
        data = card_multimodal.get("data")
        if isinstance(data, list):
            multimodal_data = data
        else:
            multimodal_data = [card_multimodal]
    elif isinstance(card_multimodal, list):
        multimodal_data = [item for item in card_multimodal if isinstance(item, dict)]

    share_multimodal = result.response.get("share_card_multimodal")
    if isinstance(share_multimodal, dict):
        multimodal_data.append(share_multimodal)
    elif isinstance(share_multimodal, list):
        multimodal_data.extend(item for item in share_multimodal if isinstance(item, dict))
    links = result.response.get("link_list")
    parsed = render_aicard_markdown(
        result.response.get("msg") or "",
        multimodal_data,
        links if isinstance(links, list) else None,
    )

    markdown_content, _, _, _ = apply_proxy_to_card(
        parsed.markdown,
        None,
        [asdict(asset) for asset in parsed.media],
        parsed.links,
    )

    target_dir.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text(markdown_content, encoding="utf-8")

    return {
        "slug": normalized_slug,
        "markdown_path": _relative_to_repo(markdown_path),
        "title": title,
        "fetched_at": result.fetched_at.isoformat(timespec="seconds"),
    }


__all__ = ["ensure_aicard_snapshot", "BASE_DIR"]
