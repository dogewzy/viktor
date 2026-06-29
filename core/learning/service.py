"""Service layer for automatic trace learning candidates."""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any

from loguru import logger
from sqlalchemy import or_

from core.audit.redaction import redact_payload
from core.database import SessionLocal
from core.learning.candidate_extractor import extract_learning_candidates
from core.learning.models import LearningCandidateDraft
from core.learning.trace_analyzer import build_trace_digest
from core.models import LearningCandidateModel
from core.registry import GlossaryItem, KnowledgeNoteItem, SkillItem, registry
from core.registry_persistence import (
    upsert_glossary_model,
    upsert_knowledge_note_model,
    upsert_skill_model,
)


_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="trace-learning")


def schedule_trace_learning(trace_id: str) -> None:
    """Submit a best-effort background learning job."""
    if not trace_id:
        return
    try:
        _EXECUTOR.submit(_run_trace_learning_safe, trace_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("trace learning 调度失败: trace_id={}, error={}", trace_id, e)


def run_trace_learning(trace_id: str) -> dict[str, Any]:
    """Analyze one trace immediately and save pending learning candidates."""
    digest = build_trace_digest(trace_id)
    if not digest.project_id:
        return {"ok": False, "trace_id": trace_id, "inserted": 0, "message": "trace 不存在或缺少 project_id"}
    drafts = extract_learning_candidates(digest)
    inserted: list[dict[str, Any]] = []
    skipped = 0
    for draft in drafts:
        row = _save_candidate(digest.project_id, trace_id, draft)
        if row is None:
            skipped += 1
            continue
        inserted.append(_serialize_candidate(row))
    return {
        "ok": True,
        "trace_id": trace_id,
        "project_id": digest.project_id,
        "inserted": len(inserted),
        "skipped": skipped,
        "items": inserted,
    }


def list_learning_candidates(
    *,
    project_id: str | None = None,
    status: str | None = None,
    target_type: str | None = None,
    source_trace_id: str | None = None,
    q: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    limit = min(max(int(limit), 1), 1000)
    offset = max(int(offset), 0)
    db = SessionLocal()
    try:
        query = db.query(LearningCandidateModel)
        if project_id:
            query = query.filter(LearningCandidateModel.project_id == project_id)
        if status:
            query = query.filter(LearningCandidateModel.status == status)
        if target_type:
            query = query.filter(LearningCandidateModel.target_type == target_type)
        if source_trace_id:
            query = query.filter(LearningCandidateModel.source_trace_id == source_trace_id)
        if q:
            query = query.filter(or_(
                LearningCandidateModel.candidate_id.contains(q),
                LearningCandidateModel.target_id.contains(q),
                LearningCandidateModel.title.contains(q),
                LearningCandidateModel.content.contains(q),
            ))
        total = query.count()
        rows = (
            query.order_by(LearningCandidateModel.created_at.desc(), LearningCandidateModel.candidate_id.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )
        return {"items": [_serialize_candidate(row) for row in rows], "total": total, "limit": limit, "offset": offset}
    finally:
        db.close()


def update_learning_candidate(candidate_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    """Edit candidate metadata/content before human apply."""
    allowed = {"target_id", "title", "content", "payload", "confidence", "risk_level", "status"}
    db = SessionLocal()
    try:
        row = db.get(LearningCandidateModel, candidate_id)
        if row is None:
            raise ValueError(f"候选 {candidate_id} 不存在")
        for key, value in patch.items():
            if key not in allowed:
                continue
            if key == "payload":
                value = redact_payload(value or {})
            if key == "content":
                value = str(redact_payload({"content": value}).get("content") or "")
            setattr(row, key, value)
        row.updated_at = datetime.now()
        db.commit()
        db.refresh(row)
        return _serialize_candidate(row)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def apply_learning_candidate(candidate_id: str) -> dict[str, Any]:
    """Apply a reviewed candidate into the formal registry when supported."""
    db = SessionLocal()
    try:
        row = db.get(LearningCandidateModel, candidate_id)
        if row is None:
            raise ValueError(f"候选 {candidate_id} 不存在")
        if row.status != "pending":
            raise ValueError(f"候选状态为 {row.status}，不能重复采纳")
        if row.target_type == "glossary":
            item = _glossary_item(row)
            registry.register_glossary(item)
            upsert_glossary_model(db, item)
            applied = item.model_dump()
        elif row.target_type == "knowledge_note":
            item = _knowledge_note_item(row)
            registry.register_knowledge_note(item)
            upsert_knowledge_note_model(db, item)
            applied = item.model_dump()
        elif row.target_type == "skill":
            item = _skill_item(row)
            registry.register_skill(item)
            upsert_skill_model(db, item)
            applied = item.model_dump()
        elif row.target_type == "agent_rule":
            applied = {
                "message": "agent_rule 首版只生成 AGENTS.md patch 建议，未自动修改文件。",
                "patch_suggestion": row.content,
            }
        else:
            raise ValueError(f"不支持的候选类型: {row.target_type}")
        row.status = "applied"
        row.updated_at = datetime.now()
        db.commit()
        return {"ok": True, "candidate": _serialize_candidate(row), "applied": applied}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _run_trace_learning_safe(trace_id: str) -> None:
    try:
        result = run_trace_learning(trace_id)
        logger.info(
            "trace learning 完成: trace_id={}, inserted={}, skipped={}",
            trace_id,
            result.get("inserted"),
            result.get("skipped"),
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("trace learning 失败: trace_id={}, error={}", trace_id, e)


def _save_candidate(project_id: str, trace_id: str, draft: LearningCandidateDraft) -> LearningCandidateModel | None:
    if not draft.evidence_event_seq:
        return None
    target_id = (draft.target_id or _target_id_from_title(draft.title)).strip()[:128]
    candidate_id = _candidate_id(trace_id, draft.target_type, target_id, draft.title, draft.content)
    payload = redact_payload(draft.payload or {})
    payload = payload if isinstance(payload, dict) else {"value": payload}
    payload["evidence_event_seq"] = [int(seq) for seq in draft.evidence_event_seq if int(seq) > 0]
    review = _review_flags(project_id, draft.target_type, target_id)
    if review:
        payload["review"] = review
    content = str(redact_payload({"content": draft.content}).get("content") or "")

    db = SessionLocal()
    try:
        existing = db.get(LearningCandidateModel, candidate_id)
        if existing is not None:
            return None
        duplicate = (
            db.query(LearningCandidateModel)
            .filter(
                LearningCandidateModel.project_id == project_id,
                LearningCandidateModel.source_trace_id == trace_id,
                LearningCandidateModel.target_type == draft.target_type,
                LearningCandidateModel.target_id == target_id,
            )
            .first()
        )
        if duplicate is not None:
            return None
        pending_conflict = (
            db.query(LearningCandidateModel)
            .filter(
                LearningCandidateModel.project_id == project_id,
                LearningCandidateModel.target_type == draft.target_type,
                LearningCandidateModel.target_id == target_id,
                LearningCandidateModel.status == "pending",
            )
            .first()
        )
        if pending_conflict is not None:
            payload.setdefault("review", {})["pending_conflict_candidate_id"] = pending_conflict.candidate_id
        row = LearningCandidateModel(
            candidate_id=candidate_id,
            project_id=project_id,
            source_trace_id=trace_id,
            target_type=draft.target_type,
            target_id=target_id,
            title=draft.title.strip()[:512],
            content=content,
            payload=payload,
            confidence=max(0.0, min(1.0, float(draft.confidence))),
            risk_level=draft.risk_level,
            status="pending",
            created_at=datetime.now(),
            updated_at=datetime.now(),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _review_flags(project_id: str, target_type: str, target_id: str) -> dict[str, Any]:
    flags: dict[str, Any] = {}
    if not target_id:
        return flags
    if target_type == "glossary" and registry.get_glossary(project_id, target_id):
        flags["target_conflict"] = "formal_glossary_exists"
    elif target_type == "knowledge_note" and registry.get_knowledge_note(project_id, target_id):
        flags["target_conflict"] = "formal_knowledge_note_exists"
    elif target_type == "skill" and registry.get_skill(project_id, target_id):
        flags["target_conflict"] = "formal_skill_exists"
    return flags


def _glossary_item(row: LearningCandidateModel) -> GlossaryItem:
    payload = dict(row.payload or {})
    return GlossaryItem(
        id=row.target_id,
        project_id=row.project_id,
        term=str(payload.get("term") or row.title).replace("术语：", "").strip(),
        aliases=list(payload.get("aliases") or []),
        code_keywords=list(payload.get("code_keywords") or []),
        description=str(payload.get("description") or row.content),
        enabled=bool(payload.get("enabled", True)),
    )


def _knowledge_note_item(row: LearningCandidateModel) -> KnowledgeNoteItem:
    payload = dict(row.payload or {})
    return KnowledgeNoteItem(
        id=row.target_id,
        project_id=row.project_id,
        kind=payload.get("kind") or "pitfall",
        scope=str(payload.get("scope") or "trace_learning"),
        title=str(payload.get("title") or row.title),
        content=str(payload.get("content") or row.content),
        tags=list(payload.get("tags") or ["trace_learning"]),
        enabled=bool(payload.get("enabled", True)),
        source=payload.get("source") or "manual",
    )


def _skill_item(row: LearningCandidateModel) -> SkillItem:
    payload = _normalize_skill_payload(dict(row.payload or {}))
    payload["id"] = row.target_id
    payload["project_id"] = row.project_id
    payload.setdefault("name", row.title)
    payload.setdefault("description", row.content[:200])
    payload.setdefault("source_type", "manual")
    payload.setdefault("raw_content", row.content)
    payload.setdefault("status", "enabled")
    return SkillItem(**payload)


# LLM 偶尔会自创字段名（steps/triggers/anti_patterns…），而 SkillItem 默认 extra='ignore'
# 会把这些键连同内容静默丢弃。这里把别名归一化回 SkillItem 真实字段，没有对应字段的
# 内容（前置条件/反模式/SQL 模板等）并入 instructions，避免采纳后得到空壳 skill。
_SKILL_INSTRUCTION_ALIASES = ("steps", "workflow", "procedure", "operations")
# 没有专属字段、但常含干货的别名键 → 以「[标签] 内容」并入 instructions（单复数都列出）
_SKILL_NOTE_ALIASES = (
    ("preconditions", "前置条件"), ("precondition", "前置条件"),
    ("anti_patterns", "反模式"), ("anti_pattern", "反模式"),
    ("sql_template", "SQL 模板"), ("sql", "SQL"),
    ("applicable_tables", "适用表"), ("applicable_connectors", "适用连接器"),
    ("connector_id", "连接器"),
)


def _step_to_str(value: Any) -> str:
    """把单个 step 归一成可读字符串：dict 形态优先取 action/detail，否则纯文本。"""
    if isinstance(value, dict):
        action = str(value.get("action") or value.get("title") or value.get("name") or "").strip()
        detail = str(value.get("detail") or value.get("description") or value.get("content") or "").strip()
        text = "：".join(p for p in (action, detail) if p) or detail or action
        if not text:  # 没有约定键时退回紧凑 JSON，避免丢内容
            text = json.dumps(value, ensure_ascii=False)
        return text
    return str(value).strip()


def _normalize_skill_payload(payload: dict[str, Any]) -> dict[str, Any]:
    def _as_str_list(value: Any) -> list[str]:
        if value is None or value == "":
            return []
        if isinstance(value, list):
            return [s for s in (_step_to_str(v) for v in value) if s]
        s = _step_to_str(value)
        return [s] if s else []

    instructions = _as_str_list(payload.get("instructions"))
    for alias in _SKILL_INSTRUCTION_ALIASES:
        if alias in payload:
            instructions.extend(_as_str_list(payload.pop(alias)))
    for key, prefix in _SKILL_NOTE_ALIASES:
        if key in payload:
            for line in _as_str_list(payload.pop(key)):
                instructions.append(f"[{prefix}] {line}")
    if instructions:
        payload["instructions"] = instructions

    # triggers（字符串/字符串数组）→ trigger_examples（SkillTriggerExample 结构）
    if "trigger_examples" not in payload and "triggers" in payload:
        payload["trigger_examples"] = [
            {"text": text, "source": "llm_expanded", "confirmed": False}
            for text in _as_str_list(payload.pop("triggers"))
        ]

    return payload


def _candidate_id(trace_id: str, target_type: str, target_id: str, title: str, content: str) -> str:
    raw = "\n".join([trace_id, target_type, target_id, title, content])
    return "lc_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _target_id_from_title(title: str) -> str:
    text = (title or "candidate").strip().lower()
    ascii_part = "".join(ch if ch.isalnum() and ch.isascii() else "-" for ch in text)
    ascii_part = "-".join(part for part in ascii_part.split("-") if part)
    if ascii_part:
        return ascii_part[:80]
    encoded = "-".join(f"{ord(ch):x}" for ch in text[:12])
    return f"candidate-{encoded}"[:80]


def _serialize_candidate(row: LearningCandidateModel) -> dict[str, Any]:
    return {
        "id": row.candidate_id,
        "candidate_id": row.candidate_id,
        "project_id": row.project_id,
        "source_trace_id": row.source_trace_id,
        "target_type": row.target_type,
        "target_id": row.target_id,
        "title": row.title,
        "content": row.content,
        "payload": row.payload or {},
        "confidence": row.confidence,
        "risk_level": row.risk_level,
        "status": row.status,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
