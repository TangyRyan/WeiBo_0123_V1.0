from __future__ import annotations

import itertools
import re
from collections import Counter, defaultdict
from typing import Any, Dict, List

from backend.health.constants import EMOTION_DIMENSIONS, STOPWORDS
from backend.health.models import EventDetail, EventFeatures, FeatureEdge, FeatureNode, HealthEvent, TimelinePoint, WordCloudItem
from backend.health.timeline import normalize_points

try:  # Optional dependencies declared in requirements
    import jieba  # type: ignore
except Exception:  # pragma: no cover
    jieba = None

try:
    import networkx as nx  # type: ignore
except Exception:  # pragma: no cover
    nx = None

try:
    from community import community_louvain  # type: ignore
except Exception:  # pragma: no cover
    community_louvain = None

HASHTAG_PATTERN = re.compile(r"#([^#]+)#")


def build_event_detail(event: HealthEvent) -> EventDetail:
    normalized_points = normalize_points(event.raw_points)
    wordcloud = _build_wordcloud(event.posts)
    tag_graph = _build_tag_graph(event)
    emotions = _build_emotions(event.sentiment_vector)
    posts = _summarize_posts(event.posts)
    return EventDetail(
        event_id=event.event_id,
        date=event.date,
        title=event.title,
        category=event.category,
        health_minor=event.health_minor,
        sentiment=event.sentiment,
        sentiment_vector=emotions,
        region=event.region,
        start_ts=event.start_ts,
        end_ts=event.end_ts,
        heat_peak=event.heat_peak,
        point_count=len(normalized_points),
        points=normalized_points,
        summary=event.summary or event.title,
        tags=event.tags,
        tag_graph=tag_graph,
        wordcloud=wordcloud,
        emotions=emotions,
        sample_posts=posts,
    )


def _build_tag_graph(event: HealthEvent) -> EventFeatures:
    tags = list(event.tags)
    tag_counts = Counter(tags)
    co_counts: Dict[tuple[str, str], int] = defaultdict(int)

    for post in event.posts:
        local_tags = set(_extract_hashtags(post.get("content_text") or ""))
        if not local_tags:
            continue
        for a, b in itertools.combinations(sorted(local_tags), 2):
            co_counts[(a, b)] += 1
        for tag in local_tags:
            tag_counts[tag] += 1

    if not tag_counts:
        return EventFeatures()

    nodes = [FeatureNode(id=tag, label=tag, weight=float(count)) for tag, count in tag_counts.items()]
    edges = [
        FeatureEdge(source=a, target=b, weight=float(weight))
        for (a, b), weight in co_counts.items()
        if weight > 0
    ]

    if nx and community_louvain and len(nodes) >= 3:
        graph = nx.Graph()
        for node in nodes:
            graph.add_node(node.id, weight=node.weight)
        for edge in edges:
            graph.add_edge(edge.source, edge.target, weight=edge.weight)
        if graph.number_of_edges() > 0:
            partition = community_louvain.best_partition(graph, weight="weight")
            for node in nodes:
                node.community = partition.get(node.id)

    return EventFeatures(nodes=nodes, edges=edges)


def _build_wordcloud(posts: List[Dict[str, Any]]) -> List[WordCloudItem]:
    texts = " ".join((post.get("content_text") or "") for post in posts)
    if not texts.strip():
        return []

    tokens: List[str]
    if jieba:
        tokens = [token.strip() for token in jieba.cut(texts) if token and len(token.strip()) > 1]
    else:
        tokens = [token for token in re.split(r"[^\w]+", texts) if len(token) > 1]

    filtered = [
        token for token in tokens if token not in STOPWORDS and not token.isdigit()
    ]
    counts = Counter(filtered)
    top = counts.most_common(80)
    return [WordCloudItem(text=text, weight=float(weight)) for text, weight in top]


def _build_emotions(vector: Dict[str, float]) -> List[Dict[str, float]]:
    return [{"name": name, "value": float(vector.get(name, 0.0))} for name in EMOTION_DIMENSIONS]


def _summarize_posts(posts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    summaries: List[Dict[str, any]] = []
    for post in posts[:8]:
        summaries.append({
            "post_id": post.get("post_id"),
            "published_at": post.get("published_at"),
            "account_name": post.get("account_name"),
            "content_text": post.get("content_text"),
            "reposts": post.get("reposts"),
            "comments": post.get("comments"),
            "likes": post.get("likes"),
        })
    return summaries


def _extract_hashtags(text: str) -> List[str]:
    return [match.strip() for match in HASHTAG_PATTERN.findall(text) if match.strip()]
