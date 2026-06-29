"""内置 Coding Agent Loop：基于 LangGraph ReAct 调用 coding tools。"""
from __future__ import annotations

import json
import re
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.prebuilt import create_react_agent

from core.clarification_gate import run_clarification_gate
from core.coding_runtime import CodingRuntime
from core.llm_client import create_llm
from settings import coding_agent_config


CODING_SYSTEM_PROMPT = """你是 Viktor Coding Agent，运行在一个隔离的可写代码 workspace 中。

你的目标是在后台无人值守完成用户开发需求，并留下清晰、可审计的 diff。

工作规则：
1. 先阅读项目上下文和需求，列出简短计划。
2. 使用 list_files / grep / read_file 定位相关代码；不要凭空猜文件。
3. 所有工具 path 参数必须传 workspace 内相对路径，例如 cronjobs/job.py；如果上下文出现 /Users/.../repo/cronjobs/job.py 这类绝对路径，调用工具前必须转换为 cronjobs/job.py。
4. 修改文件时优先使用 apply_patch，old 必须精确来自 read_file 结果。
5. 修改后用 git_diff 检查变更；检查单个 Python/Java/JavaScript/TypeScript/TSX/JSX 文件语法时必须用 check_syntax（auto 按后缀自动识别），禁止用 run_command 拼 py_compile、javac、node --check、esbuild、cd/&&/echo 等 shell 命令。
6. 必要时运行 policy 允许的测试/lint/build 命令。run_command 的工作目录已经是 workspace 根目录，禁止再 cd。Python 项目的依赖由 Viktor 预热 venv 提供并已注入 PATH，直接用裸 `pytest` / `python -m pytest` / `python xxx.py` 即可，禁止 `pip install`（依赖已装好，装包也会被 policy 拒）、禁止写 `.venv/bin/` 前缀。若测试因缺依赖失败，说明该仓库未预热 venv，请在报告里说明而不要尝试自行装包。
7. 不要修改与需求无关的文件，不要碰 secrets、生产配置、CI/CD 或数据库迁移，除非需求和 policy 明确允许。
8. 如果工具因 policy 拒绝，不要绕过；请调整方案或在最终报告里说明。
9. 最终回答必须包含：实现摘要、修改文件、已运行检查、风险/未完成项。

你无法直接 push 或创建 MR；这些由 Viktor 编排层在校验通过后处理。"""


CODING_PLAN_SYSTEM_PROMPT = """你是 Viktor Coding Agent 的 planning-only 模式。

你的任务是把 coding task 的需求、项目上下文、只读代码探索结果，压缩成后续执行阶段可直接依赖的正式 Plan。

要求：
1. 不要声称已经修改代码。
2. 不要输出“候选文件/搜索关键词/下一步探索”这种准备提示词；Plan 必须体现已经完成过代码核对。
3. 优先依据「只读代码探索结果」中的真实文件、行号、符号和调用链；静态业务上下文只能作为辅助，若两者冲突，以代码探索为准。
4. 如果探索结果不足，明确写出缺口和阻塞，不要编造文件、行号或业务链路。
5. 修改方案必须具体到文件/函数/常量/配置/数据流，并说明为什么这么改。
6. 回复使用中文 Markdown。

输出格式：
## Summary

## Terminology Alignment
- 写清本次用户表达与代码内部术语、字段、状态或历史命名的映射；如果没有特别映射，写“无额外术语映射”。

## Code Findings
- 写清已经核对的关键文件、函数/类、行号范围、当前逻辑。

## Key Changes
- 写清准备修改的文件、符号、逻辑变化。

## Impact
- 写清业务行为、数据一致性、性能/成本、回滚或恢复路径的影响。

## Test Plan
- 写清单元测试/脚本/手工验证/回归验证命令或场景。

## Assumptions
- 只写仍需人工确认或探索结果无法证明的前提。

---PLAN_QUESTIONS---
```json
{
  "open_questions": [
    {
      "id": "snake_case_id",
      "question": "需要用户裁决的实现决策问题",
      "recommended": "option_value",
      "options": [
        {
          "label": "展示名称",
          "value": "option_value",
          "description": "选择后 Agent 会怎样实现"
        }
      ]
    }
  ]
}
```

open_questions 规则：
- 只输出存在多种合理实现路径、且选择会显著影响执行方向的决策点。
- 每个问题必须有 2-4 个互斥选项，并给出 recommended。
- 每个选项的 description 必须说明选择后的实现影响。
- 最多 3 个问题。
- 如果 Plan 已经足够明确、没有需要用户裁决的分歧，输出 {"open_questions": []}。
"""


CODING_CLARIFICATION_SYSTEM_PROMPT = """你是 Viktor Coding Agent 的 clarification gate。

你的任务不是生成 Plan，而是在正式 Plan 之前判断是否必须向用户提问。

判断重点：
1. 用户表达是否与 glossary 中的官方术语一致。
2. 代码里是否存在只有研发知道的内部术语、历史命名、简称或隐喻。
3. 同一个用户表达是否可能对应多个实现层级、状态字段、定时任务或外部系统动作。
4. 如果不提问，是否可能导致 Agent 改错业务层级、状态机、数据字段或高风险链路。

规则：
1. 能基于 glossary、项目上下文和只读代码探索高置信判断时，不要提问，只输出 term_mappings。
2. 只有存在会影响实现路径的阻塞歧义时才提问。
3. 最多提出 3 个问题。
4. 每个问题必须有 2-4 个互斥选项，并给出 recommended。
5. 每个选项必须说明选择后的实现影响。
6. evidence 必须来自项目上下文或只读代码探索，不要编造文件和行号。
7. 只输出 JSON，不要输出 Markdown、代码块或额外解释。

JSON Schema:
{
  "needs_clarification": true,
  "term_mappings": [
    {
      "user_term": "用户原始表达",
      "code_terms": ["代码术语或符号"],
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
          "description": "选择后 Agent 会怎样实现"
        }
      ]
    }
  ]
}
"""


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


def _normalize_clarification(value: dict[str, Any]) -> dict[str, Any]:
    term_mappings = value.get("term_mappings")
    if not isinstance(term_mappings, list):
        term_mappings = []

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
                normalized_questions.append({
                    "id": str(item.get("id") or f"question_{index}").strip()[:80],
                    "type": str(item.get("type") or "clarification").strip()[:80],
                    "question": question[:500],
                    "recommended": str(item.get("recommended") or "").strip()[:120],
                    "blocking": bool(item.get("blocking", True)),
                    "evidence": item.get("evidence") if isinstance(item.get("evidence"), list) else [],
                    "options": normalized_options,
                })

    needs_clarification = bool(value.get("needs_clarification")) and any(q.get("blocking") for q in normalized_questions)
    return {
        "needs_clarification": needs_clarification,
        "term_mappings": term_mappings[:20],
        "questions": normalized_questions if needs_clarification else [],
    }


async def run_coding_agent(
    *,
    requirement: str,
    project_context: str,
    runtime: CodingRuntime,
) -> str:
    llm = create_llm(thinking=False, feature="coding")
    agent = create_react_agent(model=llm, tools=runtime.tools())
    result = await agent.ainvoke(
        {
            "messages": [
                SystemMessage(content=CODING_SYSTEM_PROMPT),
                HumanMessage(content=f"## 项目上下文\n{project_context}\n\n## 开发需求\n{requirement}"),
            ]
        },
        config={"recursion_limit": coding_agent_config.max_steps},
    )
    messages = result.get("messages", [])
    for msg in reversed(messages):
        content = getattr(msg, "content", None)
        if content:
            return content if isinstance(content, str) else str(content)
    return "Coding Agent 未产生最终总结。"


async def run_coding_plan(
    *,
    requirement: str,
    project_context: str,
    code_exploration: str = "",
) -> tuple[str, list[dict[str, Any]]]:
    """Generate a coding plan and return (plan_markdown, open_questions)."""
    llm = create_llm(thinking=False, feature="coding_plan")
    exploration_block = code_exploration.strip() or "未获得有效代码探索结果；请在 Assumptions 中明确该缺口。"
    result = await llm.ainvoke(
        [
            SystemMessage(content=CODING_PLAN_SYSTEM_PROMPT),
            HumanMessage(
                content=(
                    f"## 项目上下文\n{project_context}\n\n"
                    f"## 只读代码探索结果\n{exploration_block}\n\n"
                    f"## 开发需求\n{requirement}"
                )
            ),
        ]
    )
    content = getattr(result, "content", None)
    raw = content if isinstance(content, str) and content.strip() else str(content or "").strip()
    plan_markdown, open_questions = _split_plan_and_questions(raw)
    return plan_markdown, open_questions


def _split_plan_and_questions(raw: str) -> tuple[str, list[dict[str, Any]]]:
    """Split LLM output into plan markdown and structured open_questions."""
    separator = "---PLAN_QUESTIONS---"
    if separator in raw:
        parts = raw.split(separator, 1)
        plan_markdown = parts[0].strip()
        questions_block = parts[1].strip()
    else:
        plan_markdown = raw.strip()
        questions_block = ""

    open_questions: list[dict[str, Any]] = []
    if questions_block:
        parsed = _extract_json_object(questions_block)
        raw_questions = parsed.get("open_questions")
        if isinstance(raw_questions, list):
            open_questions = _normalize_plan_questions(raw_questions)

    return plan_markdown, open_questions


def _normalize_plan_questions(raw_questions: list) -> list[dict[str, Any]]:
    """Normalize open_questions into stable format (same as clarification options)."""
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(raw_questions[:3], start=1):
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
            normalized.append({
                "id": str(item.get("id") or f"plan_q_{index}").strip()[:80],
                "question": question[:500],
                "recommended": str(item.get("recommended") or "").strip()[:120],
                "options": normalized_options,
            })
    return normalized


async def run_coding_clarification(
    *,
    requirement: str,
    project_context: str,
    code_exploration: str = "",
) -> dict[str, Any]:
    return await run_clarification_gate(
        scenario="coding",
        user_message=requirement,
        project_context=project_context,
        code_exploration=code_exploration,
        feature="coding_clarification",
    )
