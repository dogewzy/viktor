"""Best-effort Agent trace recorder."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from loguru import logger
from sqlalchemy import func

from core.audit.redaction import redact_payload
from core.database import SessionLocal
from core.models import AgentTraceEventModel
from settings import agent_audit_config


def record_trace_event(
    *,
    trace_id: str,
    event_type: str,
    project_id: str = "",
    session_id: str | None = None,
    topic_thread_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    """Persist one trace event without ever breaking the Agent turn."""
    if not agent_audit_config.enabled or not trace_id:
        return
    redacted = redact_payload(payload or {}, max_string_length=agent_audit_config.max_payload_string_length)
    db = SessionLocal()
    try:
        current_seq = (
            db.query(func.max(AgentTraceEventModel.event_seq))
            .filter(AgentTraceEventModel.trace_id == trace_id)
            .scalar()
        )
        db.add(
            AgentTraceEventModel(
                trace_id=trace_id,
                event_seq=int(current_seq or 0) + 1,
                event_type=event_type,
                project_id=project_id or "",
                session_id=session_id or "",
                topic_thread_id=topic_thread_id or "",
                payload=redacted,
                created_at=datetime.now(),
            )
        )
        db.commit()
        terminal_error = event_type == "error" and redacted.get("where") != "llm_client"
        if event_type == "final_answer" or terminal_error:
            try:
                from core.learning.service import schedule_trace_learning

                schedule_trace_learning(trace_id)
            except Exception as learning_error:  # noqa: BLE001
                logger.warning("trace learning 调度失败: trace_id={}, error={}", trace_id, learning_error)
            try:
                from core.evaluation.service import schedule_trace_evaluation

                schedule_trace_evaluation(trace_id)
            except Exception as evaluation_error:  # noqa: BLE001
                logger.warning("trace evaluation 调度失败: trace_id={}, error={}", trace_id, evaluation_error)
    except Exception as e:  # noqa: BLE001
        db.rollback()
        logger.warning("Agent trace 写入失败: trace_id={}, event_type={}, error={}", trace_id, event_type, e)
    finally:
        db.close()
