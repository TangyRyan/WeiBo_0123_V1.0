import argparse
import logging
from pathlib import Path
from typing import Any, Dict, List, Mapping
from urllib.parse import unquote_plus

from spider.aicard_client import AICardError, AICardResult, fetch_ai_card
from spider.aicard_parser import MediaAsset, ParsedCard, render_aicard_markdown
from spider.aicard_proxy import apply_proxy_to_card
from spider.crawler_core import slugify_title
from backend.config import AICARD_DIR
from backend.storage import write_json

DEFAULT_OUTPUT_DIR = AICARD_DIR


def _derive_topic_slug(result: AICardResult) -> str:
    raw_query = result.response.get("query") or result.query
    clean_query = unquote_plus(str(raw_query))
    return slugify_title(clean_query or "aicard-topic")


def _serialize_media(asset: MediaAsset) -> Dict[str, Any]:
    return {
        "original_url": asset.original_url,
        "secure_url": asset.secure_url,
        "alt": asset.alt,
        "width": asset.width,
        "height": asset.height,
        "media_type": asset.media_type,
        "mirrors": asset.mirrors,
        "user": asset.user,
    }


def _persist_outputs(
    result: AICardResult,
    parsed: ParsedCard,
    output_dir: Path,
) -> Dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    slug = _derive_topic_slug(result)
    title = unquote_plus(result.response.get("query") or result.query)
    markdown_path = output_dir / f"{slug}.md"
    json_path = output_dir / f"{slug}.json"

    markdown_content, _, proxied_media, proxied_links = apply_proxy_to_card(
        parsed.markdown,
        None,
        [_serialize_media(item) for item in parsed.media],
        parsed.links,
    )

    markdown_path.write_text(markdown_content, encoding="utf-8")
    payload = {
        "meta": result.to_dict(),
        "links": proxied_links,
        "media": proxied_media,
        "markdown_path": str(markdown_path),
    }
    write_json(json_path, payload)
    return {"markdown": markdown_path, "json": json_path}


def _collect_multimodal_entries(payload: Mapping[str, Any]) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []

    def _append_candidate(candidate: Any) -> None:
        if isinstance(candidate, dict) and candidate:
            entries.append(candidate)

    card_multimodal = payload.get("card_multimodal")
    if isinstance(card_multimodal, dict):
        data = card_multimodal.get("data")
        if isinstance(data, list):
            for item in data:
                _append_candidate(item)
        else:
            _append_candidate(card_multimodal)
    elif isinstance(card_multimodal, list):
        for item in card_multimodal:
            _append_candidate(item)

    share_multimodal = payload.get("share_card_multimodal")
    if isinstance(share_multimodal, dict):
        _append_candidate(share_multimodal)
    elif isinstance(share_multimodal, list):
        for item in share_multimodal:
            _append_candidate(item)

    return entries


def run(query: str, output_dir: Path) -> Dict[str, Path]:
    logging.info("Fetching AI card for %s", query)
    result = fetch_ai_card(query)
    logger_payload = result.response.get("status_stage")
    logging.debug("AI card status_stage=%s", logger_payload)
    raw_message = str(result.response.get("msg") or "")
    card_multimodal = _collect_multimodal_entries(result.response)

    links = result.response.get("link_list")
    if not isinstance(links, list):
        links = []
    parsed = render_aicard_markdown(raw_message, card_multimodal, links)
    return _persist_outputs(result, parsed, output_dir)


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch and render Weibo AI Card content.")
    parser.add_argument("query", help="原始查询词（例如：#中方回应巴基斯坦向美国赠送稀土#）")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="结果输出目录（默认 data/aicard/，输出 Markdown 文件）",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="日志级别 (DEBUG, INFO, WARNING, ...)，默认 INFO",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    try:
        outputs = run(args.query, args.output_dir)
    except AICardError as exc:
        logging.error("AI card 请求失败：%s", exc)
        raise SystemExit(1) from exc

    logging.info("Markdown 输出：%s", outputs["markdown"])
    logging.info("JSON 输出：%s", outputs["json"])


if __name__ == "__main__":
    main()
