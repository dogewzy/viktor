#!/usr/bin/env python3
"""Offline regression tests for automatic trace learning candidates."""

from __future__ import annotations

import sys
import traceback

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import core.audit.recorder as recorder
import core.learning.service as learning_service
import core.learning.trace_analyzer as trace_analyzer
from core.learning.models import LearningCandidateDraft
from core.models import AgentTraceEventModel, GlossaryModel, LearningCandidateModel
from core.registry import GlossaryItem, ProjectItem, registry


PROJECT_ID = "trace-learning-test"
TRACE_ID = "trace-learning-fixture"

_ORIGINAL_RECORDER_SESSION = recorder.SessionLocal
_ORIGINAL_ANALYZER_SESSION = trace_analyzer.SessionLocal
_ORIGINAL_SERVICE_SESSION = learning_service.SessionLocal
_ORIGINAL_EXTRACT = learning_service.extract_learning_candidates
_ORIGINAL_SCHEDULE = learning_service.schedule_trace_learning


def _setup_db() -> None:
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            """
            CREATE TABLE viktor_agent_trace_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trace_id VARCHAR(64) NOT NULL,
                event_seq INTEGER NOT NULL DEFAULT 0,
                event_type VARCHAR(64) NOT NULL,
                project_id VARCHAR(128) NOT NULL DEFAULT '',
                session_id VARCHAR(256) NOT NULL DEFAULT '',
                topic_thread_id VARCHAR(128) NOT NULL DEFAULT '',
                payload JSON NOT NULL,
                created_at DATETIME
            )
            """
        )
    LearningCandidateModel.__table__.create(bind=engine)
    GlossaryModel.__table__.create(bind=engine)
    session_factory = sessionmaker(bind=engine)
    recorder.SessionLocal = session_factory
    trace_analyzer.SessionLocal = session_factory
    learning_service.SessionLocal = session_factory


def _seed_registry() -> None:
    registry.unregister_project(PROJECT_ID)
    registry.register_project(ProjectItem(
        id=PROJECT_ID,
        name=PROJECT_ID,
        description="trace learning offline test",
        git_url="https://example.com/repo.git",
        default_branch="master",
    ))
    registry.register_glossary(GlossaryItem(
        id="parent-work",
        project_id=PROJECT_ID,
        term="母本",
        aliases=[],
        code_keywords=["parent_work_id"],
        description="已有正式术语，用于验证未 apply 前不变化。",
    ))


def _fake_candidate_extractor(digest):
    seq = 1
    for event in digest.events:
        if event.get("event_type") == "glossary_retrieval":
            seq = int(event.get("event_seq") or 1)
            break
    return [
        LearningCandidateDraft(
            target_type="glossary",
            target_id="man-ju",
            title="术语：漫剧",
            content="漫剧在 trace 中被识别为未覆盖业务术语，需人工确认含义和代码关键词。",
            payload={
                "term": "漫剧",
                "aliases": ["短剧"],
                "code_keywords": ["drama_type"],
                "description": "自动学习候选，人工审核后写入。",
                "enabled": True,
            },
            confidence=0.82,
            risk_level="medium",
            evidence_event_seq=[seq],
        )
    ]


def _write_fixture_trace() -> None:
    recorder.record_trace_event(
        trace_id=TRACE_ID,
        event_type="glossary_retrieval",
        project_id=PROJECT_ID,
        payload={
            "query": "帮我看一下漫剧和短剧在母本中的比例",
            "hits": [{"title": "母本"}],
            "missing_terms": ["漫剧", "短剧"],
        },
    )
    recorder.record_trace_event(
        trace_id=TRACE_ID,
        event_type="tool_end",
        project_id=PROJECT_ID,
        payload={"tool": "sql_query", "ok": False, "error": "SQL 被只读策略拦截"},
    )
    recorder.record_trace_event(
        trace_id=TRACE_ID,
        event_type="final_answer",
        project_id=PROJECT_ID,
        payload={"content": "最终回答包含证据和口径说明。"},
    )


def test_final_answer_schedule_failure_does_not_break_recording() -> None:
    def boom(trace_id: str) -> None:
        raise RuntimeError(f"schedule failed for {trace_id}")

    learning_service.schedule_trace_learning = boom
    recorder.record_trace_event(
        trace_id="trace-schedule-failure",
        event_type="final_answer",
        project_id=PROJECT_ID,
        payload={"content": "ok"},
    )
    db = recorder.SessionLocal()
    try:
        count = db.query(AgentTraceEventModel).filter(AgentTraceEventModel.trace_id == "trace-schedule-failure").count()
        assert count == 1
    finally:
        db.close()


def test_trace_learning_generates_pending_candidate_without_registry_write() -> None:
    learning_service.schedule_trace_learning = lambda trace_id: None
    learning_service.extract_learning_candidates = _fake_candidate_extractor
    _write_fixture_trace()
    result = learning_service.run_trace_learning(TRACE_ID)
    assert result["inserted"] == 1, result
    candidate = result["items"][0]
    assert candidate["status"] == "pending"
    assert candidate["target_type"] == "glossary"
    assert candidate["payload"]["evidence_event_seq"], candidate
    assert registry.get_glossary(PROJECT_ID, "man-ju") is None


def test_apply_candidate_updates_registry() -> None:
    items = learning_service.list_learning_candidates(project_id=PROJECT_ID, status="pending")["items"]
    candidate_id = items[0]["candidate_id"]
    applied = learning_service.apply_learning_candidate(candidate_id)
    assert applied["ok"] is True
    item = registry.get_glossary(PROJECT_ID, "man-ju")
    assert item is not None
    assert item.term == "漫剧"
    assert item.code_keywords == ["drama_type"]
    remaining = learning_service.list_learning_candidates(project_id=PROJECT_ID, status="pending")["total"]
    assert remaining == 0


def _restore() -> None:
    recorder.SessionLocal = _ORIGINAL_RECORDER_SESSION
    trace_analyzer.SessionLocal = _ORIGINAL_ANALYZER_SESSION
    learning_service.SessionLocal = _ORIGINAL_SERVICE_SESSION
    learning_service.extract_learning_candidates = _ORIGINAL_EXTRACT
    learning_service.schedule_trace_learning = _ORIGINAL_SCHEDULE
    registry.unregister_project(PROJECT_ID)


def main() -> None:
    tests = [
        test_final_answer_schedule_failure_does_not_break_recording,
        test_trace_learning_generates_pending_candidate_without_registry_write,
        test_apply_candidate_updates_registry,
    ]
    _setup_db()
    _seed_registry()
    try:
        for test in tests:
            try:
                test()
                print(f"PASS {test.__name__}")
            except Exception:  # noqa: BLE001
                print(f"FAIL {test.__name__}")
                traceback.print_exc()
                raise
    finally:
        _restore()


if __name__ == "__main__":
    main()
