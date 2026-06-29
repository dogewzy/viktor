"""Service layer for trace shadow evaluation."""

from __future__ import annotations

import hashlib
import random
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any

from loguru import logger
from sqlalchemy import or_

from core.database import SessionLocal
from core.evaluation.models import TraceEvaluationResult
from core.evaluation.ragas_adapter import evaluate_trace
from core.models import AgentTraceEventModel, TraceEvaluationModel
from settings import trace_evaluation_config


EVALUATOR_VERSION = "ragas-0.4.3-text-v1"
_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="trace-evaluation")


def schedule_trace_evaluation(trace_id: str) -> None:
    """Best-effort background evaluation for terminal trace events."""
    if not trace_id or not trace_evaluation_config.enabled:
        return
    sample_rate = trace_evaluation_config.auto_sample_rate
    if sample_rate <= 0 or random.random() > sample_rate:
        return
    try:
        queue_trace_evaluation(trace_id, force=False)
    except Exception as e:  # noqa: BLE001
        logger.warning("trace evaluation 调度失败: trace_id={}, error={}", trace_id, e)


def queue_trace_evaluation(
    trace_id: str,
    *,
    metrics: list[str] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Create or reuse a queued evaluation row and submit a background job."""
    normalized = _normalize_metrics(metrics)
    evaluation_id = _evaluation_id(trace_id, "single_turn", normalized)
    row = _ensure_evaluation_row(trace_id, evaluation_id, normalized, force=force)
    should_submit = row["status"] == "queued" and row.get("_submit", False)
    row.pop("_submit", None)
    if should_submit:
        _EXECUTOR.submit(_run_trace_evaluation_safe, evaluation_id, trace_id, normalized)
    return row


def run_trace_evaluation(
    trace_id: str,
    *,
    metrics: list[str] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Run one trace evaluation synchronously and persist the result."""
    normalized = _normalize_metrics(metrics)
    evaluation_id = _evaluation_id(trace_id, "single_turn", normalized)
    row = _ensure_evaluation_row(trace_id, evaluation_id, normalized, force=force)
    if not force and row["status"] in {"succeeded", "skipped", "running"}:
        row.pop("_submit", None)
        return row
    return _run_trace_evaluation(evaluation_id, trace_id, normalized)


def list_trace_evaluations(
    *,
    project_id: str | None = None,
    trace_id: str | None = None,
    status: str | None = None,
    q: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    limit = min(max(int(limit), 1), 1000)
    offset = max(int(offset), 0)
    db = SessionLocal()
    try:
        query = db.query(TraceEvaluationModel)
        if project_id:
            query = query.filter(TraceEvaluationModel.project_id == project_id)
        if trace_id:
            query = query.filter(TraceEvaluationModel.trace_id == trace_id)
        if status:
            query = query.filter(TraceEvaluationModel.status == status)
        if q:
            query = query.filter(or_(
                TraceEvaluationModel.evaluation_id.contains(q),
                TraceEvaluationModel.trace_id.contains(q),
                TraceEvaluationModel.error.contains(q),
            ))
        total = query.count()
        rows = (
            query.order_by(TraceEvaluationModel.created_at.desc(), TraceEvaluationModel.evaluation_id.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )
        return {"items": [_serialize(row) for row in rows], "total": total, "limit": limit, "offset": offset}
    finally:
        db.close()


def latest_successful_evaluation_summary(trace_id: str) -> dict[str, Any]:
    """Return the newest successful evaluation signal for trace learning."""
    if not trace_id:
        return {}
    db = SessionLocal()
    try:
        row = (
            db.query(TraceEvaluationModel)
            .filter(
                TraceEvaluationModel.trace_id == trace_id,
                TraceEvaluationModel.status == "succeeded",
            )
            .order_by(TraceEvaluationModel.updated_at.desc(), TraceEvaluationModel.created_at.desc())
            .first()
        )
        if row is None:
            return {}
        scores = row.scores or {}
        flags = [
            f"low_{metric}"
            for metric, score in scores.items()
            if isinstance(score, (int, float)) and score < trace_evaluation_config.low_score_threshold
        ]
        return {
            "evaluation_id": row.evaluation_id,
            "sample_type": row.sample_type,
            "metrics": row.metrics or [],
            "scores": scores,
            "flags": flags,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }
    except Exception as e:  # noqa: BLE001
        logger.debug("读取 trace evaluation summary 失败: trace_id={}, error={}", trace_id, e)
        return {}
    finally:
        db.close()


def _run_trace_evaluation_safe(evaluation_id: str, trace_id: str, metrics: list[str]) -> None:
    try:
        result = _run_trace_evaluation(evaluation_id, trace_id, metrics)
        logger.info(
            "trace evaluation 完成: trace_id={}, evaluation_id={}, status={}",
            trace_id,
            evaluation_id,
            result.get("status"),
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("trace evaluation 失败: trace_id={}, evaluation_id={}, error={}", trace_id, evaluation_id, e)


def _run_trace_evaluation(evaluation_id: str, trace_id: str, metrics: list[str]) -> dict[str, Any]:
    _mark_running(evaluation_id)
    try:
        result = evaluate_trace(trace_id, metrics)
    except Exception as e:  # noqa: BLE001
        logger.warning("trace evaluation evaluator 异常: trace_id={}, error={}", trace_id, e)
        result = TraceEvaluationResult(
            trace_id=trace_id,
            project_id=_trace_project_id(trace_id),
            status="failed",
            metrics=metrics,
            diagnostics={"where": "evaluate_trace"},
            error=f"{e.__class__.__name__}: {e}",
        )
    db = SessionLocal()
    try:
        row = db.get(TraceEvaluationModel, evaluation_id)
        if row is None:
            row = TraceEvaluationModel(
                evaluation_id=evaluation_id,
                trace_id=trace_id,
                project_id=result.project_id,
                metrics=metrics,
                sample_type=result.sample_type,
                evaluator_version=EVALUATOR_VERSION,
                created_at=datetime.now(),
            )
            db.add(row)
        row.project_id = result.project_id or row.project_id or _trace_project_id(trace_id)
        row.status = result.status
        row.sample_type = result.sample_type
        row.metrics = metrics
        row.scores = result.scores
        row.sample_preview = result.sample_preview
        row.diagnostics = result.diagnostics
        row.error = result.error[:4000]
        row.updated_at = datetime.now()
        db.commit()
        db.refresh(row)
        return _serialize(row)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _ensure_evaluation_row(
    trace_id: str,
    evaluation_id: str,
    metrics: list[str],
    *,
    force: bool,
) -> dict[str, Any]:
    db = SessionLocal()
    try:
        row = db.get(TraceEvaluationModel, evaluation_id)
        now = datetime.now()
        if row is None:
            row = TraceEvaluationModel(
                evaluation_id=evaluation_id,
                trace_id=trace_id,
                project_id=_trace_project_id(trace_id),
                status="queued",
                sample_type="single_turn",
                evaluator_version=EVALUATOR_VERSION,
                metrics=metrics,
                scores={},
                sample_preview={},
                diagnostics={},
                error="",
                created_at=now,
                updated_at=now,
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            serialized = _serialize(row)
            serialized["_submit"] = True
            return serialized
        if force and row.status in {"failed", "skipped"}:
            row.status = "queued"
            row.metrics = metrics
            row.scores = {}
            row.diagnostics = {}
            row.error = ""
            row.updated_at = now
            db.commit()
            db.refresh(row)
            serialized = _serialize(row)
            serialized["_submit"] = True
            return serialized
        serialized = _serialize(row)
        serialized["_submit"] = False
        return serialized
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _mark_running(evaluation_id: str) -> None:
    db = SessionLocal()
    try:
        row = db.get(TraceEvaluationModel, evaluation_id)
        if row is None:
            return
        row.status = "running"
        row.updated_at = datetime.now()
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _trace_project_id(trace_id: str) -> str:
    db = SessionLocal()
    try:
        row = (
            db.query(AgentTraceEventModel)
            .filter(AgentTraceEventModel.trace_id == trace_id)
            .order_by(AgentTraceEventModel.event_seq.asc(), AgentTraceEventModel.id.asc())
            .first()
        )
        return row.project_id if row and row.project_id else ""
    finally:
        db.close()


def _normalize_metrics(metrics: list[str] | None) -> list[str]:
    raw = metrics if metrics is not None else trace_evaluation_config.metrics
    normalized: list[str] = []
    for metric in raw or []:
        text = str(metric).strip().lower()
        if text and text not in normalized:
            normalized.append(text)
    return normalized or ["faithfulness"]


def _evaluation_id(trace_id: str, sample_type: str, metrics: list[str]) -> str:
    raw = "\n".join([trace_id, sample_type, ",".join(metrics), EVALUATOR_VERSION])
    return "te_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _serialize(row: TraceEvaluationModel) -> dict[str, Any]:
    return {
        "id": row.evaluation_id,
        "evaluation_id": row.evaluation_id,
        "trace_id": row.trace_id,
        "project_id": row.project_id,
        "status": row.status,
        "sample_type": row.sample_type,
        "evaluator_version": row.evaluator_version,
        "metrics": row.metrics or [],
        "scores": row.scores or {},
        "sample_preview": row.sample_preview or {},
        "diagnostics": row.diagnostics or {},
        "error": row.error,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
