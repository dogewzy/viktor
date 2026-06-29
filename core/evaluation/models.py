"""Pydantic models for trace evaluation."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


TraceEvaluationStatus = Literal["queued", "running", "succeeded", "failed", "skipped"]
TraceEvalSampleType = Literal["single_turn", "multi_turn"]


class TraceEvalSamplePreview(BaseModel):
    sample_type: TraceEvalSampleType = "single_turn"
    user_input: str = ""
    retrieved_contexts: list[str] = Field(default_factory=list)
    response: str = ""
    context_count: int = 0
    source_event_seq: dict[str, Any] = Field(default_factory=dict)


class TraceEvalBuildResult(BaseModel):
    status: Literal["ready", "skipped"]
    trace_id: str
    project_id: str = ""
    sample_type: TraceEvalSampleType = "single_turn"
    sample_preview: TraceEvalSamplePreview | dict[str, Any] = Field(default_factory=dict)
    diagnostics: dict[str, Any] = Field(default_factory=dict)
    error: str = ""


class TraceEvaluationResult(BaseModel):
    evaluation_id: str = ""
    trace_id: str
    project_id: str = ""
    status: TraceEvaluationStatus
    sample_type: TraceEvalSampleType = "single_turn"
    metrics: list[str] = Field(default_factory=list)
    scores: dict[str, float | None] = Field(default_factory=dict)
    sample_preview: dict[str, Any] = Field(default_factory=dict)
    diagnostics: dict[str, Any] = Field(default_factory=dict)
    error: str = ""
