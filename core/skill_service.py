"""Skill draft extraction and lightweight matching helpers."""
from __future__ import annotations

import json
import re
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from loguru import logger

from core.llm_client import create_llm
from core.registry import (
    SkillContextRef,
    SkillItem,
    SkillTriggerExample,
    registry,
)

KNOWN_TOOLS: list[dict[str, str]] = [
    {"id": "list_runtime_contexts", "label": "查看运行时上下文", "category": "runtime"},
    {"id": "get_runtime_context", "label": "查看单个运行时上下文", "category": "runtime"},
    {"id": "list_database_connectors", "label": "列出数据库上下文", "category": "database"},
    {"id": "list_tables", "label": "列出表", "category": "database"},
    {"id": "describe_table", "label": "查看表结构", "category": "database"},
    {"id": "sample_rows", "label": "抽样数据", "category": "database"},
    {"id": "execute_sql", "label": "执行只读 SQL", "category": "database"},
    {"id": "list_log_connectors", "label": "列出日志上下文", "category": "log"},
    {"id": "query_logs", "label": "检索日志", "category": "log"},
    {"id": "list_external_connectors", "label": "列出外部上下文", "category": "external"},
    {"id": "redis_exists", "label": "检查 Redis key", "category": "redis"},
    {"id": "redis_get", "label": "读取 Redis key", "category": "redis"},
    {"id": "object_storage_head", "label": "检查对象是否存在", "category": "object_storage"},
    {"id": "object_storage_list", "label": "列出对象前缀", "category": "object_storage"},
    {"id": "queue_overview", "label": "查看队列概况", "category": "queue"},
    {"id": "vector_collection_info", "label": "查看向量库集合", "category": "vector_store"},
    {"id": "http_health_check", "label": "HTTP 健康检查", "category": "http_service"},
    {"id": "http_call", "label": "调用 HTTP 接口", "category": "http_service"},
    {"id": "dingtalk_doc_read", "label": "读取钉钉文档", "category": "dingtalk_doc"},
    {"id": "code_glob", "label": "查找代码文件", "category": "repository"},
    {"id": "code_grep", "label": "搜索代码", "category": "repository"},
    {"id": "code_read", "label": "读取代码片段", "category": "repository"},
    {"id": "write_repo_debug_file", "label": "写入仓库调试文件", "category": "repository"},
    {"id": "run_repo_command", "label": "运行仓库验证命令", "category": "repository"},
    {"id": "run_repo_debug_script", "label": "运行仓库验证脚本", "category": "repository"},
    {"id": "code_explore", "label": "代码探索子任务", "category": "repository"},
    {"id": "get_pod_status", "label": "查看 Pod 状态", "category": "kubernetes"},
    {"id": "get_pod_logs", "label": "查看 Pod 日志", "category": "kubernetes"},
]

_TOOL_IDS = {tool["id"] for tool in KNOWN_TOOLS}


def _slugify(text: str, fallback: str = "skill") -> str:
    raw = (text or "").strip().lower()
    pieces = re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", raw)
    if not pieces:
        return fallback
    slug = "-".join(pieces)[:80].strip("-")
    return slug or fallback


def _extract_json(text: str) -> dict[str, Any]:
    content = (text or "").strip()
    if "```json" in content:
        content = content.split("```json", 1)[1].split("```", 1)[0].strip()
    elif "```" in content:
        content = content.split("```", 1)[1].split("```", 1)[0].strip()
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        start = content.find("{")
        end = content.rfind("}")
        if start < 0 or end <= start:
            raise
        parsed = json.loads(content[start:end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("LLM 返回不是 JSON object")
    return parsed


def _available_context_refs(project_id: str) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    for item in registry.get_repository_connectors(project_id):
        refs.append({"type": "repository", "id": item.id, "name": item.display_name or item.id})
    for item in registry._database_connectors.get(project_id, {}).values():
        refs.append({"type": "database", "id": item.id, "name": item.database})
    for item in registry.get_log_connectors(project_id, only_enabled=False):
        refs.append({"type": "log", "id": item.id, "name": item.display_name or item.logstore})
    for item in registry.get_runtime_contexts(project_id, only_enabled=False):
        refs.append({"type": "runtime", "id": item.id, "name": item.workload_name or item.id})
    for item in registry.get_external_connectors(project_id, only_enabled=False):
        refs.append({"type": item.connector_type, "id": item.id, "name": item.display_name or item.id})
    for item in registry.get_contexts(project_id):
        refs.append({"type": "context", "id": item.id, "name": item.id})
    return refs


def _fallback_skill(project_id: str, raw_text: str) -> SkillItem:
    text = raw_text.strip()
    lower = text.lower()
    tools: list[str] = []
    context_types: set[str] = set()
    if any(key in lower for key in ["数据库", "表", "sql", "db", "mysql", "样本表", "任务表"]):
        tools.extend(["list_database_connectors", "list_tables", "describe_table", "execute_sql"])
        context_types.add("database")
    if any(key in lower for key in ["日志", "log", "sls", "trace", "task_id"]):
        tools.extend(["list_log_connectors", "query_logs"])
        context_types.add("log")
    if "redis" in lower or "队列" in lower or "锁" in lower:
        tools.extend(["list_external_connectors", "redis_exists", "redis_get"])
        context_types.add("redis")
    if any(key in lower for key in ["oss", "文件", "object", "bucket"]):
        tools.extend(["list_external_connectors", "object_storage_head", "object_storage_list"])
        context_types.add("object_storage")
    if any(key in lower for key in ["pod", "k8s", "kubernetes", "deployment", "集群", "服务"]):
        tools.extend(["list_runtime_contexts", "get_runtime_context", "get_pod_status"])
        context_types.add("runtime")

    refs = []
    for ctx in _available_context_refs(project_id):
        if ctx["type"] in context_types:
            refs.append(SkillContextRef(**ctx, required=True, purpose="由自然语言草稿推断需要使用"))

    name = "样本无对比结果排查" if "orders_id" in lower or "样本" in lower else "新技能草稿"
    return SkillItem(
        id=_slugify(name),
        project_id=project_id,
        name=name,
        kind="business",
        description=text[:500],
        trigger_examples=[
            SkillTriggerExample(text=text[:120], source="owner_text", confirmed=True, confidence=0.95),
            SkillTriggerExample(text="用户询问样本没有对比结果", source="llm_fallback", confirmed=False, confidence=0.6),
        ],
        input_contract={"signals": ["自然语言问题"]},
        required_contexts=refs,
        required_tools=sorted(set(tool for tool in tools if tool in _TOOL_IDS)),
        instructions=[part.strip() for part in re.split(r"[。；;\n]", text) if part.strip()][:8],
        output_contract={"sections": ["结论", "证据", "下一步建议"], "must_cite_evidence": True},
        safety_policy={"readonly_only": True, "requires_confirmation_for_mutation": True},
        source_type="manual",
        raw_content=raw_text,
        status="draft",
    )


def _coerce_skill(project_id: str, raw_text: str, data: dict[str, Any]) -> SkillItem:
    name = str(data.get("name") or "").strip() or "新技能草稿"
    skill_id = str(data.get("id") or data.get("skill_id") or "").strip() or _slugify(name)
    kind = str(data.get("kind") or "business").strip()
    if kind not in {"business", "operational"}:
        kind = "business"

    trigger_examples = []
    for raw in data.get("trigger_examples") or []:
        if isinstance(raw, str):
            trigger_examples.append(SkillTriggerExample(text=raw, source="llm_expanded"))
        elif isinstance(raw, dict) and raw.get("text"):
            source = raw.get("source") or "llm_expanded"
            trigger_examples.append(SkillTriggerExample(
                text=str(raw["text"]),
                source=str(source),
                confirmed=bool(raw.get("confirmed", source == "owner_text")),
                confidence=raw.get("confidence"),
            ))
    if not trigger_examples:
        trigger_examples = [SkillTriggerExample(text=raw_text.strip()[:160], source="owner_text", confirmed=True, confidence=0.95)]

    required_contexts = []
    for raw in data.get("required_contexts") or []:
        if isinstance(raw, dict):
            required_contexts.append(SkillContextRef(
                type=str(raw.get("type") or "context"),
                id=str(raw.get("id") or ""),
                name=str(raw.get("name") or ""),
                required=bool(raw.get("required", True)),
                purpose=str(raw.get("purpose") or ""),
            ))

    required_tools = [
        str(tool) for tool in (data.get("required_tools") or [])
        if isinstance(tool, str) and tool in _TOOL_IDS
    ]

    instructions = [
        str(step).strip() for step in (data.get("instructions") or [])
        if str(step).strip()
    ]

    return SkillItem(
        id=skill_id,
        project_id=project_id,
        name=name,
        kind=kind,  # type: ignore[arg-type]
        description=str(data.get("description") or raw_text.strip()[:500]),
        trigger_examples=trigger_examples,
        input_contract=data.get("input_contract") if isinstance(data.get("input_contract"), dict) else {},
        required_contexts=required_contexts,
        required_tools=sorted(set(required_tools)),
        related_glossary_terms=[
            str(term) for term in (data.get("related_glossary_terms") or [])
            if isinstance(term, str)
        ],
        instructions=instructions,
        output_contract=data.get("output_contract") if isinstance(data.get("output_contract"), dict) else {},
        safety_policy=data.get("safety_policy") if isinstance(data.get("safety_policy"), dict) else {"readonly_only": True},
        source_type="manual",
        source_uri=str(data.get("source_uri") or ""),
        raw_content=raw_text,
        status="draft",
        version=1,
    )


async def draft_skill_from_text(project_id: str, raw_text: str) -> SkillItem:
    """Use a non-thinking LLM request to turn owner prose into a structured Skill draft."""
    if not raw_text.strip():
        raise ValueError("raw_text 不能为空")
    if not registry.get_project(project_id):
        raise ValueError(f"项目 '{project_id}' 不存在")

    context_refs = _available_context_refs(project_id)
    project = registry.get_project(project_id)
    prompt = f"""
请把项目 owner 的自然语言经验整理成 Viktor Skill 草稿。不要发散，不要补充凭空事实。

项目：{project.name if project else project_id}
项目 ID：{project_id}

可引用的上下文（只能从中选择；如果 owner 文本只提到类型但没有明确具体项，可返回 type 但 id 留空）：
{json.dumps(context_refs, ensure_ascii=False)}

可用工具 ID：
{json.dumps(KNOWN_TOOLS, ensure_ascii=False)}

owner 原文：
{raw_text}

请只输出 JSON object，字段：
{{
  "id": "英文/拼音风格短 id，kebab-case",
  "name": "中文短名称",
  "kind": "business 或 operational",
  "description": "一句话描述",
  "trigger_examples": [
    {{"text": "从原文直接提取或合理扩展的典型问法", "source": "owner_text|llm_expanded|glossary_expanded", "confirmed": false, "confidence": 0.0}}
  ],
  "input_contract": {{"signals": ["用户可能提供的关键输入，如 orders_id"]}},
  "required_contexts": [
    {{"type": "database|log|redis|object_storage|runtime|repository|context", "id": "若能匹配到可用上下文则填 id", "name": "显示名", "required": true, "purpose": "用途"}}
  ],
  "required_tools": ["工具 id"],
  "related_glossary_terms": ["术语"],
  "instructions": ["可执行步骤，按顺序"],
  "output_contract": {{"sections": ["结论", "证据", "下一步建议"], "must_cite_evidence": true}},
  "safety_policy": {{"readonly_only": true, "requires_confirmation_for_mutation": true}}
}}
""".strip()

    try:
        llm = create_llm(thinking=False, feature="skill_draft")
        response = await llm.ainvoke([
            SystemMessage(content="你是资深 AI 工程产品助手，只做结构化抽取。"),
            HumanMessage(content=prompt),
        ])
        content = response.content if isinstance(response.content, str) else str(response.content)
        return _coerce_skill(project_id, raw_text, _extract_json(content))
    except Exception as e:  # noqa: BLE001
        logger.warning("Skill LLM 草稿生成失败，使用规则兜底: {}", e)
        return _fallback_skill(project_id, raw_text)
