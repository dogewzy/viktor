"""Shared clarification gate for webchat and coding flows."""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from langchain_core.messages import HumanMessage, SystemMessage
from loguru import logger

from core.llm_client import create_llm

ClarificationScenario = Literal["coding", "webchat"]


_BASE_SCHEMA = """
JSON Schema:
{
  "needs_clarification": true,
  "term_mappings": [
    {
      "user_term": "用户原始表达",
      "code_terms": ["代码术语、字段、表或内部概念"],
      "meaning": "本次理解",
      "confidence": 0.8,
      "evidence": ["证据"]
    }
  ],
  "questions": [
    {
      "id": "snake_case_id",
      "type": "term_disambiguation",
      "question": "需要用户裁决的问题",
      "recommended": "option_value",
      "blocking": true,
      "evidence": ["为什么必须问"],
      "options": [
        {
          "label": "展示名称",
          "value": "option_value",
          "description": "选择后 Agent 会怎样执行"
        }
      ]
    }
  ]
}
""".strip()


def _extract_json_object(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if not raw:
        return {}
    fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", raw, re.S)
    if fenced:
        raw = fenced.group(1).strip()
    if not raw.startswith("{"):
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            raw = raw[start:end + 1]
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def normalize_clarification(value: dict[str, Any]) -> dict[str, Any]:
    """Normalize LLM JSON into the stable clarification contract."""
    term_mappings = value.get("term_mappings")
    if not isinstance(term_mappings, list):
        term_mappings = []

    normalized_terms: list[dict[str, Any]] = []
    for item in term_mappings[:20]:
        if not isinstance(item, dict):
            continue
        user_term = str(item.get("user_term") or "").strip()
        code_terms = item.get("code_terms") if isinstance(item.get("code_terms"), list) else []
        meaning = str(item.get("meaning") or "").strip()
        evidence = item.get("evidence") if isinstance(item.get("evidence"), list) else []
        if user_term or code_terms or meaning:
            normalized_terms.append({
                "user_term": user_term[:120],
                "code_terms": [str(term).strip()[:160] for term in code_terms[:12] if str(term).strip()],
                "meaning": meaning[:800],
                "confidence": item.get("confidence"),
                "evidence": [str(ev).strip()[:500] for ev in evidence[:5] if str(ev).strip()],
            })

    normalized_questions: list[dict[str, Any]] = []
    questions = value.get("questions")
    if isinstance(questions, list):
        for index, item in enumerate(questions[:3], start=1):
            if not isinstance(item, dict):
                continue
            options = item.get("options")
            if not isinstance(options, list):
                continue
            normalized_options = []
            for option in options[:4]:
                if not isinstance(option, dict):
                    continue
                label = str(option.get("label") or "").strip()
                option_value = str(option.get("value") or "").strip()
                description = str(option.get("description") or "").strip()
                if label and option_value:
                    normalized_options.append({
                        "label": label[:80],
                        "value": option_value[:120],
                        "description": description[:500],
                    })
            question = str(item.get("question") or "").strip()
            if question and len(normalized_options) >= 2:
                evidence = item.get("evidence") if isinstance(item.get("evidence"), list) else []
                normalized_questions.append({
                    "id": str(item.get("id") or f"question_{index}").strip()[:80],
                    "type": str(item.get("type") or "clarification").strip()[:80],
                    "question": question[:500],
                    "recommended": str(item.get("recommended") or "").strip()[:120],
                    "blocking": bool(item.get("blocking", True)),
                    "evidence": [str(ev).strip()[:500] for ev in evidence[:5] if str(ev).strip()],
                    "options": normalized_options,
                })

    needs_clarification = bool(value.get("needs_clarification")) and any(q.get("blocking") for q in normalized_questions)
    return {
        "needs_clarification": needs_clarification,
        "term_mappings": normalized_terms,
        "questions": normalized_questions if needs_clarification else [],
    }


def _now_context() -> str:
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    return (
        f"当前日期：{now.date().isoformat()}；当前时区：Asia/Shanghai。"
        "用户未写年份的中文日期默认使用当前年份；例如“5月16日到现在”默认是当年的 5 月 16 日到当前时间。"
    )


def _system_prompt(scenario: ClarificationScenario) -> str:
    if scenario == "coding":
        scenario_rules = """
你是 Viktor Coding Agent 的 clarification gate。

你的任务不是生成 Plan，而是在正式 Plan 之前判断是否必须向用户提问。

判断重点：
1. 用户表达是否与 glossary 中的官方术语一致。
2. 代码里是否存在只有研发知道的内部术语、历史命名、简称或隐喻。
3. 同一个用户表达是否可能对应多个实现层级、状态字段、定时任务或外部系统动作。
4. 如果不提问，是否可能导致 Agent 改错业务层级、状态机、数据字段或高风险链路。
""".strip()
    else:
        scenario_rules = """
你是 Viktor Webchat 的 clarification gate。

你的任务不是回答问题，而是在进入多轮工具调用前判断是否必须先向用户确认口径。

判断重点：
1. 用户是否缺少会显著影响查询路径或成本的关键口径，例如年份、时间范围、租户/company_id、平台/website/tracking_website_id、环境、目标指标定义。
2. 用户请求是否会触发大表、大范围 COUNT/聚合/EXPLAIN；如果缺少强选择性条件，必须先提问，不要让 Agent 先试 SQL。
3. 如果项目 Skill 或知识库已经给出安全口径要求，必须按它判断是否阻塞。
4. 普通单样本、明确 ID、明确小范围排查不要误拦。
""".strip()

    return f"""
{scenario_rules}

通用规则：
1. 能基于 glossary、项目上下文、知识库、Skill 和只读代码探索高置信判断时，不要提问，只输出 term_mappings。
2. 只有存在会影响执行路径、查询成本或结果正确性的阻塞歧义时才提问。
3. 最多提出 3 个问题。
4. 每个问题必须有 2-4 个互斥选项，并给出 recommended。
5. 每个选项必须说明选择后的执行影响。
6. evidence 必须来自输入上下文；不要编造文件、行号或数据库事实。
7. 只输出 JSON，不要输出 Markdown、代码块或额外解释。

{_BASE_SCHEMA}
""".strip()


async def run_clarification_gate(
    *,
    scenario: ClarificationScenario,
    user_message: str,
    project_context: str,
    code_exploration: str = "",
    recent_context: str = "",
    feature: str | None = None,
) -> dict[str, Any]:
    """Run a cheap non-thinking clarification pass."""
    if not user_message.strip():
        return {"needs_clarification": False, "term_mappings": [], "questions": []}

    exploration_block = code_exploration.strip() or "无只读代码探索结果。"
    recent_block = recent_context.strip() or "无最近对话摘要。"
    human = (
        f"## 当前日期和时区\n{_now_context()}\n\n"
        f"## 项目上下文 / 知识 / Skill\n{project_context}\n\n"
        f"## 只读代码探索结果\n{exploration_block}\n\n"
        f"## 最近对话摘要\n{recent_block}\n\n"
        f"## 用户消息\n{user_message}"
    )
    try:
        llm = create_llm(thinking=False, feature=feature or f"{scenario}_clarification")
        response = await llm.ainvoke([
            SystemMessage(content=_system_prompt(scenario)),
            HumanMessage(content=human),
        ])
        content = response.content if isinstance(response.content, str) else str(response.content or "")
        return normalize_clarification(_extract_json_object(content))
    except Exception as e:  # noqa: BLE001
        logger.warning("clarification gate failed, scenario={}, error={}", scenario, e)
        return {"needs_clarification": False, "term_mappings": [], "questions": []}


def format_term_mappings_for_prompt(clarification: dict[str, Any]) -> str:
    mappings = clarification.get("term_mappings") if isinstance(clarification, dict) else []
    if not mappings:
        return ""
    lines = ["## 本轮前置澄清/术语映射"]
    for item in mappings[:12]:
        if not isinstance(item, dict):
            continue
        user_term = str(item.get("user_term") or "").strip()
        code_terms = ", ".join(str(term) for term in (item.get("code_terms") or []) if str(term).strip())
        meaning = str(item.get("meaning") or "").strip()
        head = user_term or code_terms or "术语映射"
        suffix = f" -> {code_terms}" if code_terms else ""
        lines.append(f"- {head}{suffix}: {meaning}")
    return "\n".join(lines).strip()


def format_clarification_text(clarification: dict[str, Any]) -> str:
    questions = clarification.get("questions") if isinstance(clarification, dict) else []
    if not questions:
        return ""
    lines = ["我需要先确认几个口径，避免查错或触发高成本查询："]
    for index, item in enumerate(questions[:3], start=1):
        if not isinstance(item, dict):
            continue
        lines.append(f"\n{index}. {item.get('question')}")
        recommended = str(item.get("recommended") or "").strip()
        for option in item.get("options") or []:
            if not isinstance(option, dict):
                continue
            marker = "（推荐）" if recommended and option.get("value") == recommended else ""
            lines.append(f"- {option.get('label')}{marker}: {option.get('description')}")
    return "\n".join(lines).strip()
