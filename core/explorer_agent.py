"""
Explore Sub-Agent：嵌套 ReAct agent，独立 messages + 独立 LLM 实例 + 只读工具集。

设计要点：
- 主 agent 视角只看到一个 `code_explore` 工具，sub-agent 的过程对用户透明
- sub-agent 强制 thinking=False（避开"思考+多轮 tool call"的兼容性问题）
- token budget 限流：超出即强制 summarize，避免无止境 ReAct
- 只能调用代码自省三件套：code_glob / code_grep / code_read
"""
from __future__ import annotations

import json
from typing import Any, Optional

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import StructuredTool
from langgraph.prebuilt import create_react_agent
from loguru import logger
from pydantic import Field, create_model

from core.llm_client import create_llm
from core.registry import registry
from settings import code_inspection_config
from tools.code_inspector import code_glob, code_grep, code_read


EXPLORE_SYSTEM_PROMPT = """你是代码探索 sub-agent，目标是在项目源码中**定位并理解**和用户问题相关的代码。

可用工具：
- code_glob(pattern, max_results, repo_connector_id/connector_id): glob 匹配文件路径
- code_grep(pattern, path, ignore_case, fuzzy, max_results, repo_connector_id/connector_id): 全文搜索；可用 '(a|b|c)' 一次搜多个候选
- code_read(path, start_line, end_line, repo_connector_id/connector_id): 读文件片段（带行号）

工作流程（严格遵守）：
1. 先基于用户任务列出 3-5 个候选关键词（含中文业务词→英文、CamelCase/snake_case 变体）
2. 调 code_grep 一次性搜索候选（fuzzy=True 时扩大召回）；0 结果就放宽/换词
3. 挑最相关的 2-4 个文件，用 code_read 读命中行前后 50-150 行
4. 必要时再做一轮 grep/read 串联调用链
5. 在 budget 内尽早给出结构化总结

**输出要求**：探索结束时必须输出严格的 JSON（放在最后一条消息里），格式：
```json
{
  "summary": "一段话概述",
  "relevant_files": [
    {"path": "services/order/create.py", "why": "订单创建主入口", "key_lines": "42-118"}
  ],
  "key_symbols": [
    {"file": "services/order/create.py", "symbol": "OrderService.create", "lines": "42-88"}
  ],
  "searched_keywords": ["createOrder", "place_order", "下单"],
  "dead_ends": ["尝试过的无效关键词或文件"]
}
```

注意：不要编造路径/行号；所有结论必须来自实际工具返回。"""


def _truncate(value: Any, limit: int = 8000) -> str:
    s = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    if len(s) <= limit:
        return s
    return s[:limit] + f"\n...(已截断，原始长度 {len(s)})"


def _approx_tokens(messages: list[Any]) -> int:
    """粗估 token 数：按字符数 / 2.5 估算（中英混合场景相对保守）。"""
    total = 0
    for m in messages:
        content = getattr(m, "content", "") or ""
        if isinstance(content, list):
            content = json.dumps(content, ensure_ascii=False)
        total += len(str(content))
    return int(total / 2.5)


def _build_explorer_tools(project_id: str, default_connector_id: str = "") -> list[StructuredTool]:
    CodeGlobArgs = create_model(
        "CodeGlobArgs",
        pattern=(str, Field(description="glob 模式")),
        max_results=(int, Field(default=200)),
        repo_connector_id=(
            Optional[str],
            Field(default=None, description="可选 Repository Connector ID；不传则使用 code_explore 指定的仓库或项目默认仓库"),
        ),
        connector_id=(
            Optional[str],
            Field(default=None, description="repo_connector_id 的别名"),
        ),
    )
    CodeGrepArgs = create_model(
        "CodeGrepArgs",
        pattern=(str, Field(description="正则或关键词")),
        path=(str, Field(default="")),
        ignore_case=(bool, Field(default=True)),
        fuzzy=(bool, Field(default=False)),
        max_results=(int, Field(default=50)),
        repo_connector_id=(
            Optional[str],
            Field(default=None, description="可选 Repository Connector ID；不传则使用 code_explore 指定的仓库或项目默认仓库"),
        ),
        connector_id=(
            Optional[str],
            Field(default=None, description="repo_connector_id 的别名"),
        ),
    )
    CodeReadArgs = create_model(
        "CodeReadArgs",
        path=(str, Field(description="workspace 内相对路径")),
        start_line=(int, Field(default=1)),
        end_line=(Optional[int], Field(default=None)),
        repo_connector_id=(
            Optional[str],
            Field(default=None, description="可选 Repository Connector ID；不传则使用 code_explore 指定的仓库或项目默认仓库"),
        ),
        connector_id=(
            Optional[str],
            Field(default=None, description="repo_connector_id 的别名"),
        ),
    )

    def _json(data: Any) -> str:
        return _truncate(data)

    def _connector_args(
        connector_id: Optional[str] = None,
        repo_connector_id: Optional[str] = None,
    ) -> dict[str, Optional[str]]:
        connector_id = connector_id or None
        repo_connector_id = repo_connector_id or None
        # sub-agent（LLM）有时会在 tool call 里凭需求文本臆造 connector_id（如把
        # dream_leaker_front 笔误成 dream-leaker-front）。若它传的 id 在项目下不存在，
        # 而我们有一个有效的 default_connector_id（plan 阶段固定的任务目标仓库），
        # 则回退到默认仓库，避免一个笔误就让整个探索阶段 RuntimeError 失败。
        if default_connector_id:
            for key, value in (("connector_id", connector_id), ("repo_connector_id", repo_connector_id)):
                if value and value != default_connector_id and not registry.get_repository_connector(project_id, value):
                    logger.warning(
                        "[explorer] sub-agent 传入未知 {}={!r}，回退到默认仓库 {!r}",
                        key, value, default_connector_id,
                    )
                    if key == "connector_id":
                        connector_id = None
                    else:
                        repo_connector_id = None
        if not connector_id and not repo_connector_id:
            connector_id = default_connector_id or None
        return {"connector_id": connector_id, "repo_connector_id": repo_connector_id}

    return [
        StructuredTool.from_function(
            func=lambda pattern, max_results=200, repo_connector_id=None, connector_id=None: _json(
                code_glob(
                    project_id, pattern, max_results=max_results,
                    **_connector_args(connector_id, repo_connector_id),
                )
            ),
            name="code_glob",
            description="按 glob 找文件路径（遵循 .gitignore）。多仓库项目先在主 agent 用 list_repository_connectors 确认 repo_connector_id。",
            args_schema=CodeGlobArgs,
        ),
        StructuredTool.from_function(
            func=lambda pattern, path="", ignore_case=True, fuzzy=False, max_results=50, repo_connector_id=None, connector_id=None: _json(
                code_grep(
                    project_id, pattern, path=path, ignore_case=ignore_case,
                    fuzzy=fuzzy, max_results=max_results,
                    **_connector_args(connector_id, repo_connector_id),
                )
            ),
            name="code_grep",
            description="全文搜索；fuzzy=True 自动拆 CamelCase/snake_case 扩大召回。多仓库项目先确认 repo_connector_id。",
            args_schema=CodeGrepArgs,
        ),
        StructuredTool.from_function(
            func=lambda path, start_line=1, end_line=None, repo_connector_id=None, connector_id=None: _json(
                code_read(
                    project_id, path, start_line=start_line, end_line=end_line,
                    **_connector_args(connector_id, repo_connector_id),
                )
            ),
            name="code_read",
            description="读文件片段（带行号），单次最多 500 行。多仓库项目先确认 repo_connector_id。",
            args_schema=CodeReadArgs,
        ),
    ]


def _parse_structured_summary(final_content: str) -> dict:
    """从最终消息中抽出 JSON；提取失败时返回原始文本。"""
    text = final_content.strip()
    # 尝试提取 ```json ... ``` 块
    if "```json" in text:
        chunk = text.split("```json", 1)[1].split("```", 1)[0]
    elif "```" in text:
        chunk = text.split("```", 1)[1].split("```", 1)[0]
    else:
        # 尝试找第一个 { 和最后一个 }
        start = text.find("{")
        end = text.rfind("}")
        chunk = text[start:end + 1] if start != -1 and end != -1 else ""

    if chunk:
        try:
            return json.loads(chunk)
        except json.JSONDecodeError:
            pass
    return {"summary": text, "relevant_files": [], "key_symbols": [], "searched_keywords": [], "dead_ends": []}


async def run_explorer(
    project_id: str,
    task: str,
    connector_id: Optional[str] = None,
    repo_connector_id: Optional[str] = None,
) -> dict:
    """执行一次代码探索。

    Args:
        project_id: 项目 ID
        task: 用户目标（通常是主 agent 转述/细化的子任务）

    Returns:
        ExploreResult 结构体的 dict 形式
    """
    connector_id = (connector_id or "").strip()
    repo_connector_id = (repo_connector_id or "").strip()
    if connector_id and repo_connector_id and connector_id != repo_connector_id:
        return {
            "error": (
                f"connector_id({connector_id!r}) 与 "
                f"repo_connector_id({repo_connector_id!r}) 不一致"
            )
        }
    effective_connector_id = connector_id or repo_connector_id
    project = registry.get_project(project_id)
    if not project:
        return {"error": f"项目 {project_id} 未注册"}
    if effective_connector_id and not registry.get_repository_connector(project_id, effective_connector_id):
        return {"error": f"项目 {project_id} 下未找到仓库 {effective_connector_id}"}
    if not effective_connector_id and not project.git_url:
        return {"error": f"项目 {project_id} 未启用代码自省（缺少 git_url）"}
    if not code_inspection_config.enabled:
        return {"error": "code_inspection.enabled=false"}

    cfg = code_inspection_config.explorer
    llm = create_llm(thinking=False, feature="explorer")
    tools = _build_explorer_tools(project_id, default_connector_id=effective_connector_id)

    agent = create_react_agent(model=llm, tools=tools)
    messages = [
        SystemMessage(content=EXPLORE_SYSTEM_PROMPT),
        HumanMessage(content=task),
    ]

    try:
        result = await agent.ainvoke(
            {"messages": messages},
            config={"recursion_limit": cfg.max_steps},
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("[explorer] ReAct 执行失败: {}", e)
        return {"error": f"sub-agent 执行失败: {e}"}

    out_messages = result.get("messages", [])
    token_estimate = _approx_tokens(out_messages)
    if token_estimate > cfg.token_budget:
        logger.warning(
            "[explorer] token 估算 {} 超过预算 {}，走 force summarize",
            token_estimate, cfg.token_budget,
        )

    final = next(
        (m for m in reversed(out_messages) if isinstance(m, AIMessage) and m.content),
        None,
    )
    if final is None:
        return {"error": "sub-agent 未产生有效回复"}

    parsed = _parse_structured_summary(final.content if isinstance(final.content, str) else str(final.content))
    parsed.setdefault("summary", "")
    parsed.setdefault("relevant_files", [])
    parsed.setdefault("key_symbols", [])
    parsed.setdefault("searched_keywords", [])
    parsed.setdefault("dead_ends", [])
    parsed["_meta"] = {
        "project_id": project_id,
        "repo_connector_id": effective_connector_id,
        "steps": len(out_messages),
        "token_estimate": token_estimate,
    }
    return parsed
