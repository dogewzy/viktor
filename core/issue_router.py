"""需求/Bug issue -> 目标代码仓库（repo connector）自动路由。

需求接入页收敛为项目级单一 issue 入口，产品/测试同学不再手动选 repo。
Viktor 收到 issue 后，用强模型（feature=issue_routing，默认 Opus）判断该由
哪一个或哪几个仓库承接（前后端联动时可路由到多个仓库），全程无需人工确认。
"""
from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from loguru import logger

from core.llm_client import create_llm
from core.registry import registry


MAX_ROUTED_REPOS = 3

_SYSTEM_PROMPT = """你是研发交付系统的“需求路由器”。给定一个产品需求或测试 Bug，以及项目下可选的代码仓库列表，判断应该由哪一个或哪几个仓库来实现这次改动。

规则：
- 只能从给定的候选仓库里选，使用它们的 repo_connector_id。
- 判据是"哪些仓库必须在它自己的代码里做改动"，而不是"哪些仓库话题相关"。仅仅提到某仓职责涉及的概念（如用户表、登录链路）不等于要改它。
- 共享资源要点：若多个仓共享同一数据库/表，对该表的写入只归负责该入口的【一个】仓，不要因为某张表逻辑上属于另一个仓就把它也算上。
- 多数需求只落在一个仓库；只有当改动确实需要多个仓库各自改代码协同（例如前端页面 + 后端接口）时，才返回多个仓库。宁可少不可多。
- 按“最该改的仓库”在前排序，最多返回 3 个。
- 不要编造仓库，不要返回候选列表之外的 id。

只输出 JSON，不要任何额外文字，格式：
{"repos": [{"repo_connector_id": "<id>", "reason": "<为什么是这个仓库，一句话>"}], "analysis": "<整体判断，一句话>"}
"""


def _extract_json_object(text: str) -> dict[str, Any]:
    if not text:
        return {}
    s = text.strip()
    if s.startswith("```"):
        body = s[3:]
        if body[:4].lower() == "json":
            body = body[4:]
        end = body.rfind("```")
        s = (body[:end] if end >= 0 else body).strip()
    start = s.find("{")
    end = s.rfind("}")
    if start < 0 or end < 0 or end <= start:
        return {}
    try:
        value = json.loads(s[start:end + 1])
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def route_issue(
    project_id: str,
    *,
    kind: str,
    title: str,
    description: str,
    default_repo_connector_id: str = "",
) -> list[dict[str, str]]:
    """返回有序的目标仓库列表 [{repo_connector_id, reason}]，至少 1 个（除非项目无仓库）。

    单仓库项目直接返回该仓库；模型失败或给不出有效仓库时回退默认仓库。
    """
    repos = registry.get_repository_connectors(project_id) or []
    real_ids = [r.id for r in repos if r.id]
    if not real_ids:
        return []
    valid_ids = set(real_ids)

    if len(real_ids) == 1:
        return [{"repo_connector_id": real_ids[0], "reason": "项目仅一个仓库"}]

    fallback = default_repo_connector_id if default_repo_connector_id in valid_ids else real_ids[0]

    try:
        catalog = "\n".join(
            (
                f"- {r.id}: {r.display_name or r.id}（git: {r.git_url}）"
                + (f"\n    职责: {desc}" if (desc := (getattr(r, 'description', '') or '').strip()) else "")
            )
            for r in repos
            if r.id
        )
        human = (
            f"## 类型\n{kind}\n\n"
            f"## 标题\n{title}\n\n"
            f"## 描述\n{(description or '').strip()[:6000]}\n\n"
            f"## 候选仓库\n{catalog}"
        )
        llm = create_llm(thinking=False, feature="issue_routing")
        response = llm.invoke([
            SystemMessage(content=_SYSTEM_PROMPT),
            HumanMessage(content=human),
        ])
        content = response.content if isinstance(response.content, str) else str(response.content or "")
        data = _extract_json_object(content)
        routed: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in data.get("repos") or []:
            if not isinstance(item, dict):
                continue
            rid = str(item.get("repo_connector_id") or "").strip()
            if rid in valid_ids and rid not in seen:
                routed.append({"repo_connector_id": rid, "reason": str(item.get("reason") or "").strip()})
                seen.add(rid)
            if len(routed) >= MAX_ROUTED_REPOS:
                break
        if routed:
            logger.info(
                "[issue-routing] project={} kind={} -> {}",
                project_id, kind, [r["repo_connector_id"] for r in routed],
            )
            return routed
        logger.warning("[issue-routing] 模型未给出有效仓库，回退默认 repo={}", fallback)
    except Exception as e:  # noqa: BLE001
        logger.warning("[issue-routing] 路由失败，回退默认 repo={}: {}", fallback, e)
    return [{"repo_connector_id": fallback, "reason": "路由失败回退默认仓库"}]
