"""多仓 issue 的 blueprint（蓝图）：fan-out 前的总体设计。

解决两类问题：
1. 过度路由：路由器按"话题相关"会把无需改代码的仓也拉进来（如共享库写入其实只归一个
   仓）。blueprint 用强模型重判"哪些仓【必须改代码】的最小集"，再交人工裁剪。
2. 契约靠猜：纯并行 fan-out 时前后端各拍各的接口。blueprint 在 fan-out 前先定死跨仓
   接口契约（method/path/request/response），注入各子任务，避免运行期字段对不上。

blueprint 是 issue 维度、规定性、临时的；与持久化、描述性的 api-contracts（viktor_contexts）
互补：MR 合并后由现有"分析代码生成 api-contracts"路径把新接口自然收敛进持久契约。
"""
from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from loguru import logger

from core.issue_router import _extract_json_object
from core.llm_client import create_llm
from core.registry import registry

MAX_BLUEPRINT_REPOS = 3

_SYSTEM_PROMPT = """你是研发交付系统的"多仓蓝图架构师"。给定一个产品需求/Bug、项目下的候选仓库（含职责描述），以及一份初步路由结果，你要产出一份"改动蓝图"，供人工确认后据此切分多仓 Coding Task。

你要做两件事：

# 1. 收敛仓库清单（只保留【必须改代码】的仓）
- 关键判断：一个仓是否真的需要在它自己的代码里做改动。仅仅"话题相关"不算。
- 特别注意共享资源：若多个仓共享同一数据库/表，则对该表的写入只归【一个】负责该写入入口的仓，不要因为"某张表逻辑上属于另一个仓"就把那个仓也拉进来。
- 若某能力可以完全在已选仓内实现，就不要再引入额外仓。
- 宁可少不可多：每多一个仓就多一个会白跑的 Coding Task。最多 3 个。

# 2. 定义跨仓接口契约（仅当存在跨仓调用时）
- 当一个仓要调用/依赖另一个仓新增或变更的接口时（典型：前端调后端新接口），必须把该接口的契约定死：
  - method + path
  - owner_repo（实现方）、consumer_repos（调用方，可多个）
  - request：请求体/参数的字段名、类型、含义
  - response：响应体的字段名、类型、含义（成功计数等字段名必须明确，避免前后端各起一个名）
- 没有跨仓调用就返回空数组。不要为单仓内部接口写契约。

只输出 JSON，不要任何额外文字，格式：
{
  "repos": [{"repo_connector_id": "<id>", "role": "<backend|frontend|engine|...>", "reason": "<为什么必须改这个仓，一句话>"}],
  "contracts": [
    {
      "name": "<接口用途>",
      "method": "POST",
      "path": "/api/...",
      "owner_repo": "<id>",
      "consumer_repos": ["<id>"],
      "request": {"<字段>": "<类型/含义>"},
      "response": {"<字段>": "<类型/含义>"},
      "notes": "<约束/备注，可空>"
    }
  ],
  "analysis": "<整体判断与取舍，一句话>"
}
"""


def build_blueprint(
    project_id: str,
    *,
    kind: str,
    title: str,
    description: str,
    routed: list[dict[str, Any]],
) -> dict[str, Any]:
    """用强模型产出 blueprint：{repos, contracts, analysis}。失败时回退为初步路由（无契约）。"""
    repos = registry.get_repository_connectors(project_id) or []
    valid_ids = {r.id for r in repos if r.id}
    fallback_repos = [
        {"repo_connector_id": rid, "role": "", "reason": str(r.get("reason") or "")}
        for r in (routed or [])
        if (rid := str(r.get("repo_connector_id") or "").strip()) in valid_ids
    ]
    fallback = {"repos": fallback_repos, "contracts": [], "analysis": "blueprint 生成失败，回退初步路由"}

    if not valid_ids:
        return fallback

    try:
        catalog = "\n".join(
            (
                f"- {r.id}: {r.display_name or r.id}（git: {r.git_url}）"
                + (f"\n    职责: {desc}" if (desc := (getattr(r, 'description', '') or '').strip()) else "")
            )
            for r in repos
            if r.id
        )
        routed_line = "、".join(
            str(r.get("repo_connector_id") or "") for r in (routed or []) if r.get("repo_connector_id")
        ) or "（无）"
        human = (
            f"## 类型\n{kind}\n\n"
            f"## 标题\n{title}\n\n"
            f"## 描述\n{(description or '').strip()[:6000]}\n\n"
            f"## 候选仓库\n{catalog}\n\n"
            f"## 初步路由\n{routed_line}"
        )
        llm = create_llm(thinking=False, feature="issue_routing")
        response = llm.invoke([SystemMessage(content=_SYSTEM_PROMPT), HumanMessage(content=human)])
        content = response.content if isinstance(response.content, str) else str(response.content or "")
        data = _extract_json_object(content)

        out_repos: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in data.get("repos") or []:
            if not isinstance(item, dict):
                continue
            rid = str(item.get("repo_connector_id") or "").strip()
            if rid in valid_ids and rid not in seen:
                out_repos.append({
                    "repo_connector_id": rid,
                    "role": str(item.get("role") or "").strip(),
                    "reason": str(item.get("reason") or "").strip(),
                })
                seen.add(rid)
            if len(out_repos) >= MAX_BLUEPRINT_REPOS:
                break
        if not out_repos:
            return fallback

        out_contracts: list[dict[str, Any]] = []
        for c in data.get("contracts") or []:
            if not isinstance(c, dict):
                continue
            out_contracts.append({
                "name": str(c.get("name") or "").strip(),
                "method": str(c.get("method") or "").strip().upper(),
                "path": str(c.get("path") or "").strip(),
                "owner_repo": str(c.get("owner_repo") or "").strip(),
                "consumer_repos": [str(x).strip() for x in (c.get("consumer_repos") or []) if str(x).strip()],
                "request": c.get("request") if isinstance(c.get("request"), dict) else {},
                "response": c.get("response") if isinstance(c.get("response"), dict) else {},
                "notes": str(c.get("notes") or "").strip(),
            })

        logger.info(
            "[issue-blueprint] project={} kind={} repos={} contracts={}",
            project_id, kind, [r["repo_connector_id"] for r in out_repos], len(out_contracts),
        )
        return {"repos": out_repos, "contracts": out_contracts, "analysis": str(data.get("analysis") or "").strip()}
    except Exception as e:  # noqa: BLE001
        logger.warning("[issue-blueprint] 生成失败，回退初步路由: {}", e)
        return fallback


def render_contracts_for_repo(contracts: list[dict[str, Any]], this_repo: str) -> str:
    """把与 this_repo 相关（作为 owner 或 consumer）的契约渲染成注入 requirement 的 Markdown。"""
    import json as _json

    related = [
        c for c in (contracts or [])
        if isinstance(c, dict) and (c.get("owner_repo") == this_repo or this_repo in (c.get("consumer_repos") or []))
    ]
    if not related:
        return ""
    lines = ["## 跨仓接口契约（已确认，必须严格遵守，不得自行更改字段名/结构）"]
    for c in related:
        role = "本仓实现（owner）" if c.get("owner_repo") == this_repo else f"本仓调用（owner: {c.get('owner_repo')}）"
        lines.append(f"\n### {c.get('name') or c.get('path')} — {role}")
        lines.append(f"- `{c.get('method')} {c.get('path')}`")
        if c.get("request"):
            lines.append(f"- 请求: {_json.dumps(c.get('request'), ensure_ascii=False)}")
        if c.get("response"):
            lines.append(f"- 响应: {_json.dumps(c.get('response'), ensure_ascii=False)}")
        if c.get("notes"):
            lines.append(f"- 备注: {c.get('notes')}")
    return "\n".join(lines) + "\n"
