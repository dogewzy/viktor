"""Pydantic models for Agent trace events."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


TraceEventType = Literal[
    "glossary_retrieval",
    "knowledge_retrieval",
    "intent_route",
    "clarification_decision",
    "llm_request",
    "llm_response",
    "tool_start",
    "tool_end",
    "final_answer",
    "error",
]


class TraceEvent(BaseModel):
    """Serialized Agent trace event exposed through admin APIs."""

    id: int | None = None
    trace_id: str
    event_seq: int = 0
    event_type: TraceEventType | str
    project_id: str = ""
    session_id: str = ""
    topic_thread_id: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None


class TraceSummary(BaseModel):
    """One trace row in the admin trace list."""

    trace_id: str
    project_id: str = ""
    session_id: str = ""
    topic_thread_id: str = ""
    first_event_at: datetime | None = None
    last_event_at: datetime | None = None
    event_count: int = 0
    event_types: list[str] = Field(default_factory=list)
    latest_event_type: str = ""
