#!/usr/bin/env python3
"""Offline regression tests for Ragas trace evaluation integration."""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import core.audit.recorder as recorder
import core.evaluation.ragas_adapter as ragas_adapter
import core.evaluation.service as evaluation_service
import core.learning.service as learning_service
import core.learning.trace_analyzer as trace_analyzer
from core.evaluation.models import TraceEvaluationResult
from core.models import AgentTraceEventModel, TraceEvaluationModel


PROJECT_ID = "trace-eval-test"
TRACE_ID = "trace-eval-fixture"

_ORIGINAL_RECORDER_SESSION = recorder.SessionLocal
_ORIGINAL_RAGAS_SESSION = ragas_adapter.SessionLocal
_ORIGINAL_SERVICE_SESSION = evaluation_service.SessionLocal
_ORIGINAL_ANALYZER_SESSION = trace_analyzer.SessionLocal
_ORIGINAL_LEARNING_SCHEDULE = learning_service.schedule_trace_learning
_ORIGINAL_EVALUATE_TRACE = evaluation_service.evaluate_trace


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
    TraceEvaluationModel.__table__.create(bind=engine)
    session_factory = sessionmaker(bind=engine)
    recorder.SessionLocal = session_factory
    ragas_adapter.SessionLocal = session_factory
    evaluation_service.SessionLocal = session_factory
    trace_analyzer.SessionLocal = session_factory
    learning_service.schedule_trace_learning = lambda trace_id: None


def _write_fixture_trace(trace_id: str = TRACE_ID, *, final: bool = True, contexts: bool = True, query: bool = True) -> None:
    if query:
        recorder.record_trace_event(
            trace_id=trace_id,
            event_type="glossary_retrieval",
            project_id=PROJECT_ID,
            payload={
                "query": "帮我判断这个告警是否和缓存穿透有关",
                "hits": [
                    {
                        "id": "cache-penetration",
                        "title": "缓存穿透",
                        "description": "请求绕过缓存直接打到后端数据源的故障模式。",
                        "code_keywords": ["cache_miss", "fallback_db"],
                    }
                ] if contexts else [],
                "missing_terms": [],
            },
        )
    if contexts:
        recorder.record_trace_event(
            trace_id=trace_id,
            event_type="tool_end",
            project_id=PROJECT_ID,
            payload={"tool": "log_query", "ok": True, "output": "cache_miss 激增，同时 fallback_db 查询量上升。"},
        )
    if final:
        recorder.record_trace_event(
            trace_id=trace_id,
            event_type="final_answer",
            project_id=PROJECT_ID,
            payload={"content": "从日志看 cache_miss 和 fallback_db 同时上升，符合缓存穿透排查方向。"},
        )


def _fake_success(trace_id: str, metrics: list[str]) -> TraceEvaluationResult:
    build = ragas_adapter.build_single_turn_sample(trace_id)
    return TraceEvaluationResult(
        trace_id=trace_id,
        project_id=build.project_id,
        status="succeeded",
        metrics=metrics,
        scores={"faithfulness": 0.42},
        sample_preview=build.sample_preview.model_dump(),
        diagnostics={"fake": True},
    )


def test_build_single_turn_sample() -> None:
    _write_fixture_trace()
    build = ragas_adapter.build_single_turn_sample(TRACE_ID)
    assert build.status == "ready", build
    sample = build.sample_preview
    assert sample.user_input
    assert sample.response
    assert sample.context_count >= 2
    assert sample.source_event_seq["user_input"] > 0


def test_run_evaluation_persists_and_is_idempotent() -> None:
    evaluation_service.evaluate_trace = _fake_success
    result = evaluation_service.run_trace_evaluation(TRACE_ID, force=True)
    assert result["status"] == "succeeded", result
    assert result["scores"]["faithfulness"] == 0.42
    again = evaluation_service.run_trace_evaluation(TRACE_ID)
    assert again["evaluation_id"] == result["evaluation_id"]
    db = evaluation_service.SessionLocal()
    try:
        count = db.query(TraceEvaluationModel).filter(TraceEvaluationModel.trace_id == TRACE_ID).count()
        assert count == 1
    finally:
        db.close()


def test_evaluator_exception_is_persisted_as_failed() -> None:
    trace_id = "trace-eval-failure"
    _write_fixture_trace(trace_id)

    def boom(trace_id: str, metrics: list[str]) -> TraceEvaluationResult:
        raise RuntimeError("judge unavailable")

    evaluation_service.evaluate_trace = boom
    result = evaluation_service.run_trace_evaluation(trace_id, force=True)
    assert result["status"] == "failed", result
    assert "judge unavailable" in result["error"]


def test_skipped_sample_reasons() -> None:
    _write_fixture_trace("trace-eval-no-final", final=False)
    _write_fixture_trace("trace-eval-no-context", contexts=False)
    _write_fixture_trace("trace-eval-no-query", query=False)
    assert ragas_adapter.build_single_turn_sample("trace-eval-no-final").error == "missing_final_answer"
    assert ragas_adapter.build_single_turn_sample("trace-eval-no-context").error == "missing_retrieved_contexts"
    assert ragas_adapter.build_single_turn_sample("trace-eval-no-query").error == "missing_user_input"


def test_trace_digest_reads_latest_successful_evaluation() -> None:
    evaluation_service.evaluate_trace = _fake_success
    evaluation_service.run_trace_evaluation(TRACE_ID, force=True)
    digest = trace_analyzer.build_trace_digest(TRACE_ID)
    assert digest.evaluation_summary["scores"]["faithfulness"] == 0.42
    assert digest.evaluation_summary["flags"] == ["low_faithfulness"]


def _restore() -> None:
    recorder.SessionLocal = _ORIGINAL_RECORDER_SESSION
    ragas_adapter.SessionLocal = _ORIGINAL_RAGAS_SESSION
    evaluation_service.SessionLocal = _ORIGINAL_SERVICE_SESSION
    trace_analyzer.SessionLocal = _ORIGINAL_ANALYZER_SESSION
    learning_service.schedule_trace_learning = _ORIGINAL_LEARNING_SCHEDULE
    evaluation_service.evaluate_trace = _ORIGINAL_EVALUATE_TRACE


def main() -> None:
    tests = [
        test_build_single_turn_sample,
        test_run_evaluation_persists_and_is_idempotent,
        test_evaluator_exception_is_persisted_as_failed,
        test_skipped_sample_reasons,
        test_trace_digest_reads_latest_successful_evaluation,
    ]
    _setup_db()
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
