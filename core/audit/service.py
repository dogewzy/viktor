"""Admin query service for Agent trace events."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func

from core.database import SessionLocal
from core.models import AgentTraceEventModel
from settings import agent_audit_config


def list_traces(
    *,
    project_id: str | None = None,
    session_id: str | None = None,
    topic_thread_id: str | None = None,
    event_type: str | None = None,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """Return paged trace summaries, optionally filtered by event fields."""
    limit = min(max(int(limit), 1), 1000)
    offset = max(int(offset), 0)
    db = SessionLocal()
    try:
        base = db.query(AgentTraceEventModel)
        base = _apply_filters(
            base,
            project_id=project_id,
            session_id=session_id,
            topic_thread_id=topic_thread_id,
            event_type=event_type,
            start_time=start_time,
            end_time=end_time,
        )
        grouped = (
            base.with_entities(
                AgentTraceEventModel.trace_id.label("trace_id"),
                func.min(AgentTraceEventModel.created_at).label("first_event_at"),
                func.max(AgentTraceEventModel.created_at).label("last_event_at"),
                func.count(AgentTraceEventModel.id).label("event_count"),
            )
            .group_by(AgentTraceEventModel.trace_id)
            .subquery()
        )
        total = db.query(func.count()).select_from(grouped).scalar() or 0
        rows = (
            db.query(
                grouped.c.trace_id,
                grouped.c.first_event_at,
                grouped.c.last_event_at,
                grouped.c.event_count,
            )
            .order_by(grouped.c.last_event_at.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )
        items = [_trace_summary(db, row.trace_id, row.first_event_at, row.last_event_at, row.event_count) for row in rows]
        return {"items": items, "total": total, "limit": limit, "offset": offset}
    finally:
        db.close()


def get_trace_events(
    trace_id: str,
    *,
    event_type: str | None = None,
    limit: int = 1000,
    offset: int = 0,
) -> dict[str, Any]:
    """Return paged events for one trace."""
    limit = min(max(int(limit), 1), 2000)
    offset = max(int(offset), 0)
    db = SessionLocal()
    try:
        query = db.query(AgentTraceEventModel).filter(AgentTraceEventModel.trace_id == trace_id)
        if event_type:
            query = query.filter(AgentTraceEventModel.event_type == event_type)
        total = query.count()
        rows = query.order_by(AgentTraceEventModel.event_seq.asc(), AgentTraceEventModel.id.asc()).offset(offset).limit(limit).all()
        return {
            "items": [_event_row(row) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
            "trace_id": trace_id,
        }
    finally:
        db.close()


def cleanup_expired_trace_events(*, retention_days: int | None = None) -> int:
    """Delete trace events older than the configured retention window."""
    days = retention_days if retention_days is not None else agent_audit_config.retention_days
    if days <= 0:
        return 0
    cutoff = datetime.now() - timedelta(days=days)
    db = SessionLocal()
    try:
        count = db.query(AgentTraceEventModel).filter(AgentTraceEventModel.created_at < cutoff).delete()
        db.commit()
        return int(count or 0)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _apply_filters(query: Any, **filters: Any) -> Any:
    if filters.get("project_id"):
        query = query.filter(AgentTraceEventModel.project_id == filters["project_id"])
    if filters.get("session_id"):
        query = query.filter(AgentTraceEventModel.session_id == filters["session_id"])
    if filters.get("topic_thread_id"):
        query = query.filter(AgentTraceEventModel.topic_thread_id == filters["topic_thread_id"])
    if filters.get("event_type"):
        query = query.filter(AgentTraceEventModel.event_type == filters["event_type"])
    if filters.get("start_time"):
        query = query.filter(AgentTraceEventModel.created_at >= filters["start_time"])
    if filters.get("end_time"):
        query = query.filter(AgentTraceEventModel.created_at <= filters["end_time"])
    return query


def _trace_summary(db: Any, trace_id: str, first_event_at: Any, last_event_at: Any, event_count: int) -> dict[str, Any]:
    rows = (
        db.query(AgentTraceEventModel)
        .filter(AgentTraceEventModel.trace_id == trace_id)
        .order_by(AgentTraceEventModel.event_seq.asc(), AgentTraceEventModel.id.asc())
        .all()
    )
    first = rows[0] if rows else None
    latest = rows[-1] if rows else None
    return {
        "trace_id": trace_id,
        "project_id": getattr(first, "project_id", "") if first else "",
        "session_id": getattr(first, "session_id", "") if first else "",
        "topic_thread_id": getattr(first, "topic_thread_id", "") if first else "",
        "first_event_at": _dt(first_event_at),
        "last_event_at": _dt(last_event_at),
        "event_count": int(event_count or 0),
        "event_types": sorted({row.event_type for row in rows}),
        "latest_event_type": getattr(latest, "event_type", "") if latest else "",
    }


def _event_row(row: AgentTraceEventModel) -> dict[str, Any]:
    return {
        "id": row.id,
        "trace_id": row.trace_id,
        "event_seq": row.event_seq,
        "event_type": row.event_type,
        "project_id": row.project_id,
        "session_id": row.session_id,
        "topic_thread_id": row.topic_thread_id,
        "payload": row.payload or {},
        "created_at": _dt(row.created_at),
    }


def _dt(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else None
