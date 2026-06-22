"""Watchdog 自治分析 Agent。

接收探针结果，在 Skill 指导下通过内置工具自主分析，
输出结构化结论（severity / conclusion / evidence / action_type）。

特点：
- 无 session / memory，单次执行后丢弃
- 只读：只调用查询类工具，不进行写操作
- 受 max_iterations 保护，超时自动中止
"""
from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import StructuredTool
from loguru import logger
from pydantic import BaseModel, Field

from core.llm_client import create_llm
from core.registry import SkillItem, WatchdogItem, registry
from core.tool_execution_manager import ToolExecutionManager, ToolJob, ToolJobResult
from settings import watchdog_config


# ────────────────────────────────────────────────────────────
# 输出结构
# ────────────────────────────────────────────────────────────

class WatchdogConclusion(BaseModel):
    """Watchdog Agent 分析结论。"""
    is_anomaly: bool = False
    severity: str = "info"  # info / warning / critical
    conclusion: str = ""
    evidence: list[str] = Field(default_factory=list)
    action_type: str = "none"  # none / notify / coding_plan


# ────────────────────────────────────────────────────────────
# System prompt 模板
# ────────────────────────────────────────────────────────────

_SYSTEM_PROMPT_TEMPLATE = """\
你是 Viktor Watchdog 分析引擎。你的任务是基于探针结果判断系统是否异常，并给出诊断结论。

## 规则
1. 只使用提供的工具做只读查询（数据库、日志、K8s、代码等），收集佐证。
2. 结合探针结果 + 工具查询结果，判断是否异常。
3. 最终以 JSON 格式输出结论（见下方模板），不要输出其他内容。
4. severity 分级标准：
   - info: 正常或轻微波动，无需关注
   - warning: 需关注但暂不影响核心功能
   - critical: 影响核心功能或用户体验，需立即处理
5. action_type:
   - none: 不需要进一步操作
   - notify: 需要通知值班人员
   - coding_plan: 需要生成修复代码方案

## Skill 指引
{skills_block}

## 探针信息
- Watchdog: {watchdog_name}
- 描述: {watchdog_description}
- 探针结果:
```json
{probe_result}
```

## 输出格式（严格 JSON，无额外文字）
```json
{{
  "is_anomaly": true/false,
  "severity": "info|warning|critical",
  "conclusion": "一段话总结分析结论",
  "evidence": ["佐证1", "佐证2"],
  "action_type": "none|notify|coding_plan"
}}
```
"""


# ────────────────────────────────────────────────────────────
# 工具构建（复用 agent_loop 中的工厂函数）
# ────────────────────────────────────────────────────────────

def _build_watchdog_tools(project_id: str) -> list[StructuredTool]:
    """复用主 Agent 的工具构建函数，为 Watchdog Agent 组装只读工具集。"""
    # 延迟导入避免循环依赖
    from core.agent_loop import _build_project_tools
    return _build_project_tools(project_id)


# ────────────────────────────────────────────────────────────
# Skills → prompt block
# ────────────────────────────────────────────────────────────

def _format_skills_for_watchdog(skills: list[SkillItem]) -> str:
    """将指定 Skill 列表格式化为 system prompt 片段。"""
    if not skills:
        return "(无指定 Skill)"
    lines = []
    for skill in skills:
        lines.append(f"### {skill.name} ({skill.id})")
        if skill.description:
            lines.append(f"- 描述：{skill.description}")
        if skill.instructions:
            lines.append("- 步骤：")
            for idx, step in enumerate(skill.instructions[:10], 1):
                lines.append(f"  {idx}. {step}")
        if skill.output_contract:
            lines.append(f"- 输出要求：{json.dumps(skill.output_contract, ensure_ascii=False)}")
        lines.append("")
    return "\n".join(lines)


# ────────────────────────────────────────────────────────────
# 核心执行
# ────────────────────────────────────────────────────────────

def _tool_jobs_from_ai_message(message: AIMessage, start_seq: int = 0) -> list[ToolJob]:
    """从 AI 消息中提取 tool calls 转为 ToolJob。"""
    jobs: list[ToolJob] = []
    for offset, call in enumerate(message.tool_calls or [], start=1):
        if not isinstance(call, dict):
            continue
        name = str(call.get("name") or "")
        call_id = str(call.get("id") or f"tool-call-{start_seq + offset}")
        raw_args = call.get("args") or {}
        args = raw_args if isinstance(raw_args, dict) else {"input": raw_args}
        jobs.append(ToolJob(seq=start_seq + offset, call_id=call_id, name=name, args=args))
    return jobs


async def run_watchdog_agent(
    watchdog: WatchdogItem,
    probe_result: dict[str, Any],
) -> WatchdogConclusion:
    """执行 Watchdog 自治分析。

    Args:
        watchdog: 注册的 Watchdog 定义。
        probe_result: 探针执行的原始结果。

    Returns:
        WatchdogConclusion 结构化结论。
    """
    max_iterations = watchdog_config.agent_max_iterations

    # 解析关联 Skills
    skills = registry.resolve_skills_for_watchdog(watchdog.project_id, watchdog.skill_ids)
    skills_block = _format_skills_for_watchdog(skills)

    system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(
        skills_block=skills_block,
        watchdog_name=watchdog.name,
        watchdog_description=watchdog.description,
        probe_result=json.dumps(probe_result, ensure_ascii=False, indent=2),
    )

    # 构建工具
    tools = _build_watchdog_tools(watchdog.project_id)
    if not tools:
        logger.warning("Watchdog {}: 项目 {} 无可用工具，跳过工具调用", watchdog.id, watchdog.project_id)

    llm = create_llm(feature="agent")
    llm_with_tools = llm.bind_tools(tools) if tools else llm

    tool_manager = ToolExecutionManager(
        tools,
        max_concurrency=3,
        timeout_sec=watchdog.max_execution_sec,
    ) if tools else None

    messages: list[Any] = [
        SystemMessage(content=system_prompt),
        HumanMessage(content="请开始分析上述探针结果，判断是否存在异常。"),
    ]

    tool_seq = 0
    final_content = ""

    for _step in range(max_iterations):
        response = await llm_with_tools.ainvoke(messages)
        if not isinstance(response, AIMessage):
            response = AIMessage(content=str(response))
        messages.append(response)

        jobs = _tool_jobs_from_ai_message(response, tool_seq)
        tool_seq += len(jobs)
        if not jobs:
            final_content = response.content if isinstance(response.content, str) else str(response.content)
            break

        if tool_manager is None:
            final_content = response.content if isinstance(response.content, str) else str(response.content)
            break

        results: dict[str, ToolJobResult] = {}
        async for result in tool_manager.iter_results(jobs):
            results[result.job.call_id] = result
        messages.extend(tool_manager.to_tool_messages(jobs, results))
    else:
        logger.warning("Watchdog {} Agent 触达 max_iterations={}", watchdog.id, max_iterations)
        final_content = messages[-1].content if messages else ""

    # 解析 JSON 结论
    return _parse_conclusion(final_content, watchdog.id)


def _parse_conclusion(raw: str, watchdog_id: str) -> WatchdogConclusion:
    """从 Agent 原始输出中提取 JSON 结论。"""
    # 尝试从 markdown code block 中提取
    import re
    json_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", raw, re.DOTALL)
    json_str = json_match.group(1).strip() if json_match else raw.strip()

    try:
        data = json.loads(json_str)
        return WatchdogConclusion(**data)
    except (json.JSONDecodeError, TypeError, ValueError) as e:
        logger.warning("Watchdog {} Agent 输出解析失败: {}, raw={}", watchdog_id, e, raw[:500])
        # 兜底：如果有内容就当 warning
        if raw.strip():
            return WatchdogConclusion(
                is_anomaly=True,
                severity="warning",
                conclusion=f"Agent 输出解析失败，原始内容: {raw[:800]}",
                evidence=[],
                action_type="notify",
            )
        return WatchdogConclusion()
