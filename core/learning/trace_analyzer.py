"""Trace digest builder for automatic learning."""

from __future__ import annotations

from collections import Counter
from typing import Any

from core.database import SessionLocal
from core.models import AgentTraceEventModel
from core.learning.models import TraceDigest


_IMPORTANT_EVENT_TYPES = {
    "glossary_retrieval",
    "knowledge_retrieval",
    "intent_route",
    "tool_start",
    "tool_end",
    "final_answer",
    "error",
}


def build_trace_digest(trace_id: str) -> TraceDigest:
    """Load a full trace and compress it into learning-oriented signals."""
    db = SessionLocal()
    try:
        rows = (
            db.query(AgentTraceEventModel)
            .filter(AgentTraceEventModel.trace_id == trace_id)
            .order_by(AgentTraceEventModel.event_seq.asc(), AgentTraceEventModel.id.asc())
            .all()
        )
    finally:
        db.close()

    if not rows:
        return TraceDigest(trace_id=trace_id)

    project_id = next((row.project_id for row in rows if row.project_id), "")
    missing_terms: list[str] = []
    tool_counts: Counter[str] = Counter()
    failed_tools: list[str] = []
    sql_blocked_events: list[int] = []
    connector_failures: list[str] = []
    final_answer_excerpt = ""
    events: list[dict[str, Any]] = []
    evidence_summary: list[str] = []

    for row in rows:
        payload = row.payload or {}
        if row.event_type == "glossary_retrieval":
            for term in payload.get("missing_terms") or []:
                text = str(term).strip()
                if text and text not in missing_terms:
                    missing_terms.append(text)
        if row.event_type == "tool_start":
            tool = str(payload.get("tool") or "")
            if tool:
                tool_counts[tool] += 1
        if row.event_type == "tool_end":
            tool = str(payload.get("tool") or "")
            output = _text(payload.get("output"))
            error = _text(payload.get("error"))
            if payload.get("ok") is False and tool:
                failed_tools.append(tool)
            if _looks_like_sql_block(output, error):
                sql_blocked_events.append(int(row.event_seq or 0))
            connector = _connector_hint(payload)
            if connector and (payload.get("ok") is False or error):
                connector_failures.append(connector)
        if row.event_type in {"final_answer", "error"}:
            final_answer_excerpt = _text(payload.get("content") or payload.get("error"))[:2000]
        if row.event_type in _IMPORTANT_EVENT_TYPES:
            events.append(_event_digest(row.event_seq, row.event_type, payload))

    repeated_tools = [name for name, count in tool_counts.items() if count >= 2]
    for seq, event_type, summary in _summaries(events):
        evidence_summary.append(f"#{seq} {event_type}: {summary}")

    return TraceDigest(
        trace_id=trace_id,
        project_id=project_id,
        event_count=len(rows),
        terminal_event_type=rows[-1].event_type,
        missing_terms=missing_terms[:20],
        repeated_tools=repeated_tools[:12],
        failed_tools=_dedupe(failed_tools)[:12],
        sql_blocked_events=sql_blocked_events[:20],
        connector_failures=_dedupe(connector_failures)[:12],
        final_answer_excerpt=final_answer_excerpt,
        events=events[:120],
        evidence_summary=evidence_summary[:40],
    )


def _event_digest(seq: int, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    keep: dict[str, Any] = {}
    for key in (
        "query",
        "missing_terms",
        "intent_type",
        "tool_strategy",
        "risk_flags",
        "tool",
        "input",
        "ok",
        "error",
        "output",
        "content",
        "scope",
        "stage",
    ):
        if key in payload:
            keep[key] = payload[key]
    return {"event_seq": int(seq or 0), "event_type": event_type, "payload": _trim(keep)}


def _summaries(events: list[dict[str, Any]]) -> list[tuple[int, str, str]]:
    out: list[tuple[int, str, str]] = []
    for event in events:
        payload = event.get("payload") or {}
        seq = int(event.get("event_seq") or 0)
        event_type = str(event.get("event_type") or "")
        if event_type == "glossary_retrieval":
            out.append((seq, event_type, f"missing_terms={payload.get('missing_terms') or []}"))
        elif event_type == "tool_end":
            out.append((seq, event_type, f"{payload.get('tool')} ok={payload.get('ok')}"))
        elif event_type in {"final_answer", "error"}:
            out.append((seq, event_type, _text(payload.get("content") or payload.get("error"))[:180]))
    return out


def _looks_like_sql_block(output: str, error: str) -> bool:
    text = f"{output}\n{error}".lower()
    return any(key in text for key in ("sql", "readonly", "blocked", "拦截", "禁止", "只读", "全表扫描", "超时"))


def _connector_hint(payload: dict[str, Any]) -> str:
    raw = payload.get("input") if isinstance(payload.get("input"), dict) else {}
    for key in ("connector_id", "database_connector_id", "db", "database"):
        value = raw.get(key) if isinstance(raw, dict) else ""
        if value:
            return str(value)
    return ""


def _trim(value: Any, *, max_len: int = 1200) -> Any:
    if isinstance(value, str):
        return value[:max_len] + ("...[TRUNCATED]" if len(value) > max_len else "")
    if isinstance(value, dict):
        return {str(k): _trim(v, max_len=max_len) for k, v in list(value.items())[:24]}
    if isinstance(value, list):
        return [_trim(item, max_len=max_len) for item in value[:24]]
    return value


def _text(value: Any) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        key = item.strip()
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out
