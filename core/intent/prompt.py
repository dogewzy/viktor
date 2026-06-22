"""Prompt formatting for project intent retrieval results."""

from __future__ import annotations

from core.intent.models import IntentRoute


def format_retrieval_context(route: IntentRoute) -> str:
    """Render concise route evidence for the main Agent prompt."""
    lines = ["## 本轮 Project Intent / 术语检索结果"]
    lines.append(f"- intent_type: {route.intent_type}")
    lines.append(f"- tool_strategy: {route.tool_strategy}")
    lines.append(f"- confidence: {route.confidence}")
    if route.risk_flags:
        lines.append(f"- risk_flags: {', '.join(route.risk_flags)}")
    if route.missing_terms:
        lines.append(f"- missing_terms: {', '.join(route.missing_terms)}")

    if route.matched_glossaries:
        lines.append("\n### 命中的业务术语（project-scoped glossary）")
        for hit in route.matched_glossaries:
            aliases = f" aliases={hit.aliases}" if hit.aliases else ""
            keywords = f" code_keywords={hit.code_keywords}" if hit.code_keywords else ""
            desc = f" — {hit.description}" if hit.description else ""
            lines.append(
                f"- {hit.title} (score={hit.score}, reason={hit.match_reason}){aliases}{keywords}{desc}"
            )

    if route.matched_knowledge_notes:
        lines.append("\n### 命中的业务知识（project-scoped knowledge notes）")
        for hit in route.matched_knowledge_notes:
            body = (hit.content or "").strip()
            if len(body) > 500:
                body = body[:500] + "..."
            lines.append(f"- [{hit.kind}] {hit.title} (score={hit.score})")
            if body:
                lines.append(f"  {body}")

    lines.append("\n### 执行约束")
    lines.append("- 先使用上述 glossary / knowledge note 解释用户业务词，不要自造字段含义。")
    lines.append("- 若 missing_terms 非空，先澄清或用代码验证补齐语义，再执行聚合 SQL。")
    lines.append("- DB 聚合前必须有 glossary、knowledge、澄清答案或代码证据支撑字段口径。")
    lines.append("- SQL 超时后不要换库、写脚本或重复大范围试错，应基于已有证据收口。")
    return "\n".join(lines).strip()
