"""Runtime glue for project-scoped intent resolution."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.audit.recorder import record_trace_event
from core.intent.models import IntentRoute
from core.intent.prompt import format_retrieval_context
from core.intent.resolver import resolve_project_intent
from settings import intent_config


@dataclass
class IntentContextResult:
    """Resolved intent route plus prompt-ready evidence."""

    route: IntentRoute | None = None
    retrieval_context: str = ""
    trace_meta: dict[str, Any] = field(default_factory=dict)


def prepare_intent_context(
    *,
    project_id: str,
    user_message: str,
    trace_id: str = "",
    session_id: str | None = None,
    topic_thread_id: str | None = None,
    recent_context: str = "",
    trace_meta: dict[str, Any] | None = None,
) -> IntentContextResult:
    """Resolve project intent, format topK evidence, and record trace events."""
    meta = dict(trace_meta or {})
    if not intent_config.enabled:
        return IntentContextResult(trace_meta=meta)

    route = resolve_project_intent(
        project_id,
        user_message,
        recent_context=recent_context,
        glossary_limit=intent_config.glossary_top_k,
        knowledge_limit=intent_config.knowledge_top_k,
    )
    retrieval_context = format_retrieval_context(route)

    record_trace_event(
        trace_id=trace_id,
        event_type="glossary_retrieval",
        project_id=project_id,
        session_id=session_id,
        topic_thread_id=topic_thread_id,
        payload={
            **meta,
            "query": user_message,
            "hits": [hit.model_dump() for hit in route.matched_glossaries],
            "missing_terms": route.missing_terms,
        },
    )
    record_trace_event(
        trace_id=trace_id,
        event_type="knowledge_retrieval",
        project_id=project_id,
        session_id=session_id,
        topic_thread_id=topic_thread_id,
        payload={
            **meta,
            "query": user_message,
            "hits": [hit.model_dump() for hit in route.matched_knowledge_notes],
        },
    )
    record_trace_event(
        trace_id=trace_id,
        event_type="intent_route",
        project_id=project_id,
        session_id=session_id,
        topic_thread_id=topic_thread_id,
        payload={**meta, **route.model_dump()},
    )
    return IntentContextResult(route=route, retrieval_context=retrieval_context, trace_meta=meta)
