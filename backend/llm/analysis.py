# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..config import OPENAI_API_KEY, OPENAI_BASE_URL, OPENAI_MODEL, REGION_LIST
from ..health.constants import EMOTION_DIMENSIONS
MAX_SAMPLE_POSTS = 20

try:
    import openai  # openai>=1.x
except Exception:  # pragma: no cover - optional dependency
    openai = None

logger = logging.getLogger(__name__)


@dataclass
class LLMResult:
    sentiment: float
    region: str
    topic_type: str
    health_major: Optional[str] = None
    health_minor: Optional[str] = None
    sentiment_vector: Dict[str, float] = field(default_factory=dict)
    raw_content: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    source: str = "llm"


SYSTEM_PROMPT = """你是一个中文舆情分析助手，服务于“微博舆情监测系统”的事件标注与检索。给定一个【事件】（对应微博热榜的一条目）及其若干条样本贴文/摘要，请完成以下任务，并且【只返回JSON】。
                    你需要为系统输出事件的核心属性，你的输出将用于系统的事件分布可视化（领域、情绪、地区、时间、风险值）、风险研判和专题分析等功能。
                【输入内容包含】
                - 事件名称与可选的AI总结/时间信息
                - 该事件下的若干条微博贴文样本（文本为主，可含地名、人名、机构、情绪词等线索）
                【输出字段（必须且仅限以下键）】
                - sentiment: 事件整体情绪分值，取值范围[-1, 1]（负面到正面）。建议：强烈负面≤-0.6；中度负面(-0.6,-0.2]；中性[-0.2,0.2]；中度正面(0.2,0.6)；强烈正面≥0.6。
                1)汇总多条样本的赞/骂/中性表达、表情/标点、情感词、负面风险词（冲突/事故/伤亡/维权等）与正向词（表彰/突破/公益等），面向“事件整体印象”给出单一分值。
                2)若样本分歧明显，以**量大/权重高**的主叙事为主，辅以强烈负面或正面信号微调。
                - region: 事件主要涉及地区（从下列35个选项中选择其一；若明显为境外则选“国外”）：北京, 天津, 上海, 重庆, 河北, 山西, 辽宁, 吉林, 黑龙江, 江苏, 浙江, 安徽, 福建, 江西, 山东, 河南, 湖北, 湖南, 广东, 海南, 四川, 贵州, 云南, 陕西, 甘肃, 青海, 台湾, 内蒙古, 广西, 西藏, 宁夏, 新疆, 香港, 澳门, 国外。
                  【地区判定优先级（严格按序）】
                1) 样本文本中明确提到事件本身发生的位置**明确地名**（省级/直辖/自治区/特别行政区/台湾），例如“在广东深圳”→广东；若出现多个中国省级地区，同时统计出现频次与语义权重（标题/主题/多条样本一致指向者权重更高），选择**出现频率最高或语义权重最高者**。
                2) 无明确省级地名时，依据**机构属地线索**（如“某省卫健委”“××大学附属医院（省域已知）”“××省交警总队”）回推出省级地区。
                3) 再无有效线索时，参考**事件上下文常识**（如“某沿海渔业政策”常见于沿海省份，但需谨慎）。
                4) “国外”仅在**明确为境外情形**（如文本含国家名/境外城市/境外机构）时选择；否则在中国34省级地区中选最可能者。
                5) 出现并列地区且难以区分时，优先：与事件起源/核心事故地点/发布主体属地**一致**者。
                6) 若无法判断时，请返回未知，避免主观臆断。
                - topic_type: 从下列离散主题中选择一类（单选）：娱乐/房产/时尚/动漫/美食/公益/历史/文学/健康/军事/汽车/旅行/游戏/交通/能源/农业/文化/财经/社会/体育/时政/教育/科技/未知。
                  · 当难以准确判断时选择“未知”，避免主观臆断。
                  · 注意：“健康” 是一个特殊领域，当涉及医疗、健康、医院、疾病、药品、疫苗、养生、心理等健康范畴时，优先判为“健康”；
                  如果你判断事件属于医疗健康类，请不要立即输出分类，而是将事件视为“健康类候选”，并调用健康领域专用分类 Prompt。
                - sentiment_vector: 事件情绪维度强度分布，覆盖8个固定维度（喜悦/信任/恐惧/惊喜/伤心/讨厌/气愤/期盼），每个值取[0,1]。
                  · 允许全部维度为0，但需结合样本语气给出合理分配；负向事件可在“伤心/讨厌/气愤”维度提高占比。
                  · 建议同时输出 sentiment_vector（字典）与 emotions（按 name/value 排列的列表），两个字段内容保持一致，便于下游解析。
                【健康专题的加注要求】
                - 若 topic_type == “健康”，请在返回JSON中额外包含两个键：
                  · health_major: 九大类之一（见下）
                  · health_minor: 对应二级小类之一（见下）
                - 健康九大类与二级小类（共9大类，21小类）：
                  1. 传染病与公共卫生应急：1.1 新发/突发传染病；1.2 已知传染病暴发；1.3 疫苗与防疫政策
                  2. 医疗服务与医患矛盾：2.1 恶性医患冲突；2.2 重大医疗事故；2.3 医疗资源与服务争议
                  3. 食品药品安全：3.1 食品安全突发情况；3.2 药品安全突发情况
                  4. 环境与灾害关联健康问题：4.1 突发污染健康影响；4.2 灾害次生健康风险
                  5. 特殊场景与群体健康：5.1 公共场合聚集性健康问题；5.2 特殊群体健康危机
                  6. 健康管理与养生：6.1 养生谣言与伪科学；6.2 慢病管理与健康干预；6.3 健康生活方式
                  7. 医疗行业与技术动态：7.1 医疗技术创新；7.2 医疗行业热点
                  8. 健康政策与公共服务：8.1 医疗保障政策；8.2 公共健康服务
                  9. 健康观念与社会现象：9.1 心理健康话题；9.2 健康相关社会争议
                【判定与约束】
                - 只能依据提供的文本信号做出标注，避免引入外部常识造成臆断。
                - 情绪应面向“事件总体舆论氛围”，综合多数贴文立场与语气；如明显多阵营对冲，可接近中性但不得返回区间或文本。
                - 地区与主题仅能单选；健康细分（health_major/health_minor）在且仅在 topic_type==“健康”时返回。
                - 最终必须返回一个JSON对象，示例：
                  {"sentiment": 0.18, "region": "????", "topic_type": "????", "health_major": "??????????????", "health_minor": "???????????????", "sentiment_vector": {"???": 0.05, "????": 0.12, "???": 0.20, "????": 0.0, "???": 0.30, "????": 0.15, "???": 0.25, "???": 0.08}}
                【输出格式】
                - 只输出JSON；不附加解释、不换行代码块、不带多余字段。"""

THINK_PATTERN = re.compile(r"<think>.*?</think>", flags=re.IGNORECASE | re.DOTALL)
CODE_FENCE_PATTERN = re.compile(r"```(?:json)?\s*(.*?)```", flags=re.IGNORECASE | re.DOTALL)
KV_PATTERN = re.compile(
    r"^\s*(sentiment|情绪|region|地区|topic_type|topic|类型)\s*[:：=]\s*(.+?)\s*$",
    flags=re.IGNORECASE,
)
SENTIMENT_WORD_MAP = {
    "positive": 0.6,
    "pos": 0.6,
    "positive emotion": 0.6,
    "正面": 0.6,
    "积极": 0.6,
    "neutral": 0.0,
    "neut": 0.0,
    "中性": 0.0,
    "negative": -0.6,
    "neg": -0.6,
    "负面": -0.6,
    "消极": -0.6,
}


def call_openai(posts: List[Dict[str, Any]], event_name: str) -> LLMResult:
    """Invoke the LLM (if configured) and coerce its reply into structured data."""
    if openai is None or not OPENAI_API_KEY:
        logger.warning(
            "LLM disabled or missing API key; using heuristic fallback for %s",
            event_name,
        )
        return _heuristic_inference(posts, event_name, source="heuristic:no_api")

    client_args = {"api_key": OPENAI_API_KEY}
    if OPENAI_BASE_URL:
        client_args["base_url"] = OPENAI_BASE_URL
    client = openai.OpenAI(**client_args)

    examples = []
    for post in posts[:MAX_SAMPLE_POSTS]:
        examples.append({
            "published_at": post.get("published_at"),
            "account_name": post.get("account_name"),
            "content_text": (post.get("content_text") or "")[:500],
            "reposts": post.get("reposts", 0),
            "comments": post.get("comments", 0),
            "likes": post.get("likes", 0),
        })

    user_prompt = {
        "event": event_name,
        "samples": examples,
        "region_candidates": REGION_LIST,
        "emotion_dimensions": EMOTION_DIMENSIONS,
    }
    logger.info("Requesting LLM analysis for %s with %s samples", event_name, len(examples))

    try:
        completion = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(user_prompt, ensure_ascii=False)},
            ],
            temperature=0.2,
        )
    except Exception as exc:  # pragma: no cover - network failures
        logger.exception("LLM request failed for %s: %s", event_name, exc)
        return _heuristic_inference(posts, event_name, source="heuristic:exception")

    content = completion.choices[0].message.content or ""
    payload, extracted = _structured_payload_from_content(content)
    if not payload:
        logger.warning("LLM response for %s lacked structured payload; falling back", event_name)
        return _heuristic_inference(posts, event_name, raw_content=content, source="heuristic:unparsed")

    result = _build_llm_result(payload, content)
    logger.info(
        "LLM parsed for %s: sentiment=%.2f region=%s topic=%s",
        event_name,
        result.sentiment,
        result.region,
        result.topic_type,
    )
    logger.debug("Structured payload for %s: %s", event_name, extracted)
    return result


def _heuristic_inference(
    posts: List[Dict[str, Any]],
    event_name: str,
    *,
    raw_content: str = "",
    source: str = "heuristic",
) -> LLMResult:
    text = " ".join((p.get("content_text") or "") for p in posts[:15])
    topic = "社会"
    sensitive_keywords = ["军机", "海警", "外交", "西沙", "涉政", "台独", "边境", "制裁", "战争", "冲突", "安全"]
    if any(keyword in text for keyword in sensitive_keywords):
        topic = "时政"
    sentiment = -0.15 if topic == "时政" else 0.0
    region = "未知"
    for prov in REGION_LIST:
        if prov and prov in text:
            region = prov
            break
    logger.debug(
        "Heuristic inference for %s -> sentiment=%.2f region=%s topic=%s",
        event_name,
        sentiment,
        region,
        topic,
    )
    return LLMResult(
        sentiment=sentiment,
        region=region,
        topic_type=topic,
        health_major=None,
        health_minor=None,
        sentiment_vector={},
        raw_content=raw_content,
        payload={"source": source},
        source=source,
    )


def _structured_payload_from_content(raw: str) -> Tuple[Dict[str, Any], str]:
    cleaned = _strip_reasoning(raw)
    for candidate in _iter_json_candidates(cleaned):
        data = _safe_json_dict(candidate)
        if data:
            return data, candidate
    kv_payload = _parse_key_value_lines(cleaned)
    if kv_payload:
        return kv_payload, cleaned
    return {}, cleaned


def _strip_reasoning(content: str) -> str:
    if not content:
        return ""
    return THINK_PATTERN.sub("", content).strip()


def _iter_json_candidates(content: str) -> Iterable[str]:
    seen = set()
    if content:
        cleaned = content.strip()
        if cleaned:
            seen.add(cleaned)
            yield cleaned
    for match in CODE_FENCE_PATTERN.finditer(content):
        candidate = (match.group(1) or "").strip()
        if candidate and candidate not in seen:
            seen.add(candidate)
            yield candidate
    for block in _extract_brace_blocks(content):
        trimmed = block.strip()
        if trimmed and trimmed not in seen:
            seen.add(trimmed)
            yield trimmed


def _extract_brace_blocks(content: str) -> List[str]:
    blocks: List[str] = []
    depth = 0
    start = None
    for idx, char in enumerate(content):
        if char == "{":
            if depth == 0:
                start = idx
            depth += 1
        elif char == "}":
            if depth:
                depth -= 1
                if depth == 0 and start is not None:
                    blocks.append(content[start:idx + 1])
                    start = None
    return blocks


def _parse_key_value_lines(content: str) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    for line in content.splitlines():
        match = KV_PATTERN.match(line.strip())
        if not match:
            continue
        key, value = match.groups()
        normalized_key = _normalize_key(key)
        payload[normalized_key] = value.strip().strip(",")
    return payload


def _normalize_key(key: str) -> str:
    lower = key.lower()
    if lower in {"sentiment", "情绪"}:
        return "sentiment"
    if lower in {"region", "地区"}:
        return "region"
    return "topic_type"


def _safe_json_dict(candidate: str) -> Dict[str, Any]:
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        return {}
    if isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, dict):
            return first
        return {}
    if isinstance(data, dict):
        return data
    return {}


def _coerce_sentiment(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        score = float(value)
    elif isinstance(value, str):
        candidate = value.strip()
        if not candidate:
            return 0.0
        try:
            score = float(candidate)
        except ValueError:
            score = SENTIMENT_WORD_MAP.get(candidate.lower(), 0.0)
    else:
        return 0.0
    return max(-1.0, min(1.0, score))


def _normalize_region(value: Any) -> str:
    if not value:
        return "未知"
    region = str(value).strip()
    if not region:
        return "未知"
    if region in REGION_LIST:
        return region
    core = region
    for suffix in ["省", "市", "壮族自治区", "维吾尔自治区", "回族自治区", "自治区"]:
        core = core.replace(suffix, "")
    for candidate in REGION_LIST:
        if candidate == "未知":
            continue
        if candidate in region or core in candidate:
            return candidate
    return "国外" if "国" in region and region not in REGION_LIST else "未知"


def _normalize_topic_type(value: Any) -> str:
    topic = str(value or "").strip()
    if not topic:
        return "其他"
    return topic


def _normalize_health_field(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _coerce_sentiment_vector(value: Any) -> Dict[str, float]:
    if not value:
        return {}
    vector: Dict[str, float] = {}
    if isinstance(value, dict):
        for key, raw in value.items():
            name = str(key).strip()
            if not name:
                continue
            try:
                vector[name] = float(raw)
            except (TypeError, ValueError):
                continue
        return vector
    if isinstance(value, list):
        for entry in value:
            if not isinstance(entry, dict):
                continue
            name = (
                entry.get("name")
                or entry.get("label")
                or entry.get("dimension")
                or entry.get("key")
            )
            if not name:
                continue
            try:
                score = float(entry.get("value") or entry.get("score"))
            except (TypeError, ValueError):
                continue
            normalized = str(name).strip()
            if normalized:
                vector[normalized] = score
        return vector
    return {}


def _build_llm_result(payload: Dict[str, Any], raw_content: str) -> LLMResult:
    sentiment = _coerce_sentiment(
        payload.get("sentiment")
        or payload.get("sentiment_score")
        or payload.get("score")
    )
    region = _normalize_region(
        payload.get("region") or payload.get("region_name") or payload.get("地区")
    )
    topic_type = _normalize_topic_type(
        payload.get("topic_type") or payload.get("topic") or payload.get("类型")
    )
    health_major = _normalize_health_field(
        payload.get("health_major")
        or payload.get("health_major_category")
        or payload.get("major")
    )
    health_minor = _normalize_health_field(
        payload.get("health_minor")
        or payload.get("health_minor_category")
        or payload.get("minor")
    )
    if topic_type != "健康":
        health_major = None
        health_minor = None
    sentiment_vector = _coerce_sentiment_vector(
        payload.get("sentiment_vector")
        or payload.get("emotions")
        or payload.get("emotion_vector")
    )
    return LLMResult(
        sentiment=sentiment,
        region=region,
        topic_type=topic_type,
        health_major=health_major,
        health_minor=health_minor,
        sentiment_vector=sentiment_vector,
        raw_content=raw_content,
        payload=payload,
        source="llm",
    )
