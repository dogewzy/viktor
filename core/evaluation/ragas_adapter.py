"""Ragas adapter for Agent trace shadow evaluation."""

from __future__ import annotations

import asyncio
import math
from typing import Any

from loguru import logger

from core.audit.redaction import redact_payload
from core.database import SessionLocal
from core.evaluation.models import (
    TraceEvalBuildResult,
    TraceEvalSamplePreview,
    TraceEvaluationResult,
)
from core.models import AgentTraceEventModel
from settings import trace_evaluation_config


SUPPORTED_TEXT_METRICS = {"faithfulness"}


def build_single_turn_sample(trace_id: str) -> TraceEvalBuildResult:
    """Build a Ragas-compatible single-turn sample preview from a stored trace."""
    rows = _load_trace_rows(trace_id)
    if not rows:
        return TraceEvalBuildResult(
            status="skipped",
            trace_id=trace_id,
            diagnostics={"reason": "trace_not_found"},
        )

    project_id = next((row.project_id for row in rows if row.project_id), "")
    user_input = ""
    response = ""
    source_event_seq: dict[str, Any] = {}
    contexts: list[str] = []
    max_contexts = trace_evaluation_config.max_contexts
    max_chars = trace_evaluation_config.max_context_chars

    for row in rows:
        payload = row.payload or {}
        if not user_input and row.event_type in {"glossary_retrieval", "knowledge_retrieval"}:
            user_input = _clean_text(payload.get("query"))
            if user_input:
                source_event_seq["user_input"] = int(row.event_seq or 0)
        if not user_input and row.event_type == "intent_route":
            user_input = _clean_text(payload.get("user_message"))
            if user_input:
                source_event_seq["user_input"] = int(row.event_seq or 0)
        if row.event_type in {"glossary_retrieval", "knowledge_retrieval"}:
            for context in _contexts_from_hits(row.event_type, payload):
                _append_context(contexts, context, max_contexts=max_contexts, max_chars=max_chars)
                if len(contexts) <= max_contexts:
                    source_event_seq.setdefault("retrieved_contexts", []).append(int(row.event_seq or 0))
        if row.event_type == "tool_end":
            context = _context_from_tool(row.event_seq, payload)
            if context:
                _append_context(contexts, context, max_contexts=max_contexts, max_chars=max_chars)
                if len(contexts) <= max_contexts:
                    source_event_seq.setdefault("retrieved_contexts", []).append(int(row.event_seq or 0))
        if row.event_type == "final_answer":
            response = _clean_text(payload.get("content"))
            if response:
                source_event_seq["response"] = int(row.event_seq or 0)

    if not user_input:
        return _skipped(trace_id, project_id, "missing_user_input")
    if not response:
        return _skipped(trace_id, project_id, "missing_final_answer")
    if not contexts:
        return _skipped(trace_id, project_id, "missing_retrieved_contexts")

    preview = TraceEvalSamplePreview(
        user_input=_trim(user_input, 1200),
        retrieved_contexts=contexts,
        response=_trim(response, 2000),
        context_count=len(contexts),
        source_event_seq=source_event_seq,
    )
    return TraceEvalBuildResult(
        status="ready",
        trace_id=trace_id,
        project_id=project_id,
        sample_preview=preview,
        diagnostics={"context_count": len(contexts)},
    )


def build_multi_turn_sample(
    trace_id: str,
    reference_tool_calls: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return a lightweight multi-turn sample preview for future agent metrics."""
    rows = _load_trace_rows(trace_id)
    messages: list[dict[str, Any]] = []
    for row in rows:
        payload = row.payload or {}
        if row.event_type == "glossary_retrieval" and payload.get("query"):
            messages.append({"role": "human", "content": _clean_text(payload.get("query"))})
        elif row.event_type == "tool_start":
            messages.append({
                "role": "ai",
                "tool_calls": [{"name": payload.get("tool"), "args": payload.get("input") or {}}],
            })
        elif row.event_type == "tool_end":
            messages.append({
                "role": "tool",
                "name": payload.get("tool") or "",
                "content": _trim(_clean_text(payload.get("output") or payload.get("error")), 1200),
            })
        elif row.event_type == "final_answer":
            messages.append({"role": "ai", "content": _clean_text(payload.get("content"))})
    return {
        "trace_id": trace_id,
        "sample_type": "multi_turn",
        "messages": messages,
        "reference_tool_calls": reference_tool_calls or [],
    }


def evaluate_trace(trace_id: str, metrics: list[str]) -> TraceEvaluationResult:
    """Build and evaluate one trace with the supported Ragas text metrics."""
    build = build_single_turn_sample(trace_id)
    preview = _preview_dict(build.sample_preview)
    if build.status == "skipped":
        return TraceEvaluationResult(
            trace_id=trace_id,
            project_id=build.project_id,
            status="skipped",
            metrics=metrics,
            sample_preview=preview,
            diagnostics=build.diagnostics,
            error=build.error,
        )

    supported = [metric for metric in metrics if metric in SUPPORTED_TEXT_METRICS]
    unsupported = [metric for metric in metrics if metric not in SUPPORTED_TEXT_METRICS]
    if not supported:
        return TraceEvaluationResult(
            trace_id=trace_id,
            project_id=build.project_id,
            status="skipped",
            metrics=metrics,
            sample_preview=preview,
            diagnostics={**build.diagnostics, "unsupported_metrics": unsupported},
            error="no_supported_metrics",
        )

    try:
        scores = _run_supported_metrics(preview, supported)
    except Exception as e:  # noqa: BLE001
        logger.warning("trace evaluation failed: trace_id={}, error={}", trace_id, e)
        return TraceEvaluationResult(
            trace_id=trace_id,
            project_id=build.project_id,
            status="failed",
            metrics=metrics,
            sample_preview=preview,
            diagnostics={**build.diagnostics, "unsupported_metrics": unsupported},
            error=f"{e.__class__.__name__}: {e}",
        )

    return TraceEvaluationResult(
        trace_id=trace_id,
        project_id=build.project_id,
        status="succeeded",
        metrics=metrics,
        scores=scores,
        sample_preview=preview,
        diagnostics={**build.diagnostics, "unsupported_metrics": unsupported},
    )


def _run_supported_metrics(sample: dict[str, Any], metrics: list[str]) -> dict[str, float | None]:
    return asyncio.run(_run_supported_metrics_async(sample, metrics))


async def _run_supported_metrics_async(sample: dict[str, Any], metrics: list[str]) -> dict[str, float | None]:
    try:
        from ragas.dataset_schema import SingleTurnSample
        from ragas.llms import LangchainLLMWrapper
        from ragas.metrics import Faithfulness
        from ragas.run_config import RunConfig
    except Exception as e:  # noqa: BLE001
        raise RuntimeError("ragas==0.4.3 is not installed or cannot be imported") from e

    from core.llm_client import create_llm

    llm = LangchainLLMWrapper(create_llm(thinking=False, feature="trace_evaluation"))
    ragas_sample = SingleTurnSample(
        user_input=str(sample.get("user_input") or ""),
        retrieved_contexts=list(sample.get("retrieved_contexts") or []),
        response=str(sample.get("response") or ""),
    )
    scores: dict[str, float | None] = {}
    for metric in metrics:
        if metric != "faithfulness":
            continue
        scorer = Faithfulness(llm=llm)
        try:
            scorer.init(RunConfig(timeout=trace_evaluation_config.timeout_sec))
        except Exception:
            pass
        raw = await scorer.single_turn_ascore(ragas_sample, timeout=trace_evaluation_config.timeout_sec)
        scores[metric] = _score(raw)
    return scores


def _load_trace_rows(trace_id: str) -> list[AgentTraceEventModel]:
    db = SessionLocal()
    try:
        return (
            db.query(AgentTraceEventModel)
            .filter(AgentTraceEventModel.trace_id == trace_id)
            .order_by(AgentTraceEventModel.event_seq.asc(), AgentTraceEventModel.id.asc())
            .all()
        )
    finally:
        db.close()


def _skipped(trace_id: str, project_id: str, reason: str) -> TraceEvalBuildResult:
    return TraceEvalBuildResult(
        status="skipped",
        trace_id=trace_id,
        project_id=project_id,
        diagnostics={"reason": reason},
        error=reason,
    )


def _contexts_from_hits(event_type: str, payload: dict[str, Any]) -> list[str]:
    contexts: list[str] = []
    kind = "glossary" if event_type == "glossary_retrieval" else "knowledge"
    for hit in payload.get("hits") or []:
        if not isinstance(hit, dict):
            continue
        parts = [f"[{kind}] {_clean_text(hit.get('title') or hit.get('id'))}"]
        for key in ("description", "content", "match_reason"):
            value = _clean_text(hit.get(key))
            if value:
                parts.append(value)
        aliases = hit.get("aliases") or []
        code_keywords = hit.get("code_keywords") or []
        if aliases:
            parts.append("aliases: " + ", ".join(str(item) for item in aliases[:12]))
        if code_keywords:
            parts.append("code_keywords: " + ", ".join(str(item) for item in code_keywords[:20]))
        contexts.append(_redacted_text("\n".join(part for part in parts if part.strip())))
    return contexts


def _context_from_tool(seq: int, payload: dict[str, Any]) -> str:
    output = _clean_text(payload.get("output"))
    if not output:
        return ""
    tool = _clean_text(payload.get("tool")) or "tool"
    ok = payload.get("ok")
    return _redacted_text(f"[tool:{tool} seq={int(seq or 0)} ok={ok}]\n{output}")


def _append_context(contexts: list[str], context: str, *, max_contexts: int, max_chars: int) -> None:
    context = _trim(context, min(3000, max_chars))
    if not context or context in contexts or len(contexts) >= max_contexts:
        return
    current_chars = sum(len(item) for item in contexts)
    remaining = max_chars - current_chars
    if remaining <= 0:
        return
    contexts.append(_trim(context, remaining))


def _preview_dict(value: TraceEvalSamplePreview | dict[str, Any]) -> dict[str, Any]:
    if isinstance(value, TraceEvalSamplePreview):
        return value.model_dump()
    return dict(value or {})


def _redacted_text(text: str) -> str:
    redacted = redact_payload(
        {"value": text},
        max_string_length=min(3000, trace_evaluation_config.max_context_chars),
    )
    value = redacted.get("value") if isinstance(redacted, dict) else text
    return _clean_text(value)


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _trim(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 14)] + "...[TRUNCATED]"


def _score(value: Any) -> float | None:
    try:
        score = float(value)
    except Exception:
        return None
    if math.isnan(score) or math.isinf(score):
        return None
    return max(0.0, min(1.0, score))
