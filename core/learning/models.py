"""Pydantic models used by trace learning."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


CandidateTargetType = Literal["glossary", "knowledge_note", "skill", "agent_rule"]
CandidateRiskLevel = Literal["low", "medium", "high"]
CandidateStatus = Literal["pending", "applied", "rejected"]


class TraceDigest(BaseModel):
    trace_id: str
    project_id: str = ""
    event_count: int = 0
    terminal_event_type: str = ""
    missing_terms: list[str] = Field(default_factory=list)
    repeated_tools: list[str] = Field(default_factory=list)
    failed_tools: list[str] = Field(default_factory=list)
    sql_blocked_events: list[int] = Field(default_factory=list)
    connector_failures: list[str] = Field(default_factory=list)
    final_answer_excerpt: str = ""
    events: list[dict[str, Any]] = Field(default_factory=list)
    evidence_summary: list[str] = Field(default_factory=list)
    evaluation_summary: dict[str, Any] = Field(default_factory=dict)


class LearningCandidateDraft(BaseModel):
    target_type: CandidateTargetType
    target_id: str = ""
    title: str
    content: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    confidence: float = 0.0
    risk_level: CandidateRiskLevel = "medium"
    evidence_event_seq: list[int] = Field(default_factory=list)
