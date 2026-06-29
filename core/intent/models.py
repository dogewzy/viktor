"""Structured models for glossary-first intent routing."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


IntentType = Literal[
    "term_mapping",
    "data_query",
    "metric_query",
    "incident_diagnosis",
    "code_debug",
    "log_query",
    "mixed",
    "direct_answer",
]

ToolStrategy = Literal[
    "glossary_only",
    "glossary_then_db",
    "glossary_then_code",
    "clarify_first",
    "log_first",
    "direct_answer",
]


class RetrievalHit(BaseModel):
    """A matched project-scoped glossary or knowledge-note item."""

    id: str
    title: str
    kind: str = ""
    score: float = 0.0
    match_reason: str = ""
    matched_fields: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    code_keywords: list[str] = Field(default_factory=list)
    description: str = ""
    content: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class IntentRoute(BaseModel):
    """Route chosen before the main Agent starts calling tools."""

    project_id: str
    user_message: str
    intent_type: IntentType = "mixed"
    tool_strategy: ToolStrategy = "glossary_then_db"
    matched_glossaries: list[RetrievalHit] = Field(default_factory=list)
    matched_knowledge_notes: list[RetrievalHit] = Field(default_factory=list)
    missing_terms: list[str] = Field(default_factory=list)
    risk_flags: list[str] = Field(default_factory=list)
    blocking_questions: list[dict[str, Any]] = Field(default_factory=list)
    confidence: float = 0.0
    evidence: list[str] = Field(default_factory=list)

    @property
    def needs_clarification(self) -> bool:
        return self.tool_strategy == "clarify_first" and bool(self.blocking_questions)

    @property
    def needs_code_semantics(self) -> bool:
        return self.tool_strategy == "glossary_then_code"
