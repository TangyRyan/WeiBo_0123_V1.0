from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


@dataclass
class TimelinePoint:
    """Single point on the timeline (10 min granularity)."""

    ts: int  # epoch seconds
    heat: float
    rank: Optional[int] = None


@dataclass
class HealthEvent:
    """In-memory representation of a health-topic event."""

    event_id: str
    date: str
    name: str
    title: str
    category: str
    health_minor: str
    sentiment: float
    sentiment_vector: Dict[str, float]
    region: str
    start_ts: int
    end_ts: int
    heat_peak: float
    raw_points: List[TimelinePoint] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    posts: List[Dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TimelineEvent:
    event_id: str
    title: str
    category: str
    start_ts: int
    end_ts: int
    heat_peak: float
    sentiment: float
    region: str
    point_count: int
    points: List[TimelinePoint] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["points"] = [asdict(point) for point in self.points]
        return payload


@dataclass
class TimelineSummary:
    total_events: int
    by_major: Dict[str, int]

    def to_dict(self) -> Dict[str, Any]:
        return {"total_events": self.total_events, "by_major": dict(self.by_major)}


@dataclass
class TimelinePayload:
    updated_at: str
    summary: TimelineSummary
    events: List[TimelineEvent]
    date: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        payload = {
            "updated_at": self.updated_at,
            "summary": self.summary.to_dict(),
            "events": [event.to_dict() for event in self.events],
        }
        if self.date:
            payload["date"] = self.date
        return payload


@dataclass
class FeatureNode:
    id: str
    label: str
    weight: float
    community: Optional[int] = None


@dataclass
class FeatureEdge:
    source: str
    target: str
    weight: float


@dataclass
class EventFeatures:
    nodes: List[FeatureNode] = field(default_factory=list)
    edges: List[FeatureEdge] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "nodes": [asdict(node) for node in self.nodes],
            "edges": [asdict(edge) for edge in self.edges],
        }


@dataclass
class WordCloudItem:
    text: str
    weight: float


@dataclass
class EventDetail:
    event_id: str
    date: str
    title: str
    category: str
    health_minor: str
    sentiment: float
    sentiment_vector: List[Dict[str, float]]
    region: str
    start_ts: int
    end_ts: int
    heat_peak: float
    point_count: int
    points: List[TimelinePoint]
    summary: str
    tags: List[str]
    tag_graph: EventFeatures
    wordcloud: List[WordCloudItem]
    emotions: List[Dict[str, Any]]
    sample_posts: List[Dict[str, Any]]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "date": self.date,
            "title": self.title,
            "category": self.category,
            "health_minor": self.health_minor,
            "sentiment": self.sentiment,
            "sentiment_vector": self.sentiment_vector,
            "region": self.region,
            "start_ts": self.start_ts,
            "end_ts": self.end_ts,
            "heat_peak": self.heat_peak,
            "point_count": self.point_count,
            "points": [asdict(point) for point in self.points],
            "summary": self.summary,
            "tags": list(self.tags),
            "tag_graph": self.tag_graph.to_dict(),
            "wordcloud": [asdict(item) for item in self.wordcloud],
            "emotions": list(self.emotions),
            "sample_posts": list(self.sample_posts),
        }
