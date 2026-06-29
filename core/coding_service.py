"""Coding Task 编排服务。"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from typing import Any
from urllib.parse import quote

from langchain_core.messages import HumanMessage, SystemMessage
from loguru import logger

from core.coding_agent_loop import run_coding_agent, run_coding_clarification, run_coding_plan
from core.coding_policy import CodingPolicy
from core.coding_runtime import CodingRuntime
from core.coding_workspace import (
    changed_files,
    commit_all,
    git_cumulative_diff,
    git_diff,
    git_status,
    prepare_workspace,
    push_branch,
)
from core.audit.recorder import record_trace_event
from core.context_compaction import compact_text, estimate_tokens, should_compact
from core.database import SessionLocal
from core.intent import prepare_intent_context
from core.llm_client import create_llm
from core.llm_metrics import llm_observation_context
from core.models import CodingArtifactModel, CodingAttemptModel, CodingEventModel, CodingTaskModel
from core.explorer_agent import run_explorer
from core.prompt_builder import build_system_prompt
from core.language_defaults import (
    has_custom_test_command,
    resolve_language,
    resolve_lint_command,
    resolve_test_command,
)
from core.registry import registry
from core.report_store import build_report_url, save_report
from gitlab.merge_request_service import (
    create_merge_request,
    create_merge_request_note,
    list_open_merge_requests_by_source_branch,
)
from core.temporal import trigger
from settings import coding_agent_config
from settings import context_compaction_config
from settings import report_config
from settings import temporal_config


FINAL_STATUSES = {"completed", "failed", "cancelled", "plan_rejected"}
EXECUTION_ACTIVE_STATUSES = {"running", "cancelling"}
# rate_limited：LLM 限流类失败的可重试中间态（非终态），由 CodingTaskWorkflow 退避后重派。
# 纳入 startable 使手动 resume/continue 也能从限流态拉起。
EXECUTION_STARTABLE_STATUSES = {"plan_approved", "failed", "waiting_code_review", "rate_limited"}
AUTOMATED_REVIEW_PROVIDER = "hiart-claude-sonnet-4-6"
AUTOMATED_REVIEW_PROVIDER_ORDER = ["hiart-claude-sonnet-4-6", "hiart-claude-sonnet-4-6-azure"]
AUTOMATED_REVIEW_MODEL = "claude-sonnet-4-6"


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _coding_task_url(task_id: str, project_id: str = "") -> str:
    task_id = str(task_id or "").strip()
    if not task_id:
        return ""
    params = [f"task_id={quote(task_id, safe='')}"]
    project_id = str(project_id or "").strip()
    if project_id:
        params.append(f"project_id={quote(project_id, safe='')}")
    return f"{report_config.base_url.rstrip('/')}/coding?{'&'.join(params)}"


def _dt(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return None


def _repo_maintainer_mobile(project_id: str, repo_connector_id: str = "") -> str:
    connector = registry.get_repository_connector(project_id, repo_connector_id) if repo_connector_id else None
    if not connector:
        repos = registry.get_repository_connectors(project_id)
        connector = repos[0] if repos else None
    return str(getattr(connector, "maintainer_mobile", "") or "").strip() if connector else ""


def _clear_pending_owner(row: CodingTaskModel) -> None:
    row.pending_gate = ""
    row.pending_owner_mobile = ""
    row.pending_owner_label = ""


def _set_pending_owner(
    row: CodingTaskModel,
    *,
    gate: str,
    owner_mobile: str = "",
    owner_label: str = "",
) -> None:
    row.pending_gate = gate
    row.pending_owner_mobile = str(owner_mobile or "").strip()
    row.pending_owner_label = str(owner_label or "").strip()


def _set_repo_maintainer_pending_owner(row: CodingTaskModel, *, gate: str) -> None:
    mobile = _repo_maintainer_mobile(row.project_id, row.repo_connector_id)
    label = f"{row.repo_connector_id} maintainer" if row.repo_connector_id else "repo maintainer"
    _set_pending_owner(row, gate=gate, owner_mobile=mobile, owner_label=label if mobile else "")


def _task_to_dict(row: CodingTaskModel) -> dict[str, Any]:
    plan = _compat_plan(row)
    workspace = _compat_workspace(row)
    plan_markdown = _compat_plan_markdown(plan, row)
    result = row.result or {}
    return {
        "id": row.task_id,
        "task_id": row.task_id,
        "project_id": row.project_id,
        "title": _task_title(row.requirement),
        "description": row.requirement,
        "requirement": row.requirement,
        "status": row.status,
        "stage": row.stage,
        "message": row.message,
        "repo_id": row.repo_connector_id,
        "repo_connector_id": row.repo_connector_id,
        "branch": row.target_branch,
        "target_branch": row.target_branch,
        "work_branch": row.work_branch,
        "mr_url": row.mr_url,
        "report_id": row.report_id,
        "report_url": build_report_url(row.report_id) if row.report_id else "",
        "policy": row.policy or {},
        "metadata": {
            "policy": row.policy or {},
            "created_by": row.created_by,
            "created_by_mobile": row.created_by_mobile,
        },
        "control": row.control or {},
        "result": result,
        "clarification": result.get("clarification") if isinstance(result.get("clarification"), dict) else {},
        "plan_questions": result.get("plan_questions") if isinstance(result.get("plan_questions"), list) else [],
        "created_by": row.created_by,
        "created_by_mobile": row.created_by_mobile,
        "pending_gate": row.pending_gate,
        "pending_owner_mobile": row.pending_owner_mobile,
        "pending_owner_label": row.pending_owner_label,
        "plan_id": row.task_id,
        "plan": plan,
        "plan_markdown": plan_markdown,
        "plan_status": _compat_plan_status(row.status),
        "workspace_id": row.task_id if workspace else "",
        "workspace": workspace,
        "automated_review": result.get("automated_review") if isinstance(result.get("automated_review"), dict) else {},
        "code_review": [result.get("code_review")] if isinstance(result.get("code_review"), dict) else [],
        "created_at": _dt(row.created_at),
        "updated_at": _dt(row.updated_at),
    }


def _event_to_dict(row: CodingEventModel) -> dict[str, Any]:
    return {
        "id": row.id,
        "task_id": row.task_id,
        "attempt_id": row.attempt_id,
        "seq": row.seq,
        "event_type": row.event_type,
        "stage": row.stage,
        "message": row.message,
        "payload": row.payload or {},
        "created_at": _dt(row.created_at),
    }


def _attempt_to_dict(row: CodingAttemptModel) -> dict[str, Any]:
    return {
        "id": row.attempt_id,
        "attempt_id": row.attempt_id,
        "task_id": row.task_id,
        "project_id": row.project_id,
        "repo_connector_id": row.repo_connector_id,
        "status": row.status,
        "stage": row.stage,
        "workspace_path": row.workspace_path,
        "branch_name": row.branch_name,
        "base_commit": row.base_commit,
        "head_commit": row.head_commit,
        "plan": row.plan,
        "summary": row.summary,
        "test_results": row.test_results or {},
        "risk_flags": row.risk_flags or [],
        "created_at": _dt(row.created_at),
        "updated_at": _dt(row.updated_at),
    }


def _task_title(requirement: str) -> str:
    return next((line.strip() for line in requirement.splitlines() if line.strip()), "Coding task")[:120]


def _compat_plan_status(status: str) -> str:
    if status in {"created", "planning"}:
        return "planning"
    if status == "waiting_plan_review":
        return "waiting_review"
    if status == "waiting_clarification":
        return "waiting_clarification"
    if status == "plan_approved":
        return "approved"
    if status in {"paused"}:
        return "paused"
    if status in FINAL_STATUSES:
        return status
    return status


def _compat_plan(row: CodingTaskModel) -> dict[str, Any]:
    result = row.result or {}
    plan = result.get("plan")
    if isinstance(plan, dict):
        return plan
    return {
        "summary": _task_title(row.requirement),
        "steps": [
            "加载项目上下文并定位相关代码",
            "准备隔离 workspace",
            "执行 Coding Agent 修改",
            "生成 diff、报告，并按策略创建 MR",
        ],
        "files": [],
        "risks": [],
        "acceptance_criteria": ["变更经过自动风险检查", "输出 diff/报告供人工 review"],
    }


def _compat_plan_markdown(plan: dict[str, Any], row: CodingTaskModel | None = None) -> str:
    if row:
        result = row.result or {}
        raw_plan = result.get("plan_markdown") or result.get("approved_plan_markdown")
        if isinstance(raw_plan, str) and raw_plan.strip():
            return raw_plan.strip()
    lines = [f"## {plan.get('summary') or '执行计划'}"]
    sections = [
        ("步骤", plan.get("steps")),
        ("预计改动文件", plan.get("files")),
        ("风险", plan.get("risks")),
        ("验收标准", plan.get("acceptance_criteria")),
    ]
    for title, items in sections:
        if not items:
            continue
        lines.append(f"\n### {title}")
        for i, item in enumerate(items, start=1):
            lines.append(f"{i}. {item}" if title == "步骤" else f"- {item}")
    return "\n".join(lines).strip()


def _approved_plan_markdown(result: dict[str, Any]) -> str:
    approved_plan = str(result.get("approved_plan_markdown") or "").strip()
    if approved_plan:
        return approved_plan
    plan_review = result.get("plan_review") if isinstance(result.get("plan_review"), dict) else {}
    if plan_review.get("decision") == "approved":
        return str(result.get("plan_markdown") or "").strip()
    return ""


def _merge_execution_result(existing: dict[str, Any] | None, execution: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing or {})
    previous_execution = merged.get("execution") if isinstance(merged.get("execution"), dict) else {}
    merged["execution"] = {**previous_execution, **execution}
    for key in (
        "changed_files",
        "risk_flags",
        "report_id",
        "report_url",
        "mr_url",
        "workspace_path",
        "branch",
        "base_commit",
        "head_commit",
        "mr",
    ):
        if key in execution:
            merged[key] = execution[key]
    return merged


def _format_clarification_markdown(clarification: dict[str, Any]) -> str:
    if not clarification:
        return ""
    lines: list[str] = []
    term_mappings = clarification.get("term_mappings") or []
    if term_mappings:
        lines.append("## 术语映射")
        for item in term_mappings:
            if not isinstance(item, dict):
                continue
            user_term = str(item.get("user_term") or "").strip()
            code_terms = item.get("code_terms") or []
            code_text = ", ".join(str(term) for term in code_terms if str(term).strip())
            meaning = str(item.get("meaning") or "").strip()
            evidence = item.get("evidence") if isinstance(item.get("evidence"), list) else []
            head = user_term or code_text or "未命名术语"
            detail = f" -> {code_text}" if code_text else ""
            lines.append(f"- {head}{detail}: {meaning}")
            for ev in evidence[:3]:
                lines.append(f"  - 证据：{ev}")
    questions = clarification.get("questions") or []
    if questions:
        lines.append("\n## 需要用户回答的问题")
        for item in questions:
            if not isinstance(item, dict):
                continue
            lines.append(f"- {item.get('question')}")
            recommended = str(item.get("recommended") or "").strip()
            for option in item.get("options") or []:
                if not isinstance(option, dict):
                    continue
                marker = "（推荐）" if recommended and option.get("value") == recommended else ""
                lines.append(f"  - {option.get('label')}{marker}: {option.get('description')}")
    return "\n".join(lines).strip()


def _format_clarification_answers(clarification: dict[str, Any]) -> str:
    if not clarification or clarification.get("status") != "answered":
        return ""
    answers = clarification.get("answers") or {}
    if not isinstance(answers, dict) or not answers:
        return ""
    lines = ["## Clarification Answers", "用户已回答 Plan 前置问题，本次 Plan 必须按这些答案理解需求。"]
    term_markdown = _format_clarification_markdown({
        "term_mappings": clarification.get("term_mappings") or [],
        "questions": [],
    })
    if term_markdown:
        lines.append(term_markdown)
    questions = {
        str(item.get("id")): item
        for item in (clarification.get("questions") or [])
        if isinstance(item, dict) and item.get("id")
    }
    for question_id, answer in answers.items():
        question = questions.get(str(question_id), {})
        question_text = str(question.get("question") or question_id).strip()
        selected = answer if isinstance(answer, list) else [answer]
        labels: list[str] = []
        options = question.get("options") if isinstance(question.get("options"), list) else []
        for value in selected:
            value_text = str(value).strip()
            matched = next((opt for opt in options if isinstance(opt, dict) and str(opt.get("value")) == value_text), None)
            labels.append(str(matched.get("label") if matched else value_text))
        lines.append(f"- {question_text}：{', '.join(label for label in labels if label)}")
    return "\n".join(lines).strip()


async def _maybe_compact_coding_context(
    *,
    task_id: str,
    attempt_id: str,
    project_id: str,
    title: str,
    content: str,
) -> str:
    if not context_compaction_config.enabled:
        return content
    if not should_compact(content, context_compaction_config.threshold_tokens):
        return content
    compacted = await compact_text(
        content,
        title=title,
        target_tokens=context_compaction_config.target_tokens,
    )
    if not compacted:
        return content
    _save_artifact(
        task_id,
        attempt_id,
        project_id,
        "context_compaction",
        title,
        compacted,
        {
            "original_estimated_tokens": estimate_tokens(content),
            "threshold_tokens": context_compaction_config.threshold_tokens,
            "target_tokens": context_compaction_config.target_tokens,
        },
    )
    return compacted


def _compat_workspace(row: CodingTaskModel) -> dict[str, Any] | None:
    result = row.result or {}
    path = result.get("workspace_path")
    if not path:
        return None
    return {
        "workspace_id": row.task_id,
        "task_id": row.task_id,
        "project_id": row.project_id,
        "repo_id": row.repo_connector_id,
        "base_commit": result.get("base_commit") or "",
        "commit_sha": result.get("base_commit") or "",
        "path": path,
        "status": row.status,
        "result": result,
    }


def _extract_plan_summary(plan_markdown: str, requirement: str) -> dict[str, Any]:
    lines = [line.strip() for line in (plan_markdown or "").splitlines()]
    title = ""
    for line in lines:
        if line.startswith("#"):
            title = line.lstrip("#").strip()
            if title:
                break
    if not title:
        title = _task_title(requirement)
    return {
        "summary": title,
        "steps": _extract_section_items(plan_markdown, "Key Changes") or _extract_section_items(plan_markdown, "修改思路") or _extract_section_items(plan_markdown, "执行方案"),
        "files": _extract_section_items(plan_markdown, "Code Findings") or _extract_section_items(plan_markdown, "候选文件"),
        "risks": _extract_section_items(plan_markdown, "Impact") or _extract_section_items(plan_markdown, "风险/待确认") or _extract_section_items(plan_markdown, "风险"),
        "acceptance_criteria": _extract_section_items(plan_markdown, "Test Plan") or _extract_section_items(plan_markdown, "验证方式") or ["按 plan 中的验证方式检查 diff/报告"],
        "raw_markdown": plan_markdown,
    }


def _extract_section_items(markdown: str, section: str) -> list[str]:
    capture = False
    items: list[str] = []
    for raw in markdown.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            heading = line.lstrip("#").strip().rstrip(":：")
            if capture:
                break
            capture = heading == section
            continue
        if not capture:
            continue
        cleaned = re.sub(r"^[-*]\s+", "", line)
        cleaned = re.sub(r"^\d+[.)、]\s*", "", cleaned)
        if cleaned:
            items.append(cleaned[:500])
    return items


def _get_latest_artifact_content(task_id: str, artifact_type: str) -> str:
    db = SessionLocal()
    try:
        artifact = (
            db.query(CodingArtifactModel)
            .filter(CodingArtifactModel.task_id == task_id, CodingArtifactModel.artifact_type == artifact_type)
            .order_by(CodingArtifactModel.created_at.desc())
            .first()
        )
        return artifact.content if artifact else ""
    finally:
        db.close()


def _format_code_exploration(exploration: dict[str, Any]) -> str:
    """Turn explorer JSON into a compact, citation-friendly Markdown block."""
    if not exploration:
        return ""
    if exploration.get("error"):
        return f"## 探索失败\n{exploration.get('error')}"

    lines: list[str] = []
    summary = str(exploration.get("summary") or "").strip()
    if summary:
        lines.extend(["## 探索摘要", summary])

    relevant_files = exploration.get("relevant_files") or []
    if relevant_files:
        lines.append("\n## 已核对文件")
        for item in relevant_files[:12]:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "").strip()
            why = str(item.get("why") or "").strip()
            key_lines = str(item.get("key_lines") or "").strip()
            if path:
                suffix = f" ({key_lines})" if key_lines else ""
                detail = f": {why}" if why else ""
                lines.append(f"- {path}{suffix}{detail}")

    key_symbols = exploration.get("key_symbols") or []
    if key_symbols:
        lines.append("\n## 关键符号")
        for item in key_symbols[:16]:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol") or "").strip()
            file = str(item.get("file") or "").strip()
            lines_no = str(item.get("lines") or "").strip()
            if symbol or file:
                loc = f"{file}:{lines_no}" if lines_no else file
                lines.append(f"- {symbol or '(未命名符号)'} @ {loc}")

    searched = [str(x) for x in (exploration.get("searched_keywords") or []) if str(x).strip()]
    if searched:
        lines.append("\n## 已搜索关键词")
        lines.append(", ".join(searched[:30]))

    dead_ends = [str(x) for x in (exploration.get("dead_ends") or []) if str(x).strip()]
    if dead_ends:
        lines.append("\n## 已排除方向")
        for item in dead_ends[:10]:
            lines.append(f"- {item}")

    meta = exploration.get("_meta") or {}
    commit = str(meta.get("commit") or "").strip()
    if commit:
        lines.append(f"\n## 代码版本\n{commit}")

    return "\n".join(lines).strip()


def _update_task(task_id: str, **kwargs: Any) -> None:
    db = SessionLocal()
    try:
        row = db.get(CodingTaskModel, task_id)
        if row:
            for key, value in kwargs.items():
                setattr(row, key, value)
            db.commit()
    finally:
        db.close()


def _update_attempt(attempt_id: str, **kwargs: Any) -> None:
    db = SessionLocal()
    try:
        row = db.get(CodingAttemptModel, attempt_id)
        if row:
            for key, value in kwargs.items():
                setattr(row, key, value)
            db.commit()
    finally:
        db.close()


def _mr_metadata(mr: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": mr.get("id"),
        "iid": mr.get("iid"),
        "project_id": mr.get("project_id"),
        "state": mr.get("state"),
        "web_url": mr.get("web_url"),
        "source_branch": mr.get("source_branch"),
        "target_branch": mr.get("target_branch"),
    }


def emit_event(
    task_id: str,
    event_type: str,
    message: str,
    payload: dict[str, Any] | None = None,
    *,
    attempt_id: str = "",
    stage: str = "",
) -> None:
    db = SessionLocal()
    try:
        last = (
            db.query(CodingEventModel)
            .filter(CodingEventModel.task_id == task_id)
            .order_by(CodingEventModel.seq.desc())
            .first()
        )
        seq = (last.seq if last else 0) + 1
        db.add(CodingEventModel(
            task_id=task_id,
            attempt_id=attempt_id,
            seq=seq,
            event_type=event_type,
            stage=stage,
            message=message,
            payload=payload or {},
        ))
        db.commit()
    finally:
        db.close()


def _set_stage(task_id: str, attempt_id: str, stage: str, message: str, status: str | None = None) -> None:
    values: dict[str, Any] = {"stage": stage, "message": message}
    if status:
        values["status"] = status
    _update_task(task_id, **values)
    _update_attempt(attempt_id, stage=stage, **({"status": status} if status else {}))
    emit_event(task_id, "stage_changed", message, {"stage": stage, "status": status}, attempt_id=attempt_id, stage=stage)


def _control(task_id: str) -> dict[str, Any]:
    db = SessionLocal()
    try:
        row = db.get(CodingTaskModel, task_id)
        return dict(row.control or {}) if row else {}
    finally:
        db.close()


def _check_control(task_id: str) -> None:
    control = _control(task_id)
    if control.get("cancel_requested"):
        raise InterruptedError("任务已被用户取消")
    if control.get("pause_requested"):
        _update_task(task_id, status="paused", stage="paused", message="任务已在安全点暂停")
        emit_event(task_id, "paused", "任务已在安全点暂停", control, stage="paused")
        raise InterruptedError("任务已暂停")


def _resolve_repo(project_id: str, repo_connector_id: str = "") -> tuple[str, str, str]:
    project = registry.get_project(project_id)
    if not project:
        raise ValueError(f"项目 {project_id} 不存在")
    if repo_connector_id:
        repo = registry.get_repository_connector(project_id, repo_connector_id)
        if not repo:
            raise ValueError(f"Repository Connector {repo_connector_id} 不存在")
        return repo.git_url, repo.default_branch or "master", repo.id
    repos = registry.get_repository_connectors(project_id)
    if repos:
        repo = repos[0]
        return repo.git_url, repo.default_branch or "master", repo.id
    if project.git_url:
        return project.git_url, project.default_branch or "master", ""
    raise ValueError(f"项目 {project_id} 未配置代码仓库")


def _resolve_test_flow(project_id: str, repo_connector_id: str = "") -> tuple[str, str, str]:
    """解析该仓库的测试流程（B 层）：返回 (language, test_command, lint_command)。

    回退链：connector 显式覆盖 > 该语言内置默认（core.language_defaults）。
    connector 不存在（项目级 git_url）时全部为空，决策层据此退化为通用提示。
    """
    connector = None
    if repo_connector_id:
        connector = registry.get_repository_connector(project_id, repo_connector_id)
    else:
        repos = registry.get_repository_connectors(project_id)
        connector = repos[0] if repos else None
    language = resolve_language(connector)
    return language, resolve_test_command(connector), resolve_lint_command(connector)


def _augment_policy_for_test_flow(policy: CodingPolicy, project_id: str, repo_connector_id: str) -> None:
    """把该仓库的测试/lint 命令并入 allowed_commands；仅自定义命令才开依赖安装窄口子。

    - 默认配置（用内置默认命令、依赖已在镜像/venv）：只放行测试/lint 命令本身，不开安装口子，保持快。
    - 用户声明了自定义 test_command（往往带 Viktor 没有的依赖）：额外窄口子放行
      `pip install` / `npm install`，允许执行阶段补依赖。
    """
    connector = None
    if repo_connector_id:
        connector = registry.get_repository_connector(project_id, repo_connector_id)
    else:
        repos = registry.get_repository_connectors(project_id)
        connector = repos[0] if repos else None

    test_command = resolve_test_command(connector)
    lint_command = resolve_lint_command(connector)
    for cmd in (test_command, lint_command):
        cmd = (cmd or "").strip()
        if cmd and cmd not in policy.allowed_commands:
            policy.allowed_commands.append(cmd)

    if connector is not None and has_custom_test_command(connector):
        for install_cmd in ("pip install", "python -m pip install", "npm install", "npm ci"):
            if install_cmd not in policy.allowed_commands:
                policy.allowed_commands.append(install_cmd)


def _test_flow_section(language: str, test_command: str, lint_command: str) -> str:
    """生成注入 prompt 的「测试流程」段（A 层哲学 + B 层项目实际命令）。"""
    lines = [
        "## 测试流程",
        "测试哲学（务必遵守）：",
        "- 由窄到宽：只跑与本次改动直接相关的测试，确认通过后再按需扩大；不要无脑跑全量。",
        "- 项目若本就没有测试，不要为了「有测试」而编造或新增无关测试。",
        "- 不要顺手修复与本任务无关的 bug 或既有失败用例，只对你的改动负责。",
        "- 依赖已在镜像 / 预热 venv 中就绪并已注入 PATH：直接用裸命令调用，"
        "不要加 `.venv/` 前缀，默认配置下也无需 `pip install` / `npm install`。",
    ]
    if test_command:
        lines.append(f"- 本项目测试命令：`{test_command}`（可按需追加具体测试文件/用例缩小范围）。")
    else:
        lines.append("- 本项目未声明测试命令：若 workspace 中存在测试，按其约定运行；否则跳过测试阶段并在总结里说明。")
    if lint_command:
        lines.append(f"- 本项目 lint 命令：`{lint_command}`（改动涉及风格/静态检查时运行）。")
    if language:
        lines.append(f"- 项目语言：{language}。")
    return "\n".join(lines)


def start_coding_task(
    *,
    project_id: str,
    requirement: str,
    repo_connector_id: str = "",
    target_branch: str = "",
    policy: dict[str, Any] | None = None,
    create_mr: bool | None = None,
    created_by: str = "",
    created_by_mobile: str = "",
) -> str:
    if not coding_agent_config.enabled:
        raise ValueError("coding_agent.enabled=false")
    if not requirement.strip():
        raise ValueError("requirement 不能为空")
    git_url, default_branch, resolved_repo = _resolve_repo(project_id, repo_connector_id)
    _ = git_url
    effective_policy = CodingPolicy.from_dict(policy)
    _augment_policy_for_test_flow(effective_policy, project_id, resolved_repo)
    should_create_mr = coding_agent_config.default_create_mr if create_mr is None else bool(create_mr)
    if should_create_mr:
        effective_policy.allow_push_branch = True
        effective_policy.allow_create_mr = True

    task_id = _new_id("ct")
    branch_target = target_branch or default_branch
    work_branch = f"viktor/{task_id}"
    db = SessionLocal()
    try:
        db.add(CodingTaskModel(
            task_id=task_id,
            project_id=project_id,
            requirement=requirement,
            status="created",
            stage="created",
            message="Coding task 已创建",
            repo_connector_id=resolved_repo,
            target_branch=branch_target,
            work_branch=work_branch,
            policy=effective_policy.to_dict(),
            control={},
            result={},
            created_by=created_by,
            created_by_mobile=created_by_mobile,
        ))
        db.commit()
    finally:
        db.close()

    emit_event(task_id, "created", "Coding task 已创建，开始生成 plan", {"project_id": project_id, "repo_connector_id": resolved_repo})
    _dispatch_coding_job(task_id, "planning")
    return task_id


def _dispatch_coding_job(task_id: str, mode: str) -> None:
    """派发一个 coding Job（planning / execution）执行任务。

    执行从 web pod 线程迁出到独立 K8s Job：滚动重启不再杀任务，状态/control 全走 DB。
    并发已满时不硬失败，置「排队中」由周期 reconcile sweep 重投。
    其它 K8s/RBAC/manifest 错误要落到 task 本身，避免上层认为 task 未创建而反复新建。
    """
    from core.coding_job_dispatch import JobConcurrencyError, create_coding_job

    try:
        create_coding_job(task_id, mode)
    except JobConcurrencyError as e:
        logger.warning("[coding] task {} 并发已满，排队等待: {}", task_id, e)
        _update_task(task_id, message="排队中：并发已满，稍后自动重试")
        emit_event(task_id, "queued", "排队中：并发已满，稍后自动重试", {"reason": str(e)})
    except Exception as e:  # noqa: BLE001
        message = f"派发 coding Job 失败：{e}"
        logger.exception("[coding] task {} 派发 {} Job 失败: {}", task_id, mode, e)
        _update_task(task_id, status="failed", stage="dispatch_failed", message=message)
        emit_event(task_id, "dispatch_failed", message, {"mode": mode, "error": str(e)}, stage="dispatch_failed")


def answer_clarification(task_id: str, *, answers: dict[str, Any], reviewer: str = "") -> dict[str, Any]:
    if not answers:
        raise ValueError("请至少回答一个问题")
    db = SessionLocal()
    try:
        row = db.get(CodingTaskModel, task_id)
        if not row:
            raise ValueError("任务不存在")
        if row.status != "waiting_clarification":
            raise ValueError(f"当前状态 {row.status} 不允许提交澄清答案")
        result = dict(row.result or {})
        clarification = dict(result.get("clarification") or {})
        questions = clarification.get("questions") if isinstance(clarification.get("questions"), list) else []
        question_ids = {str(item.get("id")) for item in questions if isinstance(item, dict) and item.get("id")}
        unknown_ids = [str(key) for key in answers if str(key) not in question_ids]
        if unknown_ids:
            raise ValueError(f"未知问题 ID: {', '.join(unknown_ids)}")
        missing_ids = [qid for qid in question_ids if qid not in answers]
        if missing_ids:
            raise ValueError(f"仍有问题未回答: {', '.join(missing_ids)}")
        normalized_answers: dict[str, Any] = {}
        for item in questions:
            if not isinstance(item, dict):
                continue
            question_id = str(item.get("id"))
            value = answers.get(question_id)
            values = value if isinstance(value, list) else [value]
            selected = [str(option_value).strip() for option_value in values if str(option_value).strip()]
            if not selected:
                raise ValueError(f"{question_id} 不能为空")
            normalized_answers[question_id] = selected if len(selected) > 1 else selected[0]
        clarification["status"] = "answered"
        clarification["answers"] = normalized_answers
        clarification["answered_at"] = datetime.now().isoformat()
        clarification["reviewer"] = reviewer
        result["clarification"] = clarification
        result.pop("approved_plan_markdown", None)
        result.pop("approved_at", None)
        row.result = result
        row.status = "planning"
        row.stage = "planning"
        row.message = "已收到澄清答案，正在生成 Plan"
        _clear_pending_owner(row)
        project_id = row.project_id
        db.commit()
    finally:
        db.close()

    answer_markdown = _format_clarification_answers({
        "status": "answered",
        "answers": normalized_answers,
        "questions": questions,
        "term_mappings": clarification.get("term_mappings") or [],
    })
    _save_artifact(
        task_id,
        "",
        project_id,
        "clarification_answer",
        "Plan 前置澄清答案",
        answer_markdown,
        {"answers": normalized_answers, "reviewer": reviewer},
    )
    emit_event(
        task_id,
        "clarification_answered",
        "已收到澄清答案，正在生成 Plan",
        {"answers": normalized_answers, "reviewer": reviewer},
        stage="planning",
    )
    _dispatch_coding_job(task_id, "planning")
    return get_task(task_id) or {"task_id": task_id, "status": "planning"}


def request_plan_revision(task_id: str, *, comment: str, reviewer: str = "") -> dict[str, Any]:
    revision_comment = comment.strip()
    if not revision_comment:
        raise ValueError("请填写需要 Agent 修正 Plan 的审核意见")
    db = SessionLocal()
    try:
        row = db.get(CodingTaskModel, task_id)
        if not row:
            raise ValueError("任务不存在")
        if row.status != "waiting_plan_review":
            raise ValueError(f"当前状态 {row.status} 不允许要求修正 Plan")
        result = dict(row.result or {})
        revisions = list(result.get("plan_revisions") or [])
        revisions.append({
            "comment": revision_comment,
            "reviewer": reviewer,
            "requested_at": datetime.now().isoformat(),
            "previous_plan_markdown": str(result.get("plan_markdown") or ""),
        })
        result["plan_revisions"] = revisions
        result["plan_review"] = {
            "decision": "revision_requested",
            "comment": revision_comment,
            "reviewer": reviewer,
            "reviewed_at": datetime.now().isoformat(),
        }
        result.pop("approved_plan_markdown", None)
        result.pop("approved_at", None)
        row.result = result
        row.status = "planning"
        row.stage = "planning"
        row.message = "已收到审核意见，正在重新生成 Plan"
        _clear_pending_owner(row)
        project_id = row.project_id
        db.commit()
    finally:
        db.close()
    _save_artifact(
        task_id,
        "",
        project_id,
        "plan_review",
        "Plan 修正意见",
        revision_comment,
        {"decision": "revision_requested", "reviewer": reviewer},
    )
    emit_event(
        task_id,
        "plan_revision_requested",
        "已收到审核意见，正在重新生成 Plan",
        {"comment": revision_comment, "reviewer": reviewer},
        stage="planning",
    )
    _dispatch_coding_job(task_id, "planning")
    return get_task(task_id) or {"task_id": task_id, "status": "planning"}


# 应当有 Job 在跑的状态：这些状态下若 backing Job 不存在，则任务是孤儿。
_RECONCILE_STATUSES = {"planning", "running", "cancelling", "reviewing_code"}


def reconcile_orphaned_coding_tasks(grace_seconds: int | None = None) -> int:
    """回收孤儿 coding task：处于「应在跑」状态但 Job 已不存在的，翻成 failed（可 resume）。

    - 只查 Job 对象是否存在（不查 pod，pod 会 CrashLoop churn，Job 才是持久句柄）。
    - grace 窗口：只回收 updated_at 早于窗口的，避免误杀刚派发、runner 尚未刷状态的任务。
      runner 一启动就刷 status/events 推高 updated_at，活任务不会落窗。
    - 顺带：cancelling 超 cancel_force_seconds 且 Job 仍活跃 → 强删 Job（有界强杀）。
    返回被回收的任务数。
    """
    # Temporal 接管后：孤儿检测由 CodingTaskWorkflow 自带的 Job 状态轮询负责，
    # 旧 reconciler 必须停手，否则与 workflow 双重驱动（误把在编排中的任务翻 failed）。
    if temporal_config.enabled:
        return 0

    from datetime import timedelta

    from core.coding_job_dispatch import delete_job, find_active_job, job_exists_for_task

    if grace_seconds is None:
        grace_seconds = coding_agent_config.reconcile_grace_seconds
    cutoff = datetime.now() - timedelta(seconds=grace_seconds)

    db = SessionLocal()
    try:
        rows = (
            db.query(CodingTaskModel)
            .filter(CodingTaskModel.status.in_(_RECONCILE_STATUSES))
            .filter(CodingTaskModel.updated_at < cutoff)
            .all()
        )
        candidates = [(r.task_id, r.status) for r in rows]
    finally:
        db.close()

    cancel_force = coding_agent_config.cancel_force_seconds
    force_cutoff = datetime.now() - timedelta(seconds=cancel_force)
    reclaimed = 0
    for task_id, status in candidates:
        try:
            # cancelling 拖太久且 Job 仍活跃：强删 Job 兜住取消。
            if status == "cancelling":
                active = find_active_job(task_id)
                if active is not None:
                    row = get_task(task_id)
                    updated = row.get("updated_at") if row else None
                    if not updated or _parse_dt(updated) < force_cutoff:
                        delete_job(task_id, "execution")
                        delete_job(task_id, "planning")
                        _update_task(task_id, status="cancelled", stage="cancelled", message="任务已强制取消")
                        emit_event(task_id, "cancelled", "任务已强制取消（Job 超时未在安全点停止）", {}, stage="cancelled")
                        reclaimed += 1
                    continue
                # 无活跃 Job 了，按孤儿处理（落到下面）。
            if job_exists_for_task(task_id):
                continue
            _update_task(task_id, status="failed", stage="failed",
                         message="执行 Job 已丢失，已标记失败，可重新执行")
            emit_event(task_id, "failed", "执行 Job 丢失，已标记失败", {"reason": "orphaned"}, stage="failed")
            reclaimed += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("[coding] reconcile task {} 失败，忽略: {}", task_id, e)
    if reclaimed:
        logger.info("[coding] reconcile 回收 {} 个孤儿任务", reclaimed)
    return reclaimed


def _parse_dt(value: Any) -> datetime:
    """把 get_task 返回的 updated_at（可能是 isoformat 字符串）解析为 datetime；失败给个旧值。"""
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return datetime.min


# 各状态「卡住」阈值（秒）。waiting_code_review 不扫——等人合 MR 属正常。
_STUCK_THRESHOLDS: dict[str, int] = {
    "queued": 600,
    "planning": 1800,
    "running": 1800,
    "reviewing_code": 1800,
    # 退避重试期内停 rate_limited 属正常；超 1h 仍未脱离说明编排没在驱动（如 worker 挂/重试耗尽未落终态），告警。
    "rate_limited": 3600,
    "waiting_clarification": 86400,
    "waiting_plan_review": 86400,
}
# 同一任务两次卡住告警的最小间隔（秒），防刷屏。
_STUCK_NOTIFY_COOLDOWN = 3600


def scan_stuck_coding_tasks() -> int:
    """扫描卡在某状态过久的 coding task，发钉钉告警（不改任务状态）。

    - 各状态阈值见 _STUCK_THRESHOLDS；waiting_code_review 不在其中（等人合 MR 正常）。
    - 冷却：task.result['stuck_notified_at'] 记上次告警时间，不足 _STUCK_NOTIFY_COOLDOWN 跳过。
    - 只告警不改状态；Temporal 模式下也扫（stuck 可见性两种模式都有价值）。
    返回命中（已发或拟发告警）的任务数。
    """
    now = datetime.now()
    db = SessionLocal()
    try:
        rows = (
            db.query(CodingTaskModel)
            .filter(CodingTaskModel.status.in_(list(_STUCK_THRESHOLDS)))
            .all()
        )
        stuck: list[dict[str, Any]] = []
        for r in rows:
            threshold = _STUCK_THRESHOLDS.get(r.status)
            if threshold is None:
                continue
            updated = _parse_dt(r.updated_at)
            stuck_for = (now - updated).total_seconds()
            if stuck_for <= threshold:
                continue
            stuck.append({
                "task_id": r.task_id,
                "status": r.status,
                "project_id": r.project_id,
                "created_by_mobile": r.created_by_mobile or "",
                "result": dict(r.result or {}),
                "stuck_for": stuck_for,
            })
    finally:
        db.close()

    hit = 0
    for item in stuck:
        task_id = item["task_id"]
        try:
            result = item["result"]
            last_raw = result.get("stuck_notified_at")
            if last_raw:
                last_dt = _parse_dt(last_raw)
                if (now - last_dt).total_seconds() < _STUCK_NOTIFY_COOLDOWN:
                    continue  # 冷却期内，跳过

            minutes = int(item["stuck_for"] // 60)
            title = "Viktor coding 任务卡住告警"
            text = (
                f"### {title}\n\n"
                f"- 任务: `{task_id}`\n"
                f"- 卡住状态: `{item['status']}`\n"
                f"- 已卡: 约 {minutes} 分钟\n"
                f"- 项目: `{item['project_id']}`\n"
            )
            # lazy import 规避循环 import（issue_intake_service 顶层不 import 本模块，
            # 但本模块顶层 import 它会让首个被加载方成环；函数内引入最稳）。
            from core.issue_intake_service import _notify_project_dingtalk

            extra = [item["created_by_mobile"]] if item["created_by_mobile"] else []
            _notify_project_dingtalk(item["project_id"], title, text, extra_mobiles=extra)

            # 写回冷却时间戳（result 是 JSON 字段，整体读改写）。
            result["stuck_notified_at"] = now.isoformat()
            _update_task(task_id, result=result)
            hit += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("[coding] scan_stuck task {} 告警失败，忽略: {}", task_id, e)
    if hit:
        logger.info("[coding] scan_stuck 发出 {} 个卡住告警", hit)
    return hit


async def run_coding_planning(task_id: str) -> None:
    db = SessionLocal()
    try:
        task = db.get(CodingTaskModel, task_id)
        if not task:
            raise ValueError(f"任务 {task_id} 不存在")
        if task.status not in {"created", "planning"}:
            emit_event(task_id, "planning_skipped", f"当前状态 {task.status} 不需要重新生成 plan", {"status": task.status})
            return
        project_id = task.project_id
        requirement = task.requirement
        repo_connector_id = task.repo_connector_id or ""
        result = dict(task.result or {})
        plan_revisions = list(result.get("plan_revisions") or [])
        previous_plan = str(result.get("plan_markdown") or "")
        clarification = dict(result.get("clarification") or {})
    finally:
        db.close()

    _update_task(task_id, status="planning", stage="planning", message="正在加载项目上下文")
    emit_event(task_id, "stage_changed", "正在加载项目上下文", {"stage": "planning", "status": "planning"}, stage="planning")
    planning_requirement = requirement
    if plan_revisions:
        latest_revision = plan_revisions[-1]
        revision_comment = str(latest_revision.get("comment") or "").strip()
        planning_requirement = (
            f"{requirement}\n\n"
            "## Plan Review 修正意见\n"
            f"{revision_comment}\n\n"
            "请基于上述审核意见重新生成完整 Plan。保留已确认的上下文，修正错误假设、过期业务知识、"
            "遗漏的验证步骤，并让新 Plan 可以直接作为后续执行阶段的压缩上下文。"
        )
        if previous_plan.strip():
            planning_requirement += f"\n\n## 上一版 Plan\n{previous_plan}"
    clarification_answers = _format_clarification_answers(clarification)
    if clarification_answers:
        planning_requirement = f"{planning_requirement}\n\n{clarification_answers}"
    trace_id = uuid.uuid4().hex
    trace_session_id = f"codetask:{task_id}"
    trace_topic_thread_id = "planning"
    intent_context = prepare_intent_context(
        project_id=project_id,
        user_message=planning_requirement,
        trace_id=trace_id,
        session_id=trace_session_id,
        topic_thread_id=trace_topic_thread_id,
        trace_meta={"scope": "codetask", "task_id": task_id, "stage": "planning"},
    )
    with llm_observation_context(
        scope="codetask",
        task_id=task_id,
        project_id=project_id,
        stage="planning",
        trace_id=trace_id,
        session_id=trace_session_id,
        topic_thread_id=trace_topic_thread_id,
    ):
        project_context = await build_system_prompt(
            project_id,
            planning_requirement,
            enable_routing=True,
            retrieval_context=intent_context.retrieval_context,
        )
        project_context = await _maybe_compact_coding_context(
            task_id=task_id,
            attempt_id="",
            project_id=project_id,
            title="Plan 阶段项目上下文压缩",
            content=project_context,
        )
    # 规划阶段就告知测试栈，让 Plan「对症下药」（附加在压缩之后，避免被压缩丢弃）。
    _pl_lang, _pl_test, _pl_lint = _resolve_test_flow(project_id, repo_connector_id)
    project_context = f"{project_context}\n\n{_test_flow_section(_pl_lang, _pl_test, _pl_lint)}"
    if intent_context.route is not None:
        _save_artifact(
            task_id,
            "",
            project_id,
            "intent_route",
            "Planning 阶段意图路由",
            json.dumps(intent_context.route.model_dump(), ensure_ascii=False, indent=2),
            {"trace_id": trace_id, "route": intent_context.route.model_dump()},
        )
    _save_artifact(task_id, "", project_id, "context", "Plan 阶段项目上下文快照", project_context)
    _check_control(task_id)

    _update_task(task_id, status="planning", stage="exploring_code", message="正在只读探索代码并压缩关键上下文")
    emit_event(
        task_id,
        "stage_changed",
        "正在只读探索代码并压缩关键上下文",
        {"stage": "exploring_code", "status": "planning"},
        stage="exploring_code",
    )
    exploration_task = (
        "请为下面 coding task 做 Plan 前置代码探索。目标不是修改代码，而是定位真实实现位置、关键常量/函数、"
        "调用链、验证入口和需要避开的旧业务知识。必须使用实际 code_grep/code_read 结果，不要只给搜索建议。\n\n"
        f"## Coding Task\n{planning_requirement}\n\n"
        "## 项目上下文摘录\n"
        f"{project_context[:12000]}"
    )
    with llm_observation_context(
        scope="codetask",
        task_id=task_id,
        project_id=project_id,
        stage="exploring_code",
        trace_id=trace_id,
        session_id=trace_session_id,
        topic_thread_id=trace_topic_thread_id,
    ):
        code_exploration = await run_explorer(
            project_id, exploration_task, connector_id=repo_connector_id
        )
    code_exploration_markdown = _format_code_exploration(code_exploration)
    with llm_observation_context(
        scope="codetask",
        task_id=task_id,
        project_id=project_id,
        stage="exploring_code",
        trace_id=trace_id,
        session_id=trace_session_id,
        topic_thread_id=trace_topic_thread_id,
    ):
        code_exploration_markdown = await _maybe_compact_coding_context(
            task_id=task_id,
            attempt_id="",
            project_id=project_id,
            title="Plan 前置代码探索压缩",
            content=code_exploration_markdown,
        )
    _save_artifact(
        task_id,
        "",
        project_id,
        "code_exploration",
        "Plan 前置代码探索",
        code_exploration_markdown or json.dumps(code_exploration, ensure_ascii=False, indent=2),
        {"exploration": code_exploration},
    )
    emit_event(
        task_id,
        "code_exploration_completed",
        "Plan 前置代码探索完成",
        {
            "summary": str(code_exploration.get("summary") or "")[:1000] if isinstance(code_exploration, dict) else "",
            "relevant_files": code_exploration.get("relevant_files", []) if isinstance(code_exploration, dict) else [],
            "error": code_exploration.get("error", "") if isinstance(code_exploration, dict) else "",
        },
        stage="exploring_code",
    )
    _check_control(task_id)

    if clarification.get("status") != "answered":
        _update_task(task_id, status="planning", stage="terminology_check", message="正在检查是否需要术语和范围澄清")
        emit_event(
            task_id,
            "stage_changed",
            "正在检查是否需要术语和范围澄清",
            {"stage": "terminology_check", "status": "planning"},
            stage="terminology_check",
        )
        with llm_observation_context(
            scope="codetask",
            task_id=task_id,
            project_id=project_id,
            stage="terminology_check",
            trace_id=trace_id,
            session_id=trace_session_id,
            topic_thread_id=trace_topic_thread_id,
        ):
            clarification_result = await run_coding_clarification(
                requirement=planning_requirement,
                project_context=project_context,
                code_exploration=code_exploration_markdown,
            )
        clarification_markdown = _format_clarification_markdown(clarification_result)
        _save_artifact(
            task_id,
            "",
            project_id,
            "clarification",
            "Plan 前置澄清检查",
            clarification_markdown or json.dumps(clarification_result, ensure_ascii=False, indent=2),
            {"clarification": clarification_result},
        )
        if clarification_result.get("needs_clarification") and clarification_result.get("questions"):
            db = SessionLocal()
            try:
                row = db.get(CodingTaskModel, task_id)
                if not row:
                    raise ValueError("任务不存在")
                if row.status not in {"created", "planning"}:
                    emit_event(
                        task_id,
                        "clarification_discarded",
                        f"澄清问题已生成但任务状态已变为 {row.status}，不覆盖当前状态",
                        {"status": row.status},
                        stage=row.stage,
                    )
                    return
                result = dict(row.result or {})
                result["clarification"] = {
                    **clarification_result,
                    "status": "waiting",
                    "generated_at": datetime.now().isoformat(),
                }
                row.result = result
                row.status = "waiting_clarification"
                row.stage = "waiting_clarification"
                row.message = "需要先回答术语或范围问题，之后再生成 Plan"
                _set_pending_owner(
                    row,
                    gate="clarification",
                    owner_mobile=row.created_by_mobile,
                    owner_label=row.created_by,
                )
                db.commit()
            finally:
                db.close()
            emit_event(
                task_id,
                "clarification_requested",
                "需要用户回答术语或范围问题",
                {"clarification": clarification_result},
                stage="waiting_clarification",
            )
            return

        db = SessionLocal()
        try:
            row = db.get(CodingTaskModel, task_id)
            if row:
                result = dict(row.result or {})
                result["clarification"] = {
                    **clarification_result,
                    "status": "not_required",
                    "checked_at": datetime.now().isoformat(),
                }
                row.result = result
                db.commit()
        finally:
            db.close()
        emit_event(
            task_id,
            "clarification_not_required",
            "术语和范围检查完成，无需阻塞提问",
            {"clarification": clarification_result},
            stage="terminology_check",
        )
        if clarification_markdown:
            code_exploration_markdown = f"{code_exploration_markdown}\n\n## Plan 前置澄清检查\n{clarification_markdown}".strip()
        _check_control(task_id)

    _update_task(task_id, status="planning", stage="drafting_plan", message="正在基于已核对代码生成正式 Plan")
    emit_event(
        task_id,
        "stage_changed",
        "正在基于已核对代码生成正式 Plan",
        {"stage": "drafting_plan", "status": "planning"},
        stage="drafting_plan",
    )
    with llm_observation_context(
        scope="codetask",
        task_id=task_id,
        project_id=project_id,
        stage="drafting_plan",
        trace_id=trace_id,
        session_id=trace_session_id,
        topic_thread_id=trace_topic_thread_id,
    ):
        plan_markdown, plan_questions = await run_coding_plan(
            requirement=planning_requirement,
            project_context=project_context,
            code_exploration=code_exploration_markdown,
        )
    if not plan_markdown.strip():
        raise RuntimeError("Coding Agent 未生成 plan")
    record_trace_event(
        trace_id=trace_id,
        event_type="final_answer",
        project_id=project_id,
        session_id=trace_session_id,
        topic_thread_id=trace_topic_thread_id,
        payload={
            "scope": "codetask",
            "task_id": task_id,
            "stage": "drafting_plan",
            "content": plan_markdown,
            "questions": plan_questions,
        },
    )
    plan = _extract_plan_summary(plan_markdown, requirement)
    _save_artifact(task_id, "", project_id, "plan", "Coding Plan", plan_markdown, {"plan": plan})

    db = SessionLocal()
    try:
        row = db.get(CodingTaskModel, task_id)
        if not row:
            raise ValueError("任务不存在")
        if row.status not in {"created", "planning"}:
            emit_event(task_id, "planning_discarded", f"Plan 已生成但任务状态已变为 {row.status}，不覆盖当前状态", {"status": row.status})
            return
        result = dict(row.result or {})
        result.update({
            "plan": plan,
            "plan_markdown": plan_markdown,
            "plan_questions": plan_questions,
            "plan_generated_at": datetime.now().isoformat(),
        })
        result.pop("approved_plan_markdown", None)
        result.pop("approved_at", None)
        row.result = result
        row.status = "waiting_plan_review"
        row.stage = "waiting_plan_review"
        row.message = "Plan 已生成，等待人工审核"
        _set_repo_maintainer_pending_owner(row, gate="plan_review")
        db.commit()
    finally:
        db.close()
    emit_event(task_id, "plan_generated", "Plan 已生成，等待人工审核", {"plan": plan}, stage="waiting_plan_review")


async def run_coding_task(task_id: str) -> None:
    db = SessionLocal()
    try:
        task = db.get(CodingTaskModel, task_id)
        if not task:
            raise ValueError(f"任务 {task_id} 不存在")
        if task.status not in {"plan_approved", "running", "paused"}:
            raise ValueError(f"任务状态 {task.status} 不允许启动执行，请先通过 Plan 审核")
        task_status = task.status
        project_id = task.project_id
        requirement = task.requirement
        repo_connector_id = task.repo_connector_id or ""
        target_branch = task.target_branch
        work_branch = task.work_branch
        existing_mr_url = task.mr_url or ""
        policy = CodingPolicy.from_dict(task.policy or {})
        result = dict(task.result or {})
        approved_plan = _approved_plan_markdown(result)
        continuation_instruction = _latest_continuation_instruction(result)
    finally:
        db.close()

    if not approved_plan.strip() and task_status == "plan_approved":
        approved_plan = _get_latest_artifact_content(task_id, "plan")
    if not approved_plan.strip():
        raise ValueError("任务缺少已审核 Plan，无法启动执行")

    attempt_id = _new_id("ca")
    db = SessionLocal()
    try:
        db.add(CodingAttemptModel(
            attempt_id=attempt_id,
            task_id=task_id,
            project_id=project_id,
            repo_connector_id=repo_connector_id,
            status="running",
            stage="created",
            branch_name=work_branch,
        ))
        db.commit()
    finally:
        db.close()

    _set_stage(task_id, attempt_id, "preparing_context", "正在加载已审核 Plan 作为执行上下文", status="running")
    tf_language, tf_test_command, tf_lint_command = _resolve_test_flow(project_id, repo_connector_id)
    execution_context = (
        "## 已审核 Coding Plan\n"
        f"{approved_plan}\n\n"
        "## 任务元信息\n"
        f"- project_id: {project_id}\n"
        f"- repo_connector_id: {repo_connector_id or 'default'}\n"
        f"- target_branch: {target_branch}\n\n"
        f"{_test_flow_section(tf_language, tf_test_command, tf_lint_command)}\n\n"
        "## 执行要求\n"
        "这是 Plan Review 后的压缩上下文。严格围绕已审核 Plan 执行；"
        "通过 workspace 内的代码搜索和读取补齐实现细节。若实际代码与 Plan 不一致，"
        "优先通过事件和最终总结说明偏差，不要扩大修改范围。"
    )
    if continuation_instruction:
        execution_context += (
            "\n\n## 继续执行 / Review 修复指令\n"
            f"{continuation_instruction}\n\n"
            "本轮必须优先处理上述 review 修复指令，完成后提交新 diff。"
        )
    with llm_observation_context(
        scope="codetask",
        task_id=task_id,
        attempt_id=attempt_id,
        project_id=project_id,
        stage="preparing_context",
    ):
        execution_context = await _maybe_compact_coding_context(
            task_id=task_id,
            attempt_id=attempt_id,
            project_id=project_id,
            title="执行阶段上下文压缩",
            content=execution_context,
        )
    _save_artifact(task_id, attempt_id, project_id, "context", "执行阶段上下文快照", execution_context)
    _check_control(task_id)

    git_url, default_branch, _ = _resolve_repo(project_id, repo_connector_id)
    target = target_branch or default_branch
    _set_stage(task_id, attempt_id, "preparing_workspace", "正在准备可写 workspace")
    ws = prepare_workspace(task_id=task_id, git_url=git_url, target_branch=target, work_branch=work_branch)
    _update_attempt(attempt_id, workspace_path=str(ws.path), branch_name=ws.branch, base_commit=ws.base_commit)
    emit_event(task_id, "workspace_ready", "workspace 已准备", {"path": str(ws.path), "branch": ws.branch, "base_commit": ws.base_commit}, attempt_id=attempt_id, stage="preparing_workspace")
    _check_control(task_id)

    def runtime_emit(event_type: str, message: str, payload: dict[str, Any]) -> None:
        emit_event(task_id, event_type, message, payload, attempt_id=attempt_id)

    runtime = CodingRuntime(
        ws.path,
        policy,
        runtime_emit,
        project_id=project_id,
        repo_connector_id=repo_connector_id or "",
    )
    summaries: list[str] = []
    agent_requirement = requirement
    if continuation_instruction:
        agent_requirement = (
            f"{requirement}\n\n"
            f"## 继续执行 / Review 修复指令\n{continuation_instruction}"
        )
    summary = ""
    diff = ""
    files: list[str] = []
    risks: list[dict[str, Any]] = []
    test_results: dict[str, Any] = {}

    for edit_round in range(1, 3):
        _set_stage(task_id, attempt_id, "editing", f"Coding Agent 正在分析并修改代码（第 {edit_round} 轮）")
        with llm_observation_context(
            scope="codetask",
            task_id=task_id,
            attempt_id=attempt_id,
            project_id=project_id,
            stage="editing",
            edit_round=edit_round,
        ):
            summary = await run_coding_agent(
                requirement=agent_requirement,
                project_context=execution_context,
                runtime=runtime,
            )
        summaries.append(f"## 第 {edit_round} 轮\n{summary}")
        combined_summary = "\n\n".join(summaries)
        _update_attempt(attempt_id, summary=combined_summary)
        _save_artifact(
            task_id,
            attempt_id,
            project_id,
            "agent_summary",
            f"Agent 第 {edit_round} 轮总结",
            summary,
            {"round": edit_round},
        )
        _check_control(task_id)

        _set_stage(task_id, attempt_id, "running_checks", "正在执行变更校验")
        diff = git_diff(ws.path)
        files = changed_files(ws.path)
        risks = _verify_changes(diff, files, policy)
        _save_artifact(
            task_id,
            attempt_id,
            project_id,
            "diff",
            f"Git diff 第 {edit_round} 轮",
            diff,
            {"round": edit_round},
        )
        test_results = {"git_status": git_status(ws.path), "changed_files": files, "risk_flags": risks, "edit_round": edit_round}
        _update_attempt(attempt_id, test_results=test_results, risk_flags=risks)
        emit_event(task_id, "diff_updated", "diff 已生成", {"changed_files": files, "risk_flags": risks, "edit_round": edit_round}, attempt_id=attempt_id, stage="running_checks")
        if diff.strip():
            summary = combined_summary
            break
        if edit_round == 1:
            emit_event(
                task_id,
                "no_change_retry",
                "Coding Agent 第一轮未产生 diff，继续同一 workspace 再跑一轮",
                {"summary": summary[:1000], "git_status": test_results.get("git_status", "")},
                attempt_id=attempt_id,
                stage="running_checks",
            )
            agent_requirement = (
                f"{requirement}\n\n"
                f"## 已审核 Plan\n{approved_plan}\n\n"
                f"## 继续执行 / Review 修复指令\n{continuation_instruction}\n\n"
                "## 续跑要求\n"
                "上一轮执行结束后 git diff 仍为空，请继续在同一个 workspace 中完成实际代码修改。"
                "不要只分析或总结；必须使用 apply_patch/write_file 产生代码变更。"
                "如果确实无法安全修改，请在最终总结中明确列出阻塞原因和已核对的文件。\n\n"
                f"## 上一轮总结\n{summary}"
            )
            _check_control(task_id)
            continue
        raise RuntimeError(
            "Coding Agent 未产生任何代码变更；"
            f"最后总结: {summary[:500]}; git_status: {test_results.get('git_status', '')}"
        )
    if any(r.get("severity") == "blocker" for r in risks):
        raise RuntimeError("变更触发 blocker 风险，已停止创建 MR")
    _check_control(task_id)

    _set_stage(task_id, attempt_id, "self_review", "正在生成改动报告")
    report_md = _build_report_markdown(task_id, requirement, summary, files, diff, test_results, risks)
    report_id, _, _ = save_report(markdown_text=report_md, project_id=project_id, thread_id=f"coding:{task_id}")
    report_url = build_report_url(report_id)
    _update_task(task_id, report_id=report_id)
    _save_artifact(task_id, attempt_id, project_id, "report", "改动报告 Markdown", report_md, {"report_id": report_id})

    mr_url = existing_mr_url
    head_commit = ""
    existing_mr_info = result.get("mr") if isinstance(result.get("mr"), dict) else {}
    review_diff = diff
    if policy.allow_push_branch and policy.allow_create_mr:
        _set_stage(task_id, attempt_id, "preparing_mr", "正在提交分支并创建 Merge Request")
        head_commit = commit_all(ws.path, f"Viktor coding task {task_id}")
        if head_commit:
            push_branch(ws.path, ws.branch)
            review_diff = git_cumulative_diff(ws.path, target)
            if existing_mr_url and existing_mr_info.get("iid"):
                mr_info = dict(existing_mr_info)
                mr_url = existing_mr_url
                emit_event(
                    task_id,
                    "mr_updated",
                    "已推送修复提交到现有 Merge Request 分支",
                    {"mr_url": mr_url, "head_commit": head_commit},
                    attempt_id=attempt_id,
                    stage="preparing_mr",
                )
            else:
                try:
                    mr = create_merge_request(
                        repo_url=git_url,
                        source_branch=ws.branch,
                        target_branch=target,
                        title=_mr_title(requirement),
                        description=_build_mr_description(
                            task_id=task_id,
                            project_id=project_id,
                            requirement=requirement,
                            approved_plan=approved_plan,
                            summary=summary,
                            files=files,
                            test_results=test_results,
                            risks=risks,
                            report_url=report_url,
                        ),
                    )
                    mr_url = str(mr.get("web_url") or "")
                    mr_info = _mr_metadata(mr)
                    _update_task(task_id, mr_url=mr_url)
                    emit_event(task_id, "mr_created", "Merge Request 已创建", {"mr_url": mr_url}, attempt_id=attempt_id, stage="preparing_mr")
                except Exception as create_exc:  # noqa: BLE001
                    # 幂等复用：覆盖「push 成功 + create 超时但 MR 实际已建」、
                    # 「409 同分支已有 open MR」等场景——按 source_branch 查 open MR。
                    existing = list_open_merge_requests_by_source_branch(
                        repo_url=git_url, source_branch=ws.branch,
                    )
                    if not existing:
                        # 没有可复用的 open MR，确属真失败，让 task 走 failed。
                        raise create_exc
                    mr = existing[0]
                    mr_url = str(mr.get("web_url") or "")
                    mr_info = _mr_metadata(mr)
                    _update_task(task_id, mr_url=mr_url)
                    emit_event(
                        task_id,
                        "mr_reused",
                        "复用已存在的 open Merge Request",
                        {"mr_url": mr_url, "reason": str(create_exc)[:300]},
                        attempt_id=attempt_id,
                        stage="preparing_mr",
                    )
        else:
            mr_info = dict(existing_mr_info)
    else:
        mr_info = {}
        emit_event(task_id, "mr_skipped", "policy 未开启自动 push/MR，已保留本地 workspace 和报告", {"report_id": report_id}, attempt_id=attempt_id)

    execution_result = {
        "changed_files": files,
        "risk_flags": risks,
        "report_id": report_id,
        "report_url": report_url,
        "mr_url": mr_url,
        "workspace_path": str(ws.path),
        "branch": ws.branch,
        "base_commit": ws.base_commit,
        "head_commit": head_commit,
        "mr": mr_info,
    }
    db = SessionLocal()
    try:
        row = db.get(CodingTaskModel, task_id)
        current_result = dict(row.result or {}) if row else {}
    finally:
        db.close()
    result = _merge_execution_result(current_result, execution_result)
    _update_attempt(attempt_id, status="completed", stage="reviewing_code", head_commit=head_commit)
    _update_task(
        task_id,
        status="reviewing_code",
        stage="reviewing_code",
        message="执行完成，Kimi 正在进行 MR Review",
        result=result,
        mr_url=mr_url,
        report_id=report_id,
    )
    emit_event(task_id, "automated_review_started", "执行完成，Kimi 正在进行 MR Review", result, attempt_id=attempt_id, stage="reviewing_code")
    if mr_url and mr_info.get("iid"):
        try:
            from core.staging_acceptance_service import mark_gitlab_pending_for_task
            mark_gitlab_pending_for_task(task_id)
            emit_event(
                task_id,
                "staging_pending",
                "MR 已标记为 Draft，等待 staging 验收",
                {"mr_url": mr_url},
                attempt_id=attempt_id,
                stage="reviewing_code",
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("[coding] 标记 staging pending 失败 task={}: {}", task_id, e)

    if mr_url and mr_info.get("iid"):
        try:
            review = await _run_automated_review(
                task_id=task_id,
                attempt_id=attempt_id,
                project_id=project_id,
                repo_url=git_url,
                mr_url=mr_url,
                mr_info=mr_info,
                requirement=requirement,
                approved_plan=approved_plan,
                summary=summary,
                files=files,
                diff=review_diff,
                test_results=test_results,
                risks=risks,
                report_url=report_url,
            )
            emit_event(
                task_id,
                "automated_review_commented",
                "Kimi Review 已评论到 MR",
                {"comment_url": review.get("comment_url"), "items": review.get("items") or []},
                attempt_id=attempt_id,
                stage="reviewing_code",
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("[coding] automated review failed task={}: {}", task_id, e)
            review = _mark_automated_review_failed(task_id, e)
            emit_event(
                task_id,
                "automated_review_failed",
                "Kimi Review 失败，已转入人工代码审核",
                review,
                attempt_id=attempt_id,
                stage="reviewing_code",
            )
    else:
        emit_event(
            task_id,
            "automated_review_skipped",
            "未创建 MR，跳过 Kimi MR Review",
            {"mr_url": mr_url},
            attempt_id=attempt_id,
            stage="reviewing_code",
        )

    db = SessionLocal()
    pending_owner_mobile = ""
    pending_owner_label = ""
    try:
        row = db.get(CodingTaskModel, task_id)
        current_result = dict(row.result or {}) if row else result
        if row:
            pending_owner_mobile = _repo_maintainer_mobile(row.project_id, row.repo_connector_id)
            pending_owner_label = f"{row.repo_connector_id} maintainer" if pending_owner_mobile and row.repo_connector_id else ""
    finally:
        db.close()
    _update_attempt(attempt_id, stage="waiting_code_review")
    _update_task(
        task_id,
        status="waiting_code_review",
        stage="waiting_code_review",
        message="执行完成，等待用户处理 Kimi Review",
        result=current_result,
        mr_url=mr_url,
        report_id=report_id,
        pending_gate="code_review",
        pending_owner_mobile=pending_owner_mobile,
        pending_owner_label=pending_owner_label,
    )
    emit_event(task_id, "waiting_code_review", "执行完成，等待用户处理 Kimi Review", current_result, attempt_id=attempt_id, stage="waiting_code_review")


def _verify_changes(diff: str, files: list[str], policy: CodingPolicy) -> list[dict[str, Any]]:
    risks: list[dict[str, Any]] = []
    if len(files) > policy.max_changed_files:
        risks.append({"severity": "blocker", "message": f"修改文件数 {len(files)} 超过上限 {policy.max_changed_files}"})
    diff_lines = diff.splitlines()
    if len(diff_lines) > policy.max_diff_lines:
        risks.append({"severity": "blocker", "message": f"diff 行数 {len(diff_lines)} 超过上限 {policy.max_diff_lines}"})
    for path in files:
        try:
            policy.check_write_path(path)
        except PermissionError as e:
            risks.append({"severity": "blocker", "file": path, "message": str(e)})
        lowered = path.lower()
        if not policy.allow_dependency_change and lowered.endswith(("package-lock.json", "pnpm-lock.yaml", "poetry.lock", "requirements.txt", "pom.xml")):
            risks.append({"severity": "warning", "file": path, "message": "依赖文件发生变化，请重点 review"})
        if not policy.allow_schema_change and ("migration" in lowered or lowered.endswith(".sql")):
            risks.append({"severity": "blocker", "file": path, "message": "policy 禁止数据库迁移/schema 变更"})
        if not policy.allow_ci_change and (lowered.startswith(".github/") or lowered.startswith(".gitlab-ci") or "/jenkinsfile" in lowered or lowered == "jenkinsfile"):
            risks.append({"severity": "blocker", "file": path, "message": "policy 禁止 CI/CD 变更"})
    # 密钥扫描分两档：
    # 1) 真·密钥特征（私钥 PEM 头 / 云厂商 access key）几乎不会误报，命中即 blocker。
    # 2) 通用「口令样式赋值」（password="..."、token=:"..."）在认证/注册/改密码功能和
    #    测试用例里遍地都是，沾词就 blocker 会误杀正常功能；降级为 warning，在报告里
    #    标出交人工 review（任务本就会停在 waiting_code_review），既不误杀又保留提醒。
    if re.search(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----", diff) or re.search(r"\bAKIA[0-9A-Z]{16}\b", diff):
        risks.append({"severity": "blocker", "message": "diff 疑似包含私钥或云厂商访问凭证"})
    elif re.search(r"(?i)(api[_-]?key|secret|password|private[_-]?key|access[_-]?token)\s*[:=]\s*['\"][^'\"]{8,}", diff):
        risks.append({"severity": "warning", "message": "diff 可能包含密钥/口令赋值，请人工确认是否为真实凭证"})
    return risks


def _build_report_markdown(
    task_id: str,
    requirement: str,
    summary: str,
    files: list[str],
    diff: str,
    test_results: dict[str, Any],
    risks: list[dict[str, Any]],
) -> str:
    file_lines = "\n".join(f"- `{f}`" for f in files) or "- 无"
    risk_lines = "\n".join(f"- [{r.get('severity')}] {r.get('file', '')} {r.get('message')}" for r in risks) or "- 未发现自动校验风险"
    diff_excerpt = diff[:12000] + ("\n... diff 已截断" if len(diff) > 12000 else "")
    return f"""# Viktor Coding Task {task_id} 改动报告

## 需求
{requirement}

## Agent 总结
{summary}

## 修改文件
{file_lines}

## 自动校验
```json
{json.dumps(test_results, ensure_ascii=False, indent=2)}
```

## 风险与 Review 建议
{risk_lines}

## Diff 摘要
```diff
{diff_excerpt}
```
"""


def _compact_markdown(value: str, *, max_chars: int) -> str:
    text = (value or "").strip()
    if not text:
        return "- 无"
    text = re.sub(r"\n{3,}", "\n\n", text)
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars].rstrip()}\n\n... 已截断，完整内容见报告"


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


def _normalize_review_items(raw: Any) -> list[dict[str, Any]]:
    items = raw if isinstance(raw, list) else []
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(items[:12], start=1):
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or item.get("summary") or "").strip()
        body = str(item.get("body") or item.get("issue") or item.get("description") or "").strip()
        suggestion = str(item.get("suggestion") or item.get("recommendation") or "").strip()
        if not title and body:
            title = body.splitlines()[0][:120]
        if not title:
            continue
        severity = str(item.get("severity") or item.get("priority") or "P2").strip().upper()
        if severity not in {"P0", "P1", "P2", "P3"}:
            severity = "P2"
        number = item.get("number") or index
        try:
            number = int(number)
        except (TypeError, ValueError):
            number = index
        line = item.get("line")
        try:
            line = int(line) if line not in (None, "") else None
        except (TypeError, ValueError):
            line = None
        normalized.append({
            "number": number,
            "title": title[:200],
            "severity": severity,
            "file": str(item.get("file") or "").strip()[:500],
            "line": line,
            "body": body[:2000],
            "suggestion": suggestion[:2000],
            "status": str(item.get("status") or "pending").strip() or "pending",
            "user_comment": str(item.get("user_comment") or "").strip(),
        })
    return normalized


def _format_review_comment(items: list[dict[str, Any]], *, task_id: str, report_url: str, project_id: str = "") -> str:
    task_url = _coding_task_url(task_id, project_id)
    lines = [
        f"## Viktor Kimi Review ({task_id})",
        "",
        f"- Reviewer: {AUTOMATED_REVIEW_MODEL}",
        f"- Report: {report_url or '无'}",
        f"- Coding Task: [{task_id}]({task_url})" if task_url else f"- Coding Task: `{task_id}`",
        "",
    ]
    if not items:
        lines.append("Kimi 未发现需要编号处理的明确问题。请继续进行人工 Code Review。")
        return "\n".join(lines).strip()
    for item in sorted(items, key=lambda x: int(x.get("number") or 0)):
        location = str(item.get("file") or "").strip()
        if item.get("line"):
            location = f"{location}:{item['line']}" if location else f"line {item['line']}"
        lines.append(f"{item['number']}. [{item.get('severity') or 'P2'}] {item.get('title')}")
        if location:
            lines.append(f"   - 位置：`{location}`")
        if item.get("body"):
            lines.append(f"   - 问题：{item['body']}")
        if item.get("suggestion"):
            lines.append(f"   - 建议：{item['suggestion']}")
        lines.append("")
    lines.append(
        f"用户可在 [Viktor Coding 工作台]({task_url}) 逐条选择：采纳、按我的意见处理、忽略。"
        if task_url else "用户可在 Viktor 前端逐条选择：采纳、按我的意见处理、忽略。"
    )
    return "\n".join(lines).strip()


def _comment_url(note: dict[str, Any], mr_url: str) -> str:
    for key in ("web_url", "url"):
        value = str(note.get(key) or "").strip()
        if value:
            return value
    note_id = note.get("id")
    return f"{mr_url}#note_{note_id}" if mr_url and note_id else mr_url


def _latest_continuation_instruction(result: dict[str, Any]) -> str:
    instructions = result.get("continuation_instructions")
    if isinstance(instructions, list):
        for item in reversed(instructions):
            if isinstance(item, dict):
                text = str(item.get("instruction") or "").strip()
                if text:
                    return text
    return ""


def _store_continuation_instruction(
    task_id: str,
    *,
    instruction: str,
    source: str,
    payload: dict[str, Any] | None = None,
) -> None:
    if not instruction.strip():
        return
    db = SessionLocal()
    try:
        row = db.get(CodingTaskModel, task_id)
        if not row:
            raise ValueError("任务不存在")
        result = dict(row.result or {})
        instructions = list(result.get("continuation_instructions") or [])
        instructions.append({
            "source": source,
            "instruction": instruction.strip(),
            "payload": payload or {},
            "created_at": datetime.now().isoformat(),
        })
        result["continuation_instructions"] = instructions
        row.result = result
        db.commit()
    finally:
        db.close()


AUTOMATED_REVIEW_SYSTEM_PROMPT = """你是资深代码审查员，正在审查 Viktor Coding Agent 创建的 GitLab MR。

只输出严格 JSON，不要输出 Markdown 或解释。JSON Schema:
{
  "items": [
    {
      "number": 1,
      "severity": "P1|P2|P3",
      "title": "一句话问题标题",
      "file": "相关文件路径，可为空",
      "line": 123,
      "body": "为什么这是问题，具体风险是什么",
      "suggestion": "建议如何修复"
    }
  ]
}

审查原则:
- 聚焦真实 bug、行为回归、遗漏验证、高风险边界条件、与需求不一致之处。
- 不要输出风格偏好、空泛建议或无法从上下文支持的问题。
- 最多 8 条，按严重程度排序。
- 如果没有明确问题，输出 {"items": []}。
"""


async def _run_automated_review(
    *,
    task_id: str,
    attempt_id: str,
    project_id: str,
    repo_url: str,
    mr_url: str,
    mr_info: dict[str, Any],
    requirement: str,
    approved_plan: str,
    summary: str,
    files: list[str],
    diff: str,
    test_results: dict[str, Any],
    risks: list[dict[str, Any]],
    report_url: str,
) -> dict[str, Any]:
    iid = mr_info.get("iid")
    if not mr_url or not iid:
        return {"status": "skipped", "reason": "missing_mr"}

    llm = create_llm(thinking=False, feature="automated_code_review", provider_order=AUTOMATED_REVIEW_PROVIDER_ORDER)
    diff_excerpt = _compact_markdown(diff, max_chars=24000)
    user_content = (
        f"## MR URL\n{mr_url}\n\n"
        f"## 需求\n{requirement}\n\n"
        f"## 已审核 Plan\n{_compact_markdown(approved_plan, max_chars=12000)}\n\n"
        f"## Agent 实现总结\n{_compact_markdown(summary, max_chars=12000)}\n\n"
        f"## 修改文件\n{json.dumps(files, ensure_ascii=False, indent=2)}\n\n"
        f"## 自动校验\n{json.dumps(test_results, ensure_ascii=False, indent=2)}\n\n"
        f"## 风险标记\n{json.dumps(risks, ensure_ascii=False, indent=2)}\n\n"
        f"## Diff\n```diff\n{diff_excerpt}\n```"
    )
    with llm_observation_context(
        scope="codetask",
        task_id=task_id,
        attempt_id=attempt_id,
        project_id=project_id,
        stage="automated_code_review",
    ):
        response = await llm.ainvoke([
            SystemMessage(content=AUTOMATED_REVIEW_SYSTEM_PROMPT),
            HumanMessage(content=user_content),
        ])
    raw = response.content if isinstance(response.content, str) else str(response.content)
    if not raw.strip():
        raise RuntimeError("Automated review returned empty response")
    parsed = _extract_json_object(raw)
    if not parsed:
        raise RuntimeError("Automated review returned non-JSON response")
    items = _normalize_review_items(parsed.get("items"))
    comment = _format_review_comment(items, task_id=task_id, report_url=report_url, project_id=project_id)
    note = create_merge_request_note(repo_url=repo_url, merge_request_iid=iid, body=comment)
    review = {
        "status": "commented",
        "provider": AUTOMATED_REVIEW_PROVIDER,
        "model": AUTOMATED_REVIEW_MODEL,
        "items": items,
        "comment_url": _comment_url(note, mr_url),
        "note_id": note.get("id"),
        "reviewed_at": datetime.now().isoformat(),
        "raw_response": raw[:12000],
    }

    db = SessionLocal()
    try:
        row = db.get(CodingTaskModel, task_id)
        if not row:
            raise ValueError("任务不存在")
        result = dict(row.result or {})
        history = list(result.get("automated_review_history") or [])
        history.append(review)
        result["automated_review_history"] = history
        result["automated_review"] = review
        row.result = result
        db.commit()
    finally:
        db.close()

    _save_artifact(
        task_id,
        attempt_id,
        project_id,
        "automated_review",
        "Automated MR Review",
        comment,
        {"review": review},
    )
    return review


def _mark_automated_review_failed(task_id: str, error: Exception) -> dict[str, Any]:
    review = {
        "status": "failed",
        "provider": AUTOMATED_REVIEW_PROVIDER,
        "model": AUTOMATED_REVIEW_MODEL,
        "items": [],
        "error": str(error),
        "reviewed_at": datetime.now().isoformat(),
    }
    db = SessionLocal()
    try:
        row = db.get(CodingTaskModel, task_id)
        if row:
            result = dict(row.result or {})
            history = list(result.get("automated_review_history") or [])
            history.append(review)
            result["automated_review_history"] = history
            result["automated_review"] = review
            row.result = result
            db.commit()
    finally:
        db.close()
    return review


def submit_automated_review_response(
    task_id: str,
    *,
    responses: list[dict[str, Any]],
    reviewer: str = "",
    additional_comment: str = "",
) -> dict[str, Any]:
    if not responses:
        raise ValueError("请至少处理一条 review item")
    db = SessionLocal()
    try:
        row = db.get(CodingTaskModel, task_id)
        if not row:
            raise ValueError("任务不存在")
        if row.status != "waiting_code_review":
            raise ValueError(f"当前状态 {row.status} 不允许提交 review 处理意见")
        result = dict(row.result or {})
        automated_review = dict(result.get("automated_review") or {})
        items = list(automated_review.get("items") or [])
        by_number = {int(item.get("number") or 0): item for item in items if isinstance(item, dict)}
        normalized: list[dict[str, Any]] = []
        actionable: list[dict[str, Any]] = []
        for raw in responses:
            if not isinstance(raw, dict):
                continue
            try:
                number = int(raw.get("number"))
            except (TypeError, ValueError):
                raise ValueError("review item number 无效")
            if number not in by_number:
                raise ValueError(f"未知 review item: {number}")
            decision = str(raw.get("decision") or "").strip()
            if decision not in {"accept", "custom", "ignore"}:
                raise ValueError("decision 必须是 accept/custom/ignore")
            comment = str(raw.get("comment") or "").strip()
            if decision == "custom" and not comment:
                raise ValueError(f"第 {number} 条选择自定义时必须填写意见")
            item = by_number[number]
            item["status"] = decision
            item["user_comment"] = comment
            entry = {
                "number": number,
                "decision": decision,
                "comment": comment,
                "item": item,
            }
            normalized.append(entry)
            if decision in {"accept", "custom"}:
                actionable.append(entry)
        if not normalized:
            raise ValueError("没有有效的 review 处理意见")

        now = datetime.now().isoformat()
        automated_review["items"] = items
        automated_review["responded_at"] = now
        automated_review["responded_by"] = reviewer
        result["automated_review"] = automated_review
        review_responses = list(result.get("review_responses") or [])
        review_responses.append({
            "reviewer": reviewer,
            "responses": normalized,
            "additional_comment": additional_comment,
            "created_at": now,
        })
        result["review_responses"] = review_responses
        row.result = result
        db.commit()
    finally:
        db.close()

    emit_event(
        task_id,
        "automated_review_response_saved",
        "已保存 Kimi review 处理意见",
        {"responses": normalized, "additional_comment": additional_comment, "reviewer": reviewer},
        stage="waiting_code_review",
    )

    if not actionable:
        emit_event(
            task_id,
            "automated_review_all_ignored",
            "所有 Kimi review 条目均被忽略，不启动修复 attempt",
            {"responses": normalized},
            stage="waiting_code_review",
        )
        return get_task(task_id) or {"task_id": task_id, "status": "waiting_code_review"}

    lines = [
        "## Kimi Review 处理意见",
        "请基于当前 MR 分支继续修复。只处理下面列出的 review 条目，不要扩大范围。",
    ]
    for entry in actionable:
        item = entry["item"]
        decision_text = "直接采纳 Kimi 建议" if entry["decision"] == "accept" else f"按用户意见处理：{entry['comment']}"
        location = str(item.get("file") or "")
        if item.get("line"):
            location = f"{location}:{item['line']}" if location else f"line {item['line']}"
        lines.append(f"\n### {entry['number']}. {item.get('title')}")
        lines.append(f"- 处理方式：{decision_text}")
        if location:
            lines.append(f"- 位置：{location}")
        if item.get("body"):
            lines.append(f"- 问题：{item['body']}")
        if item.get("suggestion"):
            lines.append(f"- Kimi 建议：{item['suggestion']}")
    if additional_comment.strip():
        lines.append(f"\n## 用户补充意见\n{additional_comment.strip()}")
    instruction = "\n".join(lines).strip()
    _store_continuation_instruction(
        task_id,
        instruction=instruction,
        source="automated_review_response",
        payload={"responses": normalized, "additional_comment": additional_comment, "reviewer": reviewer},
    )
    # Temporal 接管：续跑必须走 workflow，否则 workflow 仍卡在 waiting_code_review 的 merge gate、
    # 不知道任务已离开该态，running/queued 没人驱动也没有孤儿恢复（曾导致 task 永久卡 queued）。
    # 发 execution_continue signal → merge gate 返回 continue → continue_execution_activity 续跑。
    # signal 失败（workflow 不在/已结束/未启用）才回退旧直派路径。
    if trigger.signal_coding_task_sync(task_id, "execution_continue", ""):
        return get_task(task_id) or {"task_id": task_id, "status": "running"}
    emit_event(task_id, "continue_requested", "已提交 review 处理意见，准备启动修复 attempt", {"instruction": instruction}, stage="queued")
    return start_execution(task_id)


def _build_mr_description(
    *,
    task_id: str,
    project_id: str = "",
    requirement: str,
    approved_plan: str,
    summary: str,
    files: list[str],
    test_results: dict[str, Any],
    risks: list[dict[str, Any]],
    report_url: str,
) -> str:
    file_lines = "\n".join(f"- `{f}`" for f in files) or "- 无"
    risk_lines = "\n".join(
        f"- [{r.get('severity') or 'unknown'}] {r.get('file') or ''} {r.get('message') or ''}".strip()
        for r in risks
    ) or "- 未发现自动校验风险"
    git_status = str(test_results.get("git_status") or "").strip()
    verification_lines = [
        f"- 修改文件数：{len(files)}",
        f"- 执行轮次：{test_results.get('edit_round') or '未知'}",
        f"- 自动风险数：{len(risks)}",
    ]
    if git_status:
        verification_lines.append("\n```text\n" + _compact_markdown(git_status, max_chars=1200) + "\n```")
    report_line = f"- 完整报告：{report_url}" if report_url else "- 完整报告：未生成链接"
    task_url = _coding_task_url(task_id, project_id)
    task_line = f"- Coding Task：[{task_id}]({task_url})" if task_url else f"- Coding Task：`{task_id}`"

    return f"""# Viktor Coding Task {task_id}

## 为什么改
{_compact_markdown(requirement, max_chars=1600)}

## 已审核 Plan 摘要
{_compact_markdown(approved_plan, max_chars=2200)}

## 本次实际改动
{_compact_markdown(summary, max_chars=2600)}

## 修改文件
{file_lines}

## 自动校验
{chr(10).join(verification_lines)}

## 风险与 Review 建议
{risk_lines}

## 链接
{report_line}
{task_line}
"""


def _mr_title(requirement: str) -> str:
    first = next((line.strip() for line in requirement.splitlines() if line.strip()), "Viktor coding task")
    return first[:80]


def _save_artifact(
    task_id: str,
    attempt_id: str,
    project_id: str,
    artifact_type: str,
    title: str,
    content: str,
    payload: dict[str, Any] | None = None,
) -> None:
    db = SessionLocal()
    try:
        db.add(CodingArtifactModel(
            artifact_id=_new_id("cart"),
            task_id=task_id,
            attempt_id=attempt_id,
            project_id=project_id,
            artifact_type=artifact_type,
            title=title,
            content=content,
            payload=payload or {},
        ))
        db.commit()
    finally:
        db.close()


def list_tasks(
    project_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
    pending_for_mobile: str | None = None,
    created_by_mobile: str | None = None,
) -> dict[str, Any]:
    db = SessionLocal()
    try:
        query = db.query(CodingTaskModel)
        if project_id:
            query = query.filter(CodingTaskModel.project_id == project_id)
        if pending_for_mobile:
            query = query.filter(CodingTaskModel.pending_owner_mobile == pending_for_mobile.strip())
        if created_by_mobile:
            query = query.filter(CodingTaskModel.created_by_mobile == created_by_mobile.strip())
        total = query.count()
        rows = query.order_by(CodingTaskModel.created_at.desc()).offset(offset).limit(limit).all()
        return {"items": [_task_to_dict(row) for row in rows], "total": total, "limit": limit, "offset": offset}
    finally:
        db.close()


def get_task(task_id: str) -> dict[str, Any] | None:
    db = SessionLocal()
    try:
        row = db.get(CodingTaskModel, task_id)
        if not row:
            return None
        data = _task_to_dict(row)
        events = (
            db.query(CodingEventModel)
            .filter(CodingEventModel.task_id == task_id)
            .order_by(CodingEventModel.seq)
            .all()
        )
        data["events"] = [_event_to_dict(event) for event in events]
        data["attempts"] = list_attempts(task_id)["items"]
        return data
    finally:
        db.close()


def list_events(task_id: str, after_seq: int = 0, limit: int = 200) -> dict[str, Any]:
    db = SessionLocal()
    try:
        rows = (
            db.query(CodingEventModel)
            .filter(CodingEventModel.task_id == task_id, CodingEventModel.seq > after_seq)
            .order_by(CodingEventModel.seq)
            .limit(limit)
            .all()
        )
        return {"items": [_event_to_dict(row) for row in rows], "total": len(rows)}
    finally:
        db.close()


def list_attempts(task_id: str) -> dict[str, Any]:
    db = SessionLocal()
    try:
        rows = (
            db.query(CodingAttemptModel)
            .filter(CodingAttemptModel.task_id == task_id)
            .order_by(CodingAttemptModel.created_at)
            .all()
        )
        return {"items": [_attempt_to_dict(row) for row in rows], "total": len(rows)}
    finally:
        db.close()


def get_latest_diff(task_id: str) -> dict[str, Any]:
    db = SessionLocal()
    try:
        attempt = (
            db.query(CodingAttemptModel)
            .filter(CodingAttemptModel.task_id == task_id)
            .order_by(CodingAttemptModel.created_at.desc())
            .first()
        )
        if not attempt:
            return {"diff": "", "changed_files": []}
        artifact = (
            db.query(CodingArtifactModel)
            .filter(CodingArtifactModel.task_id == task_id, CodingArtifactModel.artifact_type == "diff")
            .order_by(CodingArtifactModel.created_at.desc())
            .first()
        )
        artifact_diff = artifact.content if artifact else ""
        artifact_files: list[str] = []
        if attempt.test_results and isinstance(attempt.test_results.get("changed_files"), list):
            artifact_files = [str(item) for item in attempt.test_results.get("changed_files", [])]
        if attempt.workspace_path:
            try:
                from pathlib import Path
                diff = git_diff(Path(attempt.workspace_path))
                files = changed_files(Path(attempt.workspace_path))
                return {"diff": diff or artifact_diff, "changed_files": files or artifact_files}
            except Exception as e:  # noqa: BLE001
                return {"diff": artifact_diff, "changed_files": artifact_files, "error": str(e)}
        return {"diff": artifact_diff, "changed_files": artifact_files}
    finally:
        db.close()


def update_control(task_id: str, **control_updates: Any) -> dict[str, Any]:
    db = SessionLocal()
    try:
        row = db.get(CodingTaskModel, task_id)
        if not row:
            raise ValueError("任务不存在")
        control = dict(row.control or {})
        control.update(control_updates)
        row.control = control
        if control_updates.get("cancel_requested") and row.status not in FINAL_STATUSES:
            row.status = "cancelling"
            row.message = "用户请求取消，等待安全点停止"
        if control_updates.get("pause_requested") and row.status not in FINAL_STATUSES:
            row.message = "用户请求暂停，等待安全点暂停"
        db.commit()
        emit_event(task_id, "control_updated", "控制指令已更新", control)
        return _task_to_dict(row)
    finally:
        db.close()


def mark_rate_limited(task_id: str, mode: str, message: str) -> None:
    """把因 LLM 限流失败的 attempt 标成可重试的 rate_limited 中间态（非终态）。

    由 Job runner 在判定为限流类失败时调用；CodingTaskWorkflow 会识别该态并退避后重派。
    记 result['rate_limit_mode'] 供 workflow 决定重派 planning 还是 execution。
    已是终态的不覆盖（避免与并发的 cancel/complete 抢写）。
    """
    db = SessionLocal()
    try:
        row = db.get(CodingTaskModel, task_id)
        if not row or row.status in FINAL_STATUSES:
            return
        result = dict(row.result or {})
        result["rate_limit_mode"] = mode if mode in ("planning", "execution") else "execution"
        row.result = result
        row.status = "rate_limited"
        row.stage = "rate_limited"
        row.message = message
        db.commit()
    finally:
        db.close()
    emit_event(task_id, "rate_limited", message, {"mode": mode}, stage="rate_limited")


def requeue_after_rate_limit(task_id: str) -> str:
    """限流退避结束：把 rate_limited 翻回可执行态，返回应重派的 mode（planning/execution）。

    planning → status=planning；execution → status=running/queued。两者都在 _ACTIVE_COMPUTE 里，
    workflow 重派 Job 后用 _await_status_leaves 跟踪 + 孤儿恢复。
    """
    mode = "execution"
    db = SessionLocal()
    try:
        row = db.get(CodingTaskModel, task_id)
        if not row:
            return mode
        mode = str((row.result or {}).get("rate_limit_mode") or "execution")
        if mode not in ("planning", "execution"):
            mode = "execution"
        if row.status == "rate_limited":
            if mode == "planning":
                row.status = "planning"
                row.stage = "planning"
            else:
                row.status = "running"
                row.stage = "queued"
            row.message = "LLM 限流退避结束，重新执行 attempt"
            db.commit()
    finally:
        db.close()
    emit_event(task_id, "rate_limit_retry", "LLM 限流退避结束，重新执行 attempt", {"mode": mode}, stage="queued")
    return mode


def resume_coding_task(task_id: str) -> dict[str, Any]:
    task = update_control(task_id, pause_requested=False, cancel_requested=False)
    if task.get("status") == "paused":
        _update_task(task_id, status="running", stage="queued", message="任务已恢复，准备重新执行 attempt")
        emit_event(task_id, "resumed", "任务已恢复，准备重新执行 attempt", {})
        _dispatch_coding_job(task_id, "execution")
        task = get_task(task_id) or task
    return task


def append_message(task_id: str, message: str) -> None:
    emit_event(task_id, "user_message", message, {"message": message})


def review_plan(task_id: str, *, decision: str, comment: str = "", reviewer: str = "") -> dict[str, Any]:
    if decision not in {"approved", "rejected"}:
        raise ValueError("decision must be approved or rejected")
    db = SessionLocal()
    try:
        row = db.get(CodingTaskModel, task_id)
        if not row:
            raise ValueError("任务不存在")
        if row.status not in {"waiting_plan_review", "planning", "created", "plan_approved"}:
            raise ValueError(f"当前状态 {row.status} 不允许审核 Plan")
        if decision == "approved" and row.status != "waiting_plan_review":
            raise ValueError("Plan 尚未生成完成，不能通过审核")
        result = dict(row.result or {})
        plan_markdown = str(result.get("plan_markdown") or "")
        if not plan_markdown.strip():
            plan_markdown = _get_latest_artifact_content(task_id, "plan")
        if decision == "rejected":
            row.status = "plan_rejected"
            row.stage = "plan_rejected"
            row.message = "Plan 已驳回"
            _clear_pending_owner(row)
            result["plan_review"] = {
                "decision": "rejected",
                "comment": comment,
                "reviewer": reviewer,
                "reviewed_at": datetime.now().isoformat(),
            }
            row.result = result
            db.commit()
            emit_event(task_id, "plan_rejected", "Plan 已驳回", {"comment": comment, "reviewer": reviewer}, stage="plan_rejected")
        else:
            if not plan_markdown.strip():
                raise ValueError("任务尚未生成 Plan")
            row.status = "plan_approved"
            row.stage = "plan_approved"
            row.message = "Plan 已通过，等待启动执行"
            _set_repo_maintainer_pending_owner(row, gate="execution_start")
            result["approved_plan_markdown"] = plan_markdown
            result["approved_at"] = datetime.now().isoformat()
            result["plan_review"] = {
                "decision": "approved",
                "comment": comment,
                "reviewer": reviewer,
                "reviewed_at": datetime.now().isoformat(),
            }
            row.result = result
            db.commit()
            _save_artifact(
                task_id,
                "",
                row.project_id,
                "plan_review",
                "Plan 审核记录",
                comment or "Plan approved",
                {"decision": "approved", "reviewer": reviewer},
            )
            emit_event(task_id, "plan_approved", "Plan 已通过，等待启动执行", {"comment": comment, "reviewer": reviewer}, stage="plan_approved")
    finally:
        db.close()
    task = get_task(task_id)
    if not task:
        raise ValueError("任务不存在")
    return task


def start_execution(task_id: str) -> dict[str, Any]:
    db = SessionLocal()
    try:
        row = db.get(CodingTaskModel, task_id)
        if not row:
            raise ValueError("任务不存在")
        if row.status in EXECUTION_ACTIVE_STATUSES:
            raise ValueError("任务已在执行中")
        if row.status not in EXECUTION_STARTABLE_STATUSES:
            raise ValueError(f"当前状态 {row.status} 不允许启动执行")
        result = dict(row.result or {})
        approved_plan = _approved_plan_markdown(result)
        if not approved_plan and row.status == "plan_approved":
            approved_plan = _get_latest_artifact_content(task_id, "plan").strip()
            if approved_plan:
                result["approved_plan_markdown"] = approved_plan
                result["approved_at"] = result.get("approved_at") or datetime.now().isoformat()
                row.result = result
        if not approved_plan:
            raise ValueError("任务缺少已审核 Plan，无法启动执行")
        row.status = "running"
        row.stage = "queued"
        row.message = "执行已启动，等待后台 worker"
        _clear_pending_owner(row)
        db.commit()
    finally:
        db.close()
    emit_event(task_id, "execution_start_requested", "执行已启动", {}, stage="queued")
    _dispatch_coding_job(task_id, "execution")
    return get_task(task_id) or {"task_id": task_id, "status": "running"}


def continue_execution(task_id: str, comment: str = "") -> dict[str, Any]:
    task = get_task(task_id)
    if not task:
        raise ValueError("任务不存在")
    status = str(task.get("status") or "")
    if status in EXECUTION_ACTIVE_STATUSES:
        raise ValueError("任务已在执行中")
    if status not in EXECUTION_STARTABLE_STATUSES:
        raise ValueError(f"当前状态 {status} 不允许继续执行")
    if comment.strip():
        instruction = (
            "## 用户继续执行指令\n"
            f"{comment.strip()}\n\n"
            "请基于当前 MR 分支继续修改、提交并更新同一个 MR。"
        )
        _store_continuation_instruction(
            task_id,
            instruction=instruction,
            source="manual_continue",
            payload={"comment": comment.strip()},
        )
        append_message(task_id, comment.strip())
    emit_event(task_id, "continue_requested", "继续执行请求已记录，准备启动新 attempt", {"comment": comment})
    return start_execution(task_id)


def complete_code_review(
    task_id: str,
    *,
    reviewer: str = "",
    comment: str = "",
    merge_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    db = SessionLocal()
    try:
        row = db.get(CodingTaskModel, task_id)
        if not row:
            raise ValueError("任务不存在")
        if row.status == "completed":
            return _task_to_dict(row)
        if row.status != "waiting_code_review":
            raise ValueError(f"当前状态 {row.status} 不允许完成代码审核")
        now = datetime.now().isoformat()
        result = dict(row.result or {})
        review = dict(result.get("code_review") or {})
        review_status = "merged" if merge_payload else "completed"
        completion_message = "MR 已合并，任务完成" if merge_payload else "代码审核已完成，任务完成"
        review.update({
            "status": review_status,
            "reviewer": reviewer,
            "comment": comment,
            "completed_at": now,
        })
        if merge_payload:
            review["merge"] = merge_payload
        result["code_review"] = review
        row.result = result
        row.status = "completed"
        row.stage = "completed"
        row.message = completion_message
        _clear_pending_owner(row)
        db.commit()
        task = _task_to_dict(row)
    finally:
        db.close()
    emit_event(
        task_id,
        "code_review_completed",
        completion_message,
        {"reviewer": reviewer, "comment": comment, "merge": merge_payload or {}},
        stage="completed",
    )
    return task


def close_code_review_by_mr_closed(
    task_id: str,
    *,
    reviewer: str = "",
    comment: str = "",
    close_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """MR 被关闭（未合并）时，把 waiting_code_review 任务取消。

    与 complete_code_review 对称：后者处理 merge→completed，本函数处理 close→cancelled。
    """
    db = SessionLocal()
    try:
        row = db.get(CodingTaskModel, task_id)
        if not row:
            raise ValueError("任务不存在")
        if row.status == "cancelled":
            return _task_to_dict(row)
        if row.status != "waiting_code_review":
            raise ValueError(f"当前状态 {row.status} 不允许因 MR 关闭而取消")
        now = datetime.now().isoformat()
        result = dict(row.result or {})
        review = dict(result.get("code_review") or {})
        review.update({
            "status": "mr_closed",
            "reviewer": reviewer,
            "comment": comment,
            "closed_at": now,
        })
        if close_payload:
            review["close"] = close_payload
        result["code_review"] = review
        row.result = result
        row.status = "cancelled"
        row.stage = "cancelled"
        row.message = "MR 已关闭（未合并），任务取消"
        _clear_pending_owner(row)
        db.commit()
        task = _task_to_dict(row)
    finally:
        db.close()
    emit_event(
        task_id,
        "code_review_cancelled",
        "MR 已关闭（未合并），任务取消",
        {"reviewer": reviewer, "comment": comment, "close": close_payload or {}},
        stage="cancelled",
    )
    return task


def complete_code_review_by_merge_request(payload: dict[str, Any]) -> dict[str, Any]:
    attrs = payload.get("object_attributes") if isinstance(payload.get("object_attributes"), dict) else {}
    action = str(attrs.get("action") or "").lower()
    state = str(attrs.get("state") or "").lower()
    is_merge = action == "merge" or state == "merged"
    is_close = action == "close" or state == "closed"
    if not is_merge and not is_close:
        return {"matched": 0, "completed": 0, "cancelled": 0, "ignored": True}

    mr_url = str(attrs.get("url") or attrs.get("web_url") or "").strip()
    mr_iid = attrs.get("iid")
    project_id = attrs.get("target_project_id") or attrs.get("source_project_id") or attrs.get("project_id")
    source_branch = str(attrs.get("source_branch") or "").strip()

    matched: list[str] = []
    db = SessionLocal()
    try:
        rows = (
            db.query(CodingTaskModel)
            .filter(CodingTaskModel.status == "waiting_code_review")
            .order_by(CodingTaskModel.updated_at.desc())
            .limit(500)
            .all()
        )
        for row in rows:
            result = row.result or {}
            mr = result.get("mr") if isinstance(result.get("mr"), dict) else {}
            row_url = str(row.mr_url or mr.get("web_url") or "").strip()
            row_iid = mr.get("iid")
            row_project_id = mr.get("project_id")
            url_match = bool(mr_url and row_url and mr_url == row_url)
            iid_match = bool(mr_iid and project_id and row_iid == mr_iid and row_project_id == project_id)
            branch_match = bool(source_branch and source_branch == row.work_branch)
            if url_match or iid_match or branch_match:
                matched.append(row.task_id)
    finally:
        db.close()

    completed = 0
    cancelled = 0
    mr_payload = {
        "action": action,
        "state": state,
        "url": mr_url,
        "iid": mr_iid,
        "project_id": project_id,
        "source_branch": source_branch,
        "merge_commit_sha": attrs.get("merge_commit_sha"),
        "merged_at": attrs.get("updated_at"),
        "closed_at": attrs.get("updated_at"),
    }
    for task_id in matched:
        try:
            if is_merge:
                complete_code_review(
                    task_id,
                    reviewer=str(attrs.get("merge_user_id") or ""),
                    comment="GitLab Merge Request webhook",
                    merge_payload=mr_payload,
                )
                completed += 1
            else:
                actor = payload.get("user") if isinstance(payload.get("user"), dict) else {}
                close_code_review_by_mr_closed(
                    task_id,
                    reviewer=str(actor.get("username") or actor.get("name") or ""),
                    comment="GitLab Merge Request webhook",
                    close_payload=mr_payload,
                )
                cancelled += 1
        except ValueError:
            continue
    issue_result: dict[str, Any] = {}
    try:
        if is_merge:
            from core.issue_intake_service import handle_merge_request_merged

            issue_result = handle_merge_request_merged(payload, matched)
        else:
            from core.issue_intake_service import handle_merge_request_closed

            issue_result = handle_merge_request_closed(payload, matched)
    except Exception as e:  # noqa: BLE001
        logger.exception("[coding] issue intake mr sync failed: {}", e)
        issue_result = {"issue_intake_error": str(e)}
    return {
        "matched": len(matched),
        "completed": completed,
        "cancelled": cancelled,
        "task_ids": matched,
        **issue_result,
    }
