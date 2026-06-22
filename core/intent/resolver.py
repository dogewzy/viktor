"""Glossary-first project intent resolver."""

from __future__ import annotations

import re

from core.intent.glossary_retriever import retrieve_glossary
from core.intent.knowledge_retriever import retrieve_knowledge_notes
from core.intent.models import IntentRoute, IntentType, ToolStrategy

_CONCEPT_RE = re.compile(r"(概念|口径|定义|如何区分|怎么区分|字段|映射|是什么|什么意思)")
_DATA_RE = re.compile(r"(统计|分布|数量|多少|count|聚合|每天|每日|趋势|新进|待审核|命中量)", re.I)
_LOG_RE = re.compile(r"(日志|报错|异常|pod|k8s|重启|超时|network error|500|502|504)", re.I)
_CODE_RE = re.compile(r"(代码|函数|实现|grep|仓库|方法|类|字段在哪)", re.I)
_WEAK_FILTER_RE = re.compile(r"(最近\s*\d+\s*天|近\s*\d+\s*天|每天|每日|全部|所有)")


class ProjectIntentResolver:
    """Resolve project-scoped business intent before the main Agent runs."""

    def __init__(self, *, glossary_limit: int = 10, knowledge_limit: int = 8) -> None:
        self.glossary_limit = glossary_limit
        self.knowledge_limit = knowledge_limit

    def resolve(
        self,
        project_id: str,
        user_message: str,
        *,
        recent_context: str = "",
    ) -> IntentRoute:
        query = "\n".join(part for part in [recent_context, user_message] if part.strip())
        glossary_hits, missing_terms = retrieve_glossary(
            project_id,
            query,
            limit=self.glossary_limit,
        )
        knowledge_query = " ".join([
            query,
            " ".join(hit.title for hit in glossary_hits),
            " ".join(keyword for hit in glossary_hits for keyword in hit.code_keywords),
        ])
        knowledge_hits = retrieve_knowledge_notes(
            project_id,
            knowledge_query,
            limit=self.knowledge_limit,
        )

        intent_type = self._intent_type(user_message)
        risk_flags = self._risk_flags(user_message, missing_terms)
        tool_strategy = self._tool_strategy(
            user_message,
            intent_type=intent_type,
            missing_terms=missing_terms,
            glossary_count=len(glossary_hits),
            risk_flags=risk_flags,
        )
        questions = self._blocking_questions(missing_terms, risk_flags, tool_strategy)
        confidence = self._confidence(glossary_hits, knowledge_hits, missing_terms)
        evidence = [
            f"glossary:{hit.title} score={hit.score}"
            for hit in glossary_hits[:5]
        ]
        evidence.extend(
            f"knowledge:{hit.title} score={hit.score}"
            for hit in knowledge_hits[:3]
        )

        return IntentRoute(
            project_id=project_id,
            user_message=user_message,
            intent_type=intent_type,
            tool_strategy=tool_strategy,
            matched_glossaries=glossary_hits,
            matched_knowledge_notes=knowledge_hits,
            missing_terms=missing_terms,
            risk_flags=risk_flags,
            blocking_questions=questions,
            confidence=confidence,
            evidence=evidence,
        )

    def _intent_type(self, message: str) -> IntentType:
        if _LOG_RE.search(message):
            return "incident_diagnosis"
        if _CODE_RE.search(message):
            return "code_debug"
        if _CONCEPT_RE.search(message) and _DATA_RE.search(message):
            return "mixed"
        if _CONCEPT_RE.search(message):
            return "term_mapping"
        if _DATA_RE.search(message):
            return "data_query"
        return "mixed"

    def _risk_flags(self, message: str, missing_terms: list[str]) -> list[str]:
        flags: list[str] = []
        if _DATA_RE.search(message) and _WEAK_FILTER_RE.search(message):
            flags.append("large_table_aggregation")
        if _CONCEPT_RE.search(message) and missing_terms:
            flags.append("missing_semantic_mapping")
        if "时间" not in message and any(term in message for term in ("今天", "最近", "每天", "每日")):
            flags.append("relative_time_scope")
        return flags

    def _tool_strategy(
        self,
        message: str,
        *,
        intent_type: IntentType,
        missing_terms: list[str],
        glossary_count: int,
        risk_flags: list[str],
    ) -> ToolStrategy:
        if intent_type == "incident_diagnosis":
            return "log_first"
        if intent_type == "direct_answer":
            return "direct_answer"
        if "missing_semantic_mapping" in risk_flags:
            return "glossary_then_code" if glossary_count else "clarify_first"
        if intent_type in {"term_mapping", "code_debug"}:
            return "glossary_then_code" if missing_terms else "glossary_only"
        if "large_table_aggregation" in risk_flags and missing_terms:
            return "clarify_first"
        if intent_type in {"data_query", "metric_query", "mixed"}:
            return "glossary_then_db"
        return "glossary_then_db"

    def _blocking_questions(
        self,
        missing_terms: list[str],
        risk_flags: list[str],
        tool_strategy: ToolStrategy,
    ) -> list[dict]:
        if tool_strategy != "clarify_first":
            return []
        if missing_terms:
            return [{
                "id": "semantic_mapping",
                "type": "term_disambiguation",
                "question": f"这些业务词需要先确认含义：{'、'.join(missing_terms[:4])}。它们在当前项目里对应什么口径？",
                "recommended": "provide_mapping",
                "blocking": True,
                "evidence": ["当前 project glossary 未找到足够明确的术语映射。"],
                "options": [
                    {
                        "label": "我补充口径",
                        "value": "provide_mapping",
                        "description": "用户补充业务定义后再查库或查代码。",
                    },
                    {
                        "label": "先查代码验证",
                        "value": "code_probe",
                        "description": "Agent 先用代码搜索补齐这些术语映射。",
                    },
                ],
            }]
        if "large_table_aggregation" in risk_flags:
            return [{
                "id": "large_table_scope",
                "type": "scope",
                "question": "这个统计可能涉及大表聚合，是否需要先收窄范围？",
                "recommended": "narrow_scope",
                "blocking": True,
                "evidence": ["命中 large_table_aggregation 风险。"],
                "options": [
                    {"label": "先收窄范围", "value": "narrow_scope", "description": "减少 SQL 超时风险。"},
                    {"label": "继续精确统计", "value": "exact_query", "description": "可能更慢，并受 SQL 超时保护。"},
                ],
            }]
        return []

    def _confidence(self, glossary_hits: list, knowledge_hits: list, missing_terms: list[str]) -> float:
        score = 0.35
        if glossary_hits:
            score += 0.35
        if knowledge_hits:
            score += 0.2
        if missing_terms:
            score -= 0.2
        return max(0.0, min(0.95, round(score, 2)))


def resolve_project_intent(
    project_id: str,
    user_message: str,
    *,
    recent_context: str = "",
    glossary_limit: int = 10,
    knowledge_limit: int = 8,
) -> IntentRoute:
    return ProjectIntentResolver(
        glossary_limit=glossary_limit,
        knowledge_limit=knowledge_limit,
    ).resolve(project_id, user_message, recent_context=recent_context)
