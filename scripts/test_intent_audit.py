#!/usr/bin/env python3
"""Offline regression tests for glossary-first intent routing and audit redaction."""

from __future__ import annotations

import sys
import traceback

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.audit.redaction import redact_payload
import core.intent.glossary_retriever as glossary_retriever
from core.intent.resolver import resolve_project_intent
from core.intent.term_extractor import ExtractedTerm, TermExtractionResult
from core.intent.tokenizer import split_identifier, tokenize
from core.registry import GlossaryItem, KnowledgeNoteItem, ProjectItem, registry


PROJECT_A = "intent-audit-test-a"
PROJECT_B = "intent-audit-test-b"
_ORIGINAL_EXTRACT_QUERY_TERMS = glossary_retriever.extract_query_terms


def _mock_extract_query_terms(query: str) -> TermExtractionResult:
    terms: list[ExtractedTerm] = []
    if "帮我" in query:
        terms.append(ExtractedTerm(text="帮我", kind="colloquial_noise", confidence=0.99))
    if "看一下" in query:
        terms.append(ExtractedTerm(text="看一下", kind="colloquial_noise", confidence=0.99))
    for term in ("漫剧", "真人剧", "短剧", "母本"):
        if term in query:
            terms.append(ExtractedTerm(text=term, kind="business_term", confidence=0.9))
    for term in ("比例", "统计", "数量", "分布"):
        if term in query:
            terms.append(ExtractedTerm(text=term, kind="metric_word", confidence=0.9))
    return TermExtractionResult(terms=terms)


def _seed() -> None:
    for project_id in (PROJECT_A, PROJECT_B):
        registry.unregister_project(project_id)
        registry.register_project(ProjectItem(
            id=project_id,
            name=project_id,
            description="intent/audit offline test",
            git_url="https://example.com/repo.git",
            default_branch="master",
        ))
    registry.register_glossary(GlossaryItem(
        id="parent-work",
        project_id=PROJECT_A,
        term="母本",
        aliases=["原始作品", "源作品"],
        code_keywords=["parentWork", "parent_work_id", "originalWork"],
        description="剧集或样本所归属的原始作品概念。",
    ))
    registry.register_glossary(GlossaryItem(
        id="sample",
        project_id=PROJECT_A,
        term="样本",
        aliases=["视频样本"],
        code_keywords=["sample_id", "orders"],
        description="系统内用于匹配、审核和统计的视频样本。",
    ))
    registry.register_glossary(GlossaryItem(
        id="parent-work-other-project",
        project_id=PROJECT_B,
        term="母本",
        aliases=["项目B母本"],
        code_keywords=["other_parent_id"],
        description="另一个项目的同名术语，不应污染 project A。",
    ))
    registry.register_knowledge_note(KnowledgeNoteItem(
        id="parent-work-field",
        project_id=PROJECT_A,
        kind="field_semantics",
        scope="orders.parent_work",
        title="母本字段语义",
        content="母本用于把样本归因到原始作品，统计前需确认剧种维度是否已有字段。",
        tags=["母本", "parent_work"],
        source="manual",
    ))


def _cleanup() -> None:
    for project_id in (PROJECT_A, PROJECT_B):
        registry.unregister_project(project_id)


def test_tokenizer() -> None:
    tokens = tokenize("parentWork parent_work_id errorCode=1102 母本概念")
    assert "parent" in tokens and "work" in tokens
    assert "parentwork" in tokens
    assert "1102" in tokens
    assert "母本" in tokens
    assert split_identifier("parentWorkID")[:2] == ["parent", "work"]


def test_glossary_retrieval_and_project_isolation() -> None:
    hits, missing = glossary_retriever.retrieve_glossary(PROJECT_A, "漫剧和真人剧在母本概念上怎么区分？")
    assert hits and hits[0].title == "母本", [h.model_dump() for h in hits]
    assert "漫剧" in missing and "真人剧" in missing, missing
    _, noisy_missing = glossary_retriever.retrieve_glossary(PROJECT_A, "帮我看一下漫剧和短剧在母本中的比例")
    assert noisy_missing[:2] == ["漫剧", "短剧"], noisy_missing
    hits_b, _ = glossary_retriever.retrieve_glossary(PROJECT_B, "母本 parent_work_id")
    assert hits_b and hits_b[0].code_keywords == ["other_parent_id"], [h.model_dump() for h in hits_b]


def test_resolver_route() -> None:
    route = resolve_project_intent(PROJECT_A, "漫剧和真人剧在母本概念上怎么区分？")
    assert route.intent_type in {"term_mapping", "mixed"}
    assert route.tool_strategy == "glossary_then_code", route.model_dump()
    assert route.matched_glossaries and route.matched_glossaries[0].title == "母本"
    assert {"漫剧", "真人剧"} <= set(route.missing_terms)


def test_redaction() -> None:
    payload = {
        "headers": {
            "Authorization": "Bearer sk-test-abcdefghijklmnopqrstuvwxyz",
            "Cookie": "sid=abcdef1234567890; path=/",
        },
        "db_url": "mysql+pymysql://user:secret-password@db.example.com:3306/app",
        "nested": "password=abc123 token=very-secret-token",
    }
    redacted = redact_payload(payload)
    flat = str(redacted)
    assert "secret-password" not in flat
    assert "very-secret-token" not in flat
    assert "abcdefghijklmnopqrstuvwxyz" not in flat
    assert "[REDACTED]" in flat


def main() -> None:
    tests = [
        test_tokenizer,
        test_glossary_retrieval_and_project_isolation,
        test_resolver_route,
        test_redaction,
    ]
    glossary_retriever.extract_query_terms = _mock_extract_query_terms
    _seed()
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
        glossary_retriever.extract_query_terms = _ORIGINAL_EXTRACT_QUERY_TERMS
        _cleanup()


if __name__ == "__main__":
    main()
