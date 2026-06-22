"""LLM analyst that turns trace digests into learning candidate drafts."""

from __future__ import annotations

import json
import re
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from loguru import logger

from core.learning.models import LearningCandidateDraft, TraceDigest
from core.llm_client import create_llm


_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.S)


def extract_learning_candidates(digest: TraceDigest) -> list[LearningCandidateDraft]:
    """Ask a small non-thinking LLM for reusable knowledge candidates."""
    if not digest.project_id:
        return []
    try:
        llm = create_llm(thinking=False, feature="trace_learning_candidate_extraction")
        response = llm.invoke([
            SystemMessage(content=_SYSTEM_PROMPT),
            HumanMessage(content=_human_prompt(digest)),
        ])
        content = response.content if isinstance(response.content, str) else str(response.content or "")
        drafts = _coerce_candidates(_extract_json(content))
        if drafts:
            return drafts
    except Exception as e:  # noqa: BLE001
        logger.warning("trace learning LLM extraction failed, use conservative fallback: {}", e)
    return _fallback_candidates(digest)


_SYSTEM_PROMPT = """
你是 Viktor 的 trace 复盘分析员。你的任务是从一次 Agent trace 中提炼「可复用」长期上下文候选。

只能输出 JSON object，不要 Markdown。候选必须满足：
1. 只沉淀可泛化知识：字段映射、术语映射、指标口径、可复用 workflow、反模式。
2. 不把一次 trace 的具体统计结果、具体数据值、临时错误文本沉淀为事实。
3. 每条候选必须包含 evidence_event_seq，引用 trace 中支持它的事件序号。
4. target_type 只能是 glossary、knowledge_note、skill、agent_rule。
5. glossary payload 必须能转成 GlossaryItem；knowledge_note payload 必须能转成 KnowledgeNoteItem；skill payload 必须能转成 SkillItem。
6. agent_rule 只生成 AGENTS.md patch 建议，不能要求自动改文件。

输出格式：
{
  "candidates": [
    {
      "target_type": "glossary",
      "target_id": "short-id",
      "title": "术语：漫剧",
      "content": "漫剧是待确认的业务分类词，需要补齐代码关键词。",
      "payload": {"term": "漫剧", "aliases": [], "code_keywords": [], "description": "...", "enabled": true},
      "confidence": 0.72,
      "risk_level": "medium",
      "evidence_event_seq": [2, 9]
    }
  ]
}
""".strip()


def _human_prompt(digest: TraceDigest) -> str:
    return f"""
Trace digest JSON:
{json.dumps(digest.model_dump(), ensure_ascii=False, indent=2)}

请输出候选 JSON。优先考虑：
- missing_terms 是否应成为 glossary 候选；
- 重复试错/SQL 拦截/跨 connector 失败是否应成为 knowledge_note 或 skill；
- 最终答案是否包含可复用 workflow 或指标口径。
""".strip()


def _extract_json(content: str) -> dict[str, Any]:
    match = _JSON_BLOCK_RE.search(content or "")
    if not match:
        return {}
    try:
        data = json.loads(match.group(0))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _coerce_candidates(data: dict[str, Any]) -> list[LearningCandidateDraft]:
    out: list[LearningCandidateDraft] = []
    for raw in data.get("candidates") or []:
        if not isinstance(raw, dict):
            continue
        try:
            draft = LearningCandidateDraft(**raw)
        except Exception:
            continue
        if draft.title.strip() and draft.evidence_event_seq:
            out.append(draft)
    return out[:12]


def _fallback_candidates(digest: TraceDigest) -> list[LearningCandidateDraft]:
    drafts: list[LearningCandidateDraft] = []
    evidence_seq = _first_event_seq(digest, "glossary_retrieval") or 1
    for term in digest.missing_terms[:6]:
        target_id = _slug(term)
        drafts.append(LearningCandidateDraft(
            target_type="glossary",
            target_id=target_id,
            title=f"术语：{term}",
            content=f"{term} 在 trace 中被识别为未覆盖业务术语，建议审核后补充业务含义与代码关键词。",
            payload={
                "term": term,
                "aliases": [],
                "code_keywords": [],
                "description": f"自动 trace learning 候选：{term} 是一次问题中出现但当前 Glossary 未覆盖的业务词，需人工确认口径。",
                "enabled": True,
            },
            confidence=0.55,
            risk_level="medium",
            evidence_event_seq=[evidence_seq],
        ))
    if digest.repeated_tools or digest.sql_blocked_events or digest.connector_failures:
        evidence = digest.sql_blocked_events[:3] or [_first_event_seq(digest, "tool_end") or 1]
        title = "Trace 复盘：查询路径需要沉淀为可复用经验"
        content = "\n".join([
            "自动 trace learning 发现本轮存在可复用操作经验，建议审核后整理为知识笔记或 Skill。",
            f"- 重复工具: {', '.join(digest.repeated_tools) or '无'}",
            f"- 失败工具: {', '.join(digest.failed_tools) or '无'}",
            f"- Connector 线索: {', '.join(digest.connector_failures) or '无'}",
        ])
        drafts.append(LearningCandidateDraft(
            target_type="knowledge_note",
            target_id="trace-query-workflow-note",
            title=title,
            content=content,
            payload={
                "kind": "pitfall",
                "scope": "trace_learning",
                "title": title,
                "content": content,
                "tags": ["trace_learning"],
                "enabled": True,
                "source": "manual",
            },
            confidence=0.5,
            risk_level="medium",
            evidence_event_seq=evidence,
        ))
    return drafts[:8]


def _first_event_seq(digest: TraceDigest, event_type: str) -> int:
    for event in digest.events:
        if event.get("event_type") == event_type:
            return int(event.get("event_seq") or 0)
    return 0


def _slug(text: str) -> str:
    ascii_part = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    if ascii_part:
        return ascii_part[:64]
    encoded = "-".join(f"{ord(ch):x}" for ch in text.strip()[:12])
    return f"term-{encoded}"[:64]
