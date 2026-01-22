
from __future__ import annotations
from typing import Dict, Any, List
from .config import RISK_WEIGHTS, HIGH_SENSITIVE,MEDIUM_SENSITIVE,LOW_SENSITIVE

def clamp(v, lo=0.0, hi=100.0):
    return max(lo, min(hi, v))

def calc_negativity(sentiment: float) -> float:
    # -1(最负) -> 100, +1(最正) -> 0
    return clamp(((-sentiment + 1) / 2) * 100.0)

def calc_growth(current_hot: float, prev_hot: float) -> float:
    if current_hot is None or prev_hot is None:
        return 50.0
    if prev_hot <= 0:
        return 100.0
    ratio = (current_hot - prev_hot) / prev_hot
    score = 50 + 50 * max(-1.0, min(1.0, ratio))
    return clamp(score)

def calc_sensitivity(topic_type: str) -> float:
    if topic_type in HIGH_SENSITIVE:
        return 85.0
    elif topic_type in MEDIUM_SENSITIVE:
        return 60.0
    else:
        return 40.0

def calc_crowd(posts: List[Dict[str, Any]]) -> float:
    total = 0
    for p in posts:
        total += p.get("reposts",0) + p.get("comments",0) + p.get("likes",0)
    import math
    if total <= 0: return 20.0
    return clamp(min(100.0, 20.0 + 20.0 * math.log10(1 + total)))

def aggregate_score(dims: Dict[str, float]) -> float:
    s = 0.0
    for k, w in RISK_WEIGHTS.items():
        s += w * clamp(dims.get(k, 0.0))
    return clamp(s)

RISK_LEVEL_LABELS = {
    "low": "低风险",
    "mid": "中风险",
    "high": "高风险",
}


def risk_level_from_score(score: float | None) -> str:
    """Return qualitative level key for the supplied risk score."""
    normalized = clamp(float(score or 0.0))
    if normalized >= 50.0:
        return "high"
    if normalized >= 20.0:
        return "mid"
    return "low"


def risk_level_label(level_key: str | None) -> str:
    return RISK_LEVEL_LABELS.get(level_key, "未知")


def risk_tier_segments(score: float | None) -> Dict[str, float]:
    """Split a risk score into dedicated low/mid/high buckets for visualization."""
    normalized = clamp(float(score or 0.0))
    level = risk_level_from_score(normalized)
    segments = {"low": 0.0, "mid": 0.0, "high": 0.0}
    segments[level] = normalized
    return segments
