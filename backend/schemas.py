
from __future__ import annotations
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field

class Post(BaseModel):
    post_id: str
    published_at: str
    account_name: str
    content_text: str
    media: List[str] = Field(default_factory=list)
    reposts: int = 0
    comments: int = 0
    likes: int = 0

class LLMLabels(BaseModel):
    sentiment: float = 0.0   # -1~1
    region: Optional[str] = None
    topic_type: Optional[str] = None

class RiskDimensions(BaseModel):
    negativity: float = 0.0
    growth: float = 0.0
    sensitivity: float = 0.0
    crowd: float = 0.0

class EventRecord(BaseModel):
    event_id: str
    name: str
    first_seen_at: str
    last_seen_at: str
    last_content_update_date: Optional[str] = None
    hot_values: Dict[str, float] = Field(default_factory=dict)
    hour_list: Dict[str, int] = Field(default_factory=dict)
    summary_html: Optional[str] = None
    posts: List[Post] = Field(default_factory=list)

    llm: LLMLabels = Field(default_factory=LLMLabels)
    risk_dims: RiskDimensions = Field(default_factory=RiskDimensions)
    risk_score: float = 0.0
