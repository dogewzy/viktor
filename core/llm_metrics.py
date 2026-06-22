"""LLM 调用观测与供应商健康状态。"""
from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from contextvars import ContextVar
from typing import Any

from loguru import logger
from sqlalchemy.exc import SQLAlchemyError

from core.database import SessionLocal
from core.models import ChatMessageModel, CodingTaskModel, LLMCallModel
from settings import llm_config


_cooldowns: dict[str, float] = {}
_llm_context: ContextVar[dict[str, Any]] = ContextVar("llm_observation_context", default={})


@dataclass
class LLMCallRecord:
    request_id: str
    feature: str
    provider: str
    model: str
    attempt_index: int
    fallback_from: str | None = None
    status: str = "success"
    streaming: bool = False
    started_at: datetime = field(default_factory=datetime.now)
    first_token_ms: float | None = None
    duration_ms: float | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    output_chars: int = 0
    tokens_per_second: float | None = None
    error_type: str = ""
    error_message: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


@contextmanager
def llm_observation_context(**meta: Any) -> Iterator[None]:
    """Attach business identifiers to all LLM calls made inside this context."""
    current = dict(_llm_context.get() or {})
    clean_meta = {key: value for key, value in meta.items() if value not in (None, "")}
    token = _llm_context.set({**current, **clean_meta})
    try:
        yield
    finally:
        _llm_context.reset(token)


def current_llm_context() -> dict[str, Any]:
    return dict(_llm_context.get() or {})


def mark_provider_cooldown(provider_id: str, seconds: int | None = None) -> None:
    ttl = seconds if seconds is not None else llm_config.cooldown_sec
    _cooldowns[provider_id] = time.monotonic() + max(1, ttl)


def provider_cooldown_remaining(provider_id: str) -> float:
    until = _cooldowns.get(provider_id, 0)
    remaining = until - time.monotonic()
    if remaining <= 0:
        _cooldowns.pop(provider_id, None)
        return 0.0
    return remaining


def record_llm_call(record: LLMCallRecord) -> None:
    db = SessionLocal()
    try:
        db.add(
            LLMCallModel(
                request_id=record.request_id,
                feature=record.feature,
                provider=record.provider,
                model=record.model,
                attempt_index=record.attempt_index,
                fallback_from=record.fallback_from,
                status=record.status,
                streaming=1 if record.streaming else 0,
                started_at=record.started_at,
                first_token_ms=record.first_token_ms,
                duration_ms=record.duration_ms,
                prompt_tokens=record.prompt_tokens,
                completion_tokens=record.completion_tokens,
                total_tokens=record.total_tokens,
                output_chars=record.output_chars,
                tokens_per_second=record.tokens_per_second,
                error_type=record.error_type,
                error_message=record.error_message[:2000],
                meta=record.meta,
            )
        )
        db.commit()
    except Exception as e:  # noqa: BLE001
        db.rollback()
        logger.warning("LLM 调用观测写入失败: {}", e)
    finally:
        db.close()


def list_coding_task_token_usage(
    *,
    project_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
    days: int | None = None,
) -> dict[str, Any]:
    """按 Coding Task 聚合 LLM token 用量。"""
    since = _since_days(days)
    db = SessionLocal()
    try:
        task_query = db.query(CodingTaskModel)
        if project_id:
            task_query = task_query.filter(CodingTaskModel.project_id == project_id)
        if since:
            task_query = task_query.filter(CodingTaskModel.created_at >= since)
        total = task_query.count()
        tasks = (
            task_query.order_by(CodingTaskModel.created_at.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )
        task_ids = [row.task_id for row in tasks]
        usage = _usage_by_meta_key(db, scope="codetask", key="task_id", allowed_ids=set(task_ids), since=since)
        return {
            "items": [_coding_task_usage_row(row, usage.get(row.task_id)) for row in tasks],
            "total": total,
            "limit": limit,
            "offset": offset,
            "days": days,
            "unattributed_note": "仅统计已写入 llm_calls.meta.task_id 的调用；历史无上下文记录不会被归入具体任务。",
        }
    except SQLAlchemyError as e:
        logger.warning("读取 Coding Task token 用量失败，返回空列表: {}", e.__class__.__name__)
        return {"items": [], "total": 0, "limit": limit, "offset": offset, "days": days}
    finally:
        db.close()


def list_chat_token_usage(
    *,
    project_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
    days: int | None = None,
) -> dict[str, Any]:
    """按 Chat topic_thread_id 聚合 LLM token 用量。"""
    since = _since_days(days)
    db = SessionLocal()
    try:
        thread_rows = _chat_thread_rows(db, project_id=project_id, since=since)
        total = len(thread_rows)
        page = thread_rows[offset: offset + limit]
        topic_ids = {str(row["topic_thread_id"]) for row in page}
        usage = _usage_by_meta_key(db, scope="chat", key="topic_thread_id", allowed_ids=topic_ids, since=since)
        return {
            "items": [_chat_usage_row(row, usage.get(str(row["topic_thread_id"]))) for row in page],
            "total": total,
            "limit": limit,
            "offset": offset,
            "days": days,
            "unattributed_note": "仅统计已写入 llm_calls.meta.topic_thread_id 的调用；历史无上下文记录不会被归入具体会话。",
        }
    except SQLAlchemyError as e:
        logger.warning("读取 Chat token 用量失败，返回空列表: {}", e.__class__.__name__)
        return {"items": [], "total": 0, "limit": limit, "offset": offset, "days": days}
    finally:
        db.close()


def provider_health() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for provider_id, cfg in llm_config.providers.items():
        cooldown_remaining = provider_cooldown_remaining(provider_id)
        rows.append(
            {
                "id": provider_id,
                "provider": cfg.provider,
                "model": cfg.model,
                "base_url": cfg.base_url,
                "default": provider_id == llm_config.default,
                "fallback_order": llm_config.fallback_order.index(provider_id)
                if provider_id in llm_config.fallback_order
                else None,
                "supports_tools": cfg.supports_tools,
                "supports_stream": cfg.supports_stream,
                "supports_thinking": cfg.supports_thinking,
                "configured": bool((cfg.api_key or "").strip()),
                "cooldown_remaining_sec": round(cooldown_remaining, 1),
                "status": "cooldown" if cooldown_remaining > 0 else "ready",
            }
        )
    return rows


def list_llm_calls(*, limit: int = 100, offset: int = 0, provider: str | None = None, feature: str | None = None) -> dict:
    db = SessionLocal()
    try:
        query = db.query(LLMCallModel)
        if provider:
            query = query.filter(LLMCallModel.provider == provider)
        if feature:
            query = query.filter(LLMCallModel.feature == feature)
        total = query.count()
        rows = query.order_by(LLMCallModel.started_at.desc(), LLMCallModel.id.desc()).offset(offset).limit(limit).all()
        return {
            "items": [_serialize_call(row) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    except SQLAlchemyError as e:
        logger.warning("读取 LLM 调用记录失败，返回空列表: {}", e.__class__.__name__)
        return {"items": [], "total": 0, "limit": limit, "offset": offset}
    finally:
        db.close()


def llm_summary(*, window_minutes: int = 60) -> dict[str, Any]:
    since = datetime.now() - timedelta(minutes=max(1, window_minutes))
    db = SessionLocal()
    try:
        rows = (
            db.query(LLMCallModel)
            .filter(LLMCallModel.started_at >= since)
            .order_by(LLMCallModel.started_at.desc())
            .limit(5000)
            .all()
        )
    except SQLAlchemyError as e:
        logger.warning("读取 LLM 调用汇总失败，返回空汇总: {}", e.__class__.__name__)
        rows = []
    finally:
        db.close()

    by_provider: dict[str, dict[str, Any]] = {}
    totals = {
        "calls": len(rows),
        "success": 0,
        "rate_limited": 0,
        "errors": 0,
        "fallbacks": 0,
        "avg_first_token_ms": None,
        "avg_duration_ms": None,
        "avg_tokens_per_second": None,
        "total_tokens": 0,
    }
    first_token_values: list[float] = []
    duration_values: list[float] = []
    tps_values: list[float] = []

    for row in rows:
        bucket = by_provider.setdefault(
            row.provider,
            {
                "provider": row.provider,
                "model": row.model,
                "calls": 0,
                "success": 0,
                "rate_limited": 0,
                "errors": 0,
                "fallbacks": 0,
                "avg_first_token_ms": None,
                "avg_duration_ms": None,
                "avg_tokens_per_second": None,
                "total_tokens": 0,
                "_first": [],
                "_duration": [],
                "_tps": [],
            },
        )
        bucket["calls"] += 1
        bucket["fallbacks"] += 1 if row.fallback_from else 0
        totals["fallbacks"] += 1 if row.fallback_from else 0
        if row.status == "success":
            bucket["success"] += 1
            totals["success"] += 1
        elif row.status == "rate_limited":
            bucket["rate_limited"] += 1
            totals["rate_limited"] += 1
        else:
            bucket["errors"] += 1
            totals["errors"] += 1
        if row.first_token_ms is not None:
            bucket["_first"].append(float(row.first_token_ms))
            first_token_values.append(float(row.first_token_ms))
        if row.duration_ms is not None:
            bucket["_duration"].append(float(row.duration_ms))
            duration_values.append(float(row.duration_ms))
        if row.tokens_per_second is not None:
            bucket["_tps"].append(float(row.tokens_per_second))
            tps_values.append(float(row.tokens_per_second))
        token_count = int(row.total_tokens or 0)
        bucket["total_tokens"] += token_count
        totals["total_tokens"] += token_count

    provider_rows = []
    for bucket in by_provider.values():
        bucket["avg_first_token_ms"] = _avg(bucket.pop("_first"))
        bucket["avg_duration_ms"] = _avg(bucket.pop("_duration"))
        bucket["avg_tokens_per_second"] = _avg(bucket.pop("_tps"))
        provider_rows.append(bucket)

    totals["avg_first_token_ms"] = _avg(first_token_values)
    totals["avg_duration_ms"] = _avg(duration_values)
    totals["avg_tokens_per_second"] = _avg(tps_values)
    return {
        "window_minutes": window_minutes,
        "since": since.isoformat(),
        "totals": totals,
        "providers": sorted(provider_rows, key=lambda item: item["calls"], reverse=True),
        "health": provider_health(),
    }


def _since_days(days: int | None) -> datetime | None:
    if days is None:
        return None
    return datetime.now() - timedelta(days=max(1, days))


def _empty_usage() -> dict[str, Any]:
    return {
        "calls": 0,
        "success": 0,
        "errors": 0,
        "rate_limited": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "estimated_tokens": 0,
        "output_chars": 0,
        "first_started_at": None,
        "last_started_at": None,
        "features": {},
        "providers": {},
    }


def _usage_by_meta_key(
    db,
    *,
    scope: str,
    key: str,
    allowed_ids: set[str],
    since: datetime | None,
) -> dict[str, dict[str, Any]]:
    if not allowed_ids:
        return {}
    query = db.query(LLMCallModel)
    if since:
        query = query.filter(LLMCallModel.started_at >= since)
    rows = query.order_by(LLMCallModel.started_at.asc(), LLMCallModel.id.asc()).all()
    usage: dict[str, dict[str, Any]] = {}
    for row in rows:
        meta = row.meta if isinstance(row.meta, dict) else {}
        if meta.get("scope") != scope:
            continue
        group_id = str(meta.get(key) or "")
        if group_id not in allowed_ids:
            continue
        bucket = usage.setdefault(group_id, _empty_usage())
        _add_call_to_usage(bucket, row)
    return usage


def _add_call_to_usage(bucket: dict[str, Any], row: LLMCallModel) -> None:
    bucket["calls"] += 1
    if row.status == "success":
        bucket["success"] += 1
    elif row.status == "rate_limited":
        bucket["rate_limited"] += 1
    else:
        bucket["errors"] += 1
    bucket["prompt_tokens"] += int(row.prompt_tokens or 0)
    bucket["completion_tokens"] += int(row.completion_tokens or 0)
    token_count = int(row.total_tokens or 0)
    bucket["total_tokens"] += token_count
    bucket["estimated_tokens"] += int(_meta_value(row, "estimated_total_tokens") or 0)
    bucket["output_chars"] += int(row.output_chars or 0)
    bucket["features"][row.feature] = int(bucket["features"].get(row.feature, 0)) + 1
    bucket["providers"][row.provider] = int(bucket["providers"].get(row.provider, 0)) + 1
    if row.started_at:
        started = row.started_at.isoformat()
        bucket["first_started_at"] = bucket["first_started_at"] or started
        bucket["last_started_at"] = started


def _meta_value(row: LLMCallModel, key: str) -> Any:
    meta = row.meta if isinstance(row.meta, dict) else {}
    return meta.get(key)


def _coding_task_usage_row(row: CodingTaskModel, usage: dict[str, Any] | None) -> dict[str, Any]:
    usage = usage or _empty_usage()
    return {
        "task_id": row.task_id,
        "project_id": row.project_id,
        "title": _first_line(row.requirement),
        "status": row.status,
        "stage": row.stage,
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        **usage,
    }


def _chat_thread_rows(db, *, project_id: str | None, since: datetime | None) -> list[dict[str, Any]]:
    query = db.query(ChatMessageModel)
    if project_id:
        query = query.filter(ChatMessageModel.project_id == project_id)
    if since:
        query = query.filter(ChatMessageModel.created_at >= since)
    rows = query.order_by(ChatMessageModel.created_at.desc(), ChatMessageModel.id.desc()).all()
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        group_key = (row.thread_id, row.topic_thread_id, row.project_id)
        bucket = grouped.setdefault(
            group_key,
            {
                "session_id": row.thread_id,
                "topic_thread_id": row.topic_thread_id,
                "project_id": row.project_id,
                "last_at": row.created_at,
                "first_at": row.created_at,
                "message_count": 0,
                "turn_ids": set(),
            },
        )
        if row.created_at and (bucket["first_at"] is None or row.created_at < bucket["first_at"]):
            bucket["first_at"] = row.created_at
        if row.role not in {"system_note", "summary"}:
            bucket["message_count"] += 1
            bucket["turn_ids"].add(row.turn_id)
    ordered = sorted(grouped.values(), key=lambda item: item["last_at"] or datetime.min, reverse=True)
    for item in ordered:
        item["turn_count"] = len(item.pop("turn_ids"))
        item["first_at"] = item["first_at"].isoformat() if item["first_at"] else None
        item["last_at"] = item["last_at"].isoformat() if item["last_at"] else None
    return ordered


def _chat_usage_row(row: dict[str, Any], usage: dict[str, Any] | None) -> dict[str, Any]:
    usage = usage or _empty_usage()
    return {
        **row,
        **usage,
    }


def _first_line(text: str) -> str:
    return next((line.strip() for line in (text or "").splitlines() if line.strip()), "Coding task")[:120]


def _avg(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 2) if values else None


def _serialize_call(row: LLMCallModel) -> dict[str, Any]:
    return {
        "id": row.id,
        "request_id": row.request_id,
        "feature": row.feature,
        "provider": row.provider,
        "model": row.model,
        "attempt_index": row.attempt_index,
        "fallback_from": row.fallback_from,
        "status": row.status,
        "streaming": bool(row.streaming),
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "first_token_ms": row.first_token_ms,
        "duration_ms": row.duration_ms,
        "prompt_tokens": row.prompt_tokens,
        "completion_tokens": row.completion_tokens,
        "total_tokens": row.total_tokens,
        "output_chars": row.output_chars,
        "tokens_per_second": row.tokens_per_second,
        "error_type": row.error_type,
        "error_message": row.error_message,
        "meta": row.meta,
    }
