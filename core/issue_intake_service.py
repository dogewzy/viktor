"""GitLab Issue Intake：需求/Bug -> GitLab issue -> Coding Task -> MR 闭环。"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
import time
import uuid
from datetime import datetime
from typing import Any
from urllib.parse import quote

from loguru import logger
from sqlalchemy.exc import IntegrityError

from core.database import SessionLocal
from core.dingtalk_notifier import send_dingtalk_markdown_sync
from core.models import (
    CodingTaskModel,
    IssueIntakeConfigModel,
    IssueIntakeEventModel,
    IssueIntakeLinkModel,
    IssueIntakeTargetModel,
)
from core.issue_router import route_issue
from core.registry import registry
from core.report_store import build_report_url
from gitlab.issue_service import (
    add_issue_labels,
    close_issue,
    create_issue,
    create_issue_note,
    list_issues,
)
from gitlab.service import GitLabClient
from settings import gitlab_config, report_config, temporal_config, watchdog_config


FINAL_LINK_STATUSES = {"failed", "issue_closed", "completed"}
ACTIVE_LINK_STATUSES = {
    "created",
    "issue_created",
    "coding_task_created",
    "plan_waiting",
    "running",
    "mr_created",
}

# 多个仓库的 watcher 线程 + MR webhook 会并发读改 link.result["coding_tasks"]，加锁串行化。
_RESULT_LOCK = threading.Lock()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _coding_task_url(task_id: str, project_id: str = "") -> str:
    task_id = str(task_id or "").strip()
    if not task_id:
        return ""
    params = [f"task_id={quote(task_id, safe='')}"]
    project_id = str(project_id or "").strip()
    if project_id:
        params.append(f"project_id={quote(project_id, safe='')}")
    return f"{report_config.base_url.rstrip('/')}/coding?{'&'.join(params)}"


def _blueprint_review_url(link_id: str, project_id: str = "") -> str:
    """工作台「需求接入」蓝图确认链接。蓝图阶段尚未建 task，故用 link_id 定位。"""
    link_id = str(link_id or "").strip()
    if not link_id:
        return ""
    params = [f"link_id={quote(link_id, safe='')}"]
    project_id = str(project_id or "").strip()
    if project_id:
        params.append(f"project_id={quote(project_id, safe='')}")
    return f"{report_config.base_url.rstrip('/')}/issue-intake?{'&'.join(params)}"


def _coding_task_markdown(task_id: str, project_id: str = "") -> str:
    task_id = str(task_id or "").strip()
    if not task_id:
        return "`-`"
    url = _coding_task_url(task_id, project_id)
    return f"[{task_id}]({url})" if url else f"`{task_id}`"


def _requirement_digest(link: Any, *, max_chars: int = 200) -> str:
    """从 link 取「原始需求」摘要：标题 + 截断正文，剥掉附件块与 metadata 注释。"""
    title = str(getattr(link, "title", "") or "").strip()
    desc = str(getattr(link, "description", "") or "")
    # description 是整理后的 Markdown，含 `## 附件` 与 `<!-- viktor-issue-intake ... -->`，
    # 通知里只要正文摘要，截掉这两段。
    for marker in ("\n## 附件", "<!-- viktor-issue-intake"):
        idx = desc.find(marker)
        if idx >= 0:
            desc = desc[:idx]
    desc = desc.strip()
    if len(desc) > max_chars:
        desc = desc[:max_chars].rstrip() + "…"
    lines = []
    if title:
        lines.append(f"- 需求: {title}")
    if desc:
        # 引用块逐行加 `> `，避免多行在钉钉 markdown 里塌成一行。
        quoted = "\n".join(f"> {ln}" for ln in desc.splitlines() if ln.strip())
        if quoted:
            lines.append(quoted)
    return "\n".join(lines)


def _build_link_notification_text(link: Any, title: str, body: str) -> str:
    tracking_ref = (
        _coding_task_markdown(link.coding_task_id, link.project_id)
        if getattr(link, "coding_task_id", "") else f"`{link.link_id}`"
    )
    digest = _requirement_digest(link)
    digest_block = f"{digest}\n" if digest else ""
    return (
        f"## {title}\n\n"
        f"- 项目: {link.project_id}\n"
        f"- Repo: {link.repo_connector_id or '-'}\n"
        f"- 跟踪 ID: {tracking_ref}\n"
        f"- Issue: {link.issue_url}\n"
        f"{digest_block}\n"
        f"{body}"
    )


def _target_to_dict(row: IssueIntakeTargetModel) -> dict[str, Any]:
    return {
        "project_id": row.project_id,
        "repo_connector_id": row.repo_connector_id,
        "issue_project_url": row.issue_project_url,
        "labels": row.labels or [],
        "enabled": bool(row.enabled),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _config_to_dict(row: IssueIntakeConfigModel, targets: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "project_id": row.project_id,
        "issue_project_url": row.issue_project_url,
        "default_repo_connector_id": row.default_repo_connector_id,
        "default_labels": row.default_labels or [],
        "targets": targets or [],
        "submit_token": row.submit_token,
        "notification": row.notification or {},
        "assignee_mobiles": row.assignee_mobiles or {},
        "scan_interval_sec": row.scan_interval_sec,
        "enabled": bool(row.enabled),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _link_to_dict(row: IssueIntakeLinkModel) -> dict[str, Any]:
    report_url = build_report_url(row.report_id) if row.report_id else ""
    result = _as_dict(row.result)
    return {
        "id": row.link_id,
        "link_id": row.link_id,
        "project_id": row.project_id,
        "repo_connector_id": row.repo_connector_id,
        "source": row.source,
        "kind": row.kind,
        "reporter": row.reporter,
        "title": row.title,
        "description": row.description,
        "status": row.status,
        "stage": row.stage,
        "message": row.message,
        "gitlab_base_url": row.gitlab_base_url,
        "gitlab_project_path": row.gitlab_project_path,
        "gitlab_project_id": row.gitlab_project_id,
        "issue_id": row.issue_id,
        "issue_iid": row.issue_iid,
        "issue_url": row.issue_url,
        "issue_state": row.issue_state,
        "issue_labels": row.issue_labels or [],
        "issue_payload": row.issue_payload or {},
        "assignees": row.assignees or [],
        "coding_task_id": row.coding_task_id,
        "coding_task_url": _coding_task_url(row.coding_task_id, row.project_id) if row.coding_task_id else "",
        "mr_url": row.mr_url,
        "report_id": row.report_id,
        "report_url": report_url,
        "dedupe_key": row.dedupe_key,
        "last_error": row.last_error,
        "reporter_mobile": str(result.get("reporter_mobile") or ""),
        "result": result,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _event_to_dict(row: IssueIntakeEventModel) -> dict[str, Any]:
    return {
        "id": row.id,
        "link_id": row.link_id,
        "seq": row.seq,
        "event_type": row.event_type,
        "stage": row.stage,
        "message": row.message,
        "payload": row.payload or {},
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _project_config(project_id: str) -> IssueIntakeConfigModel | None:
    db = SessionLocal()
    try:
        return db.get(IssueIntakeConfigModel, project_id)
    finally:
        db.close()


def _list_target_models(project_id: str, *, enabled_only: bool = False) -> list[IssueIntakeTargetModel]:
    db = SessionLocal()
    try:
        query = db.query(IssueIntakeTargetModel).filter(IssueIntakeTargetModel.project_id == project_id)
        if enabled_only:
            query = query.filter(IssueIntakeTargetModel.enabled == 1)
        return query.order_by(IssueIntakeTargetModel.repo_connector_id.asc()).all()
    finally:
        db.close()


def get_issue_intake_config(project_id: str) -> dict[str, Any] | None:
    row = _project_config(project_id)
    if not row:
        return None
    targets = [_target_to_dict(item) for item in _list_target_models(project_id)]
    return _config_to_dict(row, targets)


def upsert_issue_intake_config(project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not registry.get_project(project_id):
        raise ValueError(f"项目 {project_id} 不存在")
    issue_project_url = str(payload.get("issue_project_url") or "").strip()
    default_repo_connector_id = str(payload.get("default_repo_connector_id") or "").strip()
    if default_repo_connector_id and not registry.get_repository_connector(project_id, default_repo_connector_id):
        raise ValueError(f"Repository Connector {default_repo_connector_id} 不存在")
    if not issue_project_url:
        repo = _resolve_repo(project_id, default_repo_connector_id)
        issue_project_url = repo.git_url
    default_labels = _normalize_labels(payload.get("default_labels") or [])
    if "viktor:auto" not in default_labels:
        default_labels.append("viktor:auto")
    submit_token = str(payload.get("submit_token") or "").strip() or secrets.token_urlsafe(24)
    scan_interval_sec = int(payload.get("scan_interval_sec") or 300)
    target_payloads = payload.get("targets")
    normalized_targets = _normalize_target_payloads(
        project_id=project_id,
        targets=target_payloads,
        default_repo_connector_id=default_repo_connector_id,
        legacy_issue_project_url=issue_project_url,
    )
    db = SessionLocal()
    try:
        row = db.get(IssueIntakeConfigModel, project_id)
        if row:
            row.issue_project_url = issue_project_url
            row.default_repo_connector_id = default_repo_connector_id
            row.default_labels = default_labels
            row.submit_token = submit_token
            row.notification = _as_dict(payload.get("notification"))
            row.assignee_mobiles = _as_dict(payload.get("assignee_mobiles"))
            row.scan_interval_sec = max(60, scan_interval_sec)
            row.enabled = 1 if payload.get("enabled", True) else 0
        else:
            row = IssueIntakeConfigModel(
                project_id=project_id,
                issue_project_url=issue_project_url,
                default_repo_connector_id=default_repo_connector_id,
                default_labels=default_labels,
                submit_token=submit_token,
                notification=_as_dict(payload.get("notification")),
                assignee_mobiles=_as_dict(payload.get("assignee_mobiles")),
                scan_interval_sec=max(60, scan_interval_sec),
                enabled=1 if payload.get("enabled", True) else 0,
            )
            db.add(row)
        if target_payloads is not None:
            db.query(IssueIntakeTargetModel).filter(IssueIntakeTargetModel.project_id == project_id).delete()
            for item in normalized_targets:
                db.add(IssueIntakeTargetModel(**item))
        db.commit()
        db.refresh(row)
        targets = (
            db.query(IssueIntakeTargetModel)
            .filter(IssueIntakeTargetModel.project_id == project_id)
            .order_by(IssueIntakeTargetModel.repo_connector_id.asc())
            .all()
        )
        return _config_to_dict(row, [_target_to_dict(item) for item in targets])
    finally:
        db.close()


def list_issue_intake_links(
    *,
    project_id: str | None = None,
    status: str | None = None,
    q: str | None = None,
    reporter_mobile: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    db = SessionLocal()
    try:
        query = db.query(IssueIntakeLinkModel)
        if project_id:
            query = query.filter(IssueIntakeLinkModel.project_id == project_id)
        if status:
            query = query.filter(IssueIntakeLinkModel.status == status)
        if q:
            like = f"%{q}%"
            query = query.filter(
                IssueIntakeLinkModel.title.like(like)
                | IssueIntakeLinkModel.issue_url.like(like)
                | IssueIntakeLinkModel.coding_task_id.like(like)
            )
        if reporter_mobile:
            query = query.filter(IssueIntakeLinkModel.result["reporter_mobile"].as_string() == reporter_mobile.strip())
        total = query.count()
        rows = (
            query.order_by(IssueIntakeLinkModel.updated_at.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )
        return {"items": [_link_to_dict(row) for row in rows], "total": total, "limit": limit, "offset": offset}
    finally:
        db.close()


def get_issue_intake_link(link_id: str) -> dict[str, Any] | None:
    db = SessionLocal()
    try:
        row = db.get(IssueIntakeLinkModel, link_id)
        return _link_to_dict(row) if row else None
    finally:
        db.close()


def list_issue_intake_events(link_id: str, *, after_seq: int = 0, limit: int = 200) -> dict[str, Any]:
    db = SessionLocal()
    try:
        rows = (
            db.query(IssueIntakeEventModel)
            .filter(IssueIntakeEventModel.link_id == link_id, IssueIntakeEventModel.seq > after_seq)
            .order_by(IssueIntakeEventModel.seq.asc())
            .limit(limit)
            .all()
        )
        return {"items": [_event_to_dict(row) for row in rows], "total": len(rows), "limit": limit, "offset": 0}
    finally:
        db.close()


def emit_issue_event(
    link_id: str,
    event_type: str,
    message: str,
    payload: dict[str, Any] | None = None,
    *,
    stage: str = "",
) -> None:
    db = SessionLocal()
    try:
        last = (
            db.query(IssueIntakeEventModel.seq)
            .filter(IssueIntakeEventModel.link_id == link_id)
            .order_by(IssueIntakeEventModel.seq.desc())
            .first()
        )
        seq = int(last[0]) + 1 if last else 1
        db.add(IssueIntakeEventModel(
            link_id=link_id,
            seq=seq,
            event_type=event_type,
            stage=stage,
            message=message,
            payload=payload or {},
        ))
        db.commit()
    finally:
        db.close()


def generate_local_agent_skill(
    project_id: str,
    *,
    kind: str = "feature",
    repo_connector_id: str = "",
    reporter_display_name: str = "",
    reporter_mobile: str = "",
) -> dict[str, str]:
    cfg = get_issue_intake_config(project_id)
    if not cfg:
        raise ValueError("项目尚未配置 Issue Intake")
    submit_url = f"{report_config.base_url.rstrip('/')}/api/v1/issue-intake/submit"
    kind_label = "产品需求" if kind == "feature" else "测试 Bug"
    template = _feature_template() if kind == "feature" else _bug_template()
    reporter_display_name = str(reporter_display_name or "").strip() or "提交人显示名"
    reporter_mobile = str(reporter_mobile or "").strip() or "提交人钉钉手机号"
    reporter_note = (
        "默认已写入复制 Skill 的当前登录用户；只有替别人提交时才需要替换提交人信息。"
        if reporter_mobile != "提交人钉钉手机号"
        else "当前登录用户没有手机号，提交前必须补充真实钉钉手机号。"
    )

    repo_connector_id = (repo_connector_id or "").strip()
    if repo_connector_id:
        target = _resolve_issue_target(project_id, repo_connector_id)
        repo_connector_id = target["repo_connector_id"]
        skill_name = re.sub(r"[^a-z0-9-]+", "-", f"viktor-{project_id}-{repo_connector_id}-{kind}-issue-intake".lower()).strip("-")
        repo_config_line = f"\n- repo_connector_id: `{repo_connector_id}`（已固定到该仓库）"
        repo_body_line = f'\n    "repo_connector_id": "{repo_connector_id}",'
        routing_note = ""
        filename = f"viktor-{project_id}-{repo_connector_id}-{kind}-issue-skill.md"
    else:
        skill_name = re.sub(r"[^a-z0-9-]+", "-", f"viktor-{project_id}-{kind}-issue-intake".lower()).strip("-")
        repo_config_line = ""
        repo_body_line = ""
        routing_note = "\n   注意：不要指定目标仓库，Viktor 会根据需求自动路由到一个或多个代码仓库（前后端联动时会路由到多个）。"
        filename = f"viktor-{project_id}-{kind}-issue-skill.md"

    text = f"""---
name: {skill_name}
description: 将{kind_label}整理并提交到 Viktor Issue Intake，由 Viktor 服务端创建 GitLab issue、自动路由代码仓库并创建 Coding Task；当用户要求上报{kind_label}、同步到 GitLab issue、进入研发闭环时使用。
---

# Viktor {kind_label} GitLab Issue 录入 Skill

当用户要求把{kind_label}提交给 Viktor/GitLab/研发闭环时，使用本流程。

## 固定配置
- Viktor submit endpoint: `{submit_url}`
- project_id: `{project_id}`
- submit_token: `{cfg['submit_token']}`{repo_config_line}
- kind: `{kind}`

## 工作流
1. 读取用户给出的草稿、截图说明、复现信息或本地 agent 产出。
2. 按下方模板整理为 Markdown。不要编造业务事实；缺少必填项时先追问。
3. `reporter_display_name` 与 `reporter_mobile` 使用下方 API 调用中的默认值；{reporter_note}
4. 调用 Viktor submit endpoint 创建 GitLab issue，不要直接保存、打印或索要 GitLab bot token。
5. 如用户明确指定目标分支，把它放入 `target_branch`；否则留空让 Viktor 使用仓库默认分支。{routing_note}
6. 成功后把 `issue_url`、`coding_task_id` 或当前状态回复给用户。

## 必填模板
```markdown
{template}
```

## API 调用
```bash
curl -X POST "{submit_url}" \\
  -H "Content-Type: application/json" \\
  -d '{{
    "project_id": "{project_id}",
    "submit_token": "{cfg['submit_token']}",
    "kind": "{kind}",{repo_body_line}
    "target_branch": "",
    "reporter_display_name": "{reporter_display_name}",
    "reporter_mobile": "{reporter_mobile}",
    "title": "一句话标题",
    "description": "按模板整理后的 Markdown"
  }}'
```
"""
    return {"text": text, "filename": filename, "version": "3"}


def submit_local_agent_issue(
    *,
    project_id: str,
    submit_token: str = "",
    kind: str,
    title: str,
    description: str,
    repo_connector_id: str = "",
    target_branch: str = "",
    reporter_display_name: str = "",
    reporter_mobile: str = "",
    labels: list[str] | None = None,
    attachments: list[dict[str, Any]] | None = None,
    source: str = "local_agent",
    require_token: bool = True,
    create_coding_task: bool = True,
) -> dict[str, Any]:
    cfg = _require_config(project_id)
    if require_token and submit_token != cfg.submit_token:
        raise ValueError("Issue Intake submit_token 无效")
    # 需求人钉钉手机号强制必填：MR 合并后据此 @ 需求人，闭环少了它就断了。
    reporter_mobile = (reporter_mobile or "").strip()
    if not reporter_mobile:
        raise ValueError("reporter_mobile 必填（提交人钉钉手机号，用于 MR 合并后 @ 需求人）")
    # 项目级提交：不指定 repo 时由 Viktor 自动路由代码仓库；issue 统一落在项目默认 board。
    explicit_repo = (repo_connector_id or "").strip()
    board_target = _resolve_issue_target(project_id, explicit_repo or cfg.default_repo_connector_id)
    issue_project_url = board_target["issue_project_url"]
    normalized_kind = _normalize_kind(kind)
    description = _build_issue_description(
        project_id=project_id,
        repo_connector_id=explicit_repo,
        kind=normalized_kind,
        reporter=reporter_display_name,
        reporter_mobile=reporter_mobile,
        source=source,
        target_branch=target_branch,
        description=description,
        attachments=attachments or [],
    )
    issue_labels = _issue_labels([*(cfg.default_labels or []), *board_target["labels"]], labels or [], normalized_kind, explicit_repo, source)
    issue = create_issue(
        project_url=issue_project_url,
        title=_safe_title(title, normalized_kind),
        description=description,
        labels=issue_labels,
        confidential=True,
    )
    link = process_issue(
        project_id=project_id,
        issue=issue,
        issue_project_url=issue_project_url,
        source=source,
        fallback_repo_connector_id=explicit_repo or cfg.default_repo_connector_id,
        create_coding_task=create_coding_task,
    )
    return link


def scan_project_issues(project_id: str, *, repo_connector_id: str = "") -> dict[str, Any]:
    cfg = _require_config(project_id)
    if not cfg.enabled:
        raise ValueError("Issue Intake 未启用")
    targets = _scan_targets(project_id, repo_connector_id=repo_connector_id)
    processed: list[dict[str, Any]] = []
    skipped = 0
    total = 0
    target_results: list[dict[str, Any]] = []
    for target in targets:
        issue_project_url = target["issue_project_url"]
        issues = list_issues(project_url=issue_project_url, state="opened", labels=["viktor:auto"])
        target_total = len(issues)
        target_processed = 0
        target_skipped = 0
        total += target_total
        for issue in issues:
            labels = [str(item) for item in issue.get("labels") or []]
            if not _kind_from_labels(labels):
                skipped += 1
                target_skipped += 1
                continue
            try:
                processed.append(process_issue(
                    project_id=project_id,
                    issue=issue,
                    issue_project_url=issue_project_url,
                    source="scan",
                    fallback_repo_connector_id=target["repo_connector_id"],
                    create_coding_task=True,
                ))
                target_processed += 1
            except Exception as e:  # noqa: BLE001
                skipped += 1
                target_skipped += 1
                logger.exception(
                    "[issue-intake] process issue failed: project={}, repo={}, issue={}, err={}",
                    project_id,
                    target["repo_connector_id"],
                    issue.get("iid"),
                    e,
                )
        target_results.append({
            "repo_connector_id": target["repo_connector_id"],
            "issue_project_url": issue_project_url,
            "processed": target_processed,
            "skipped": target_skipped,
            "total": target_total,
        })
    return {
        "ok": True,
        "project_id": project_id,
        "processed": processed,
        "skipped": skipped,
        "total": total,
        "targets": target_results,
    }


def process_issue(
    *,
    project_id: str,
    issue: dict[str, Any],
    issue_project_url: str,
    source: str,
    fallback_repo_connector_id: str = "",
    create_coding_task: bool = True,
) -> dict[str, Any]:
    labels = [str(item) for item in issue.get("labels") or []]
    metadata = _metadata_from_issue(issue)
    kind = _kind_from_labels(labels) or _normalize_kind(str(metadata.get("kind") or "")) or "bug"
    explicit_repo = _repo_from_labels(labels) or str(metadata.get("repo_connector_id") or "").strip()
    cfg = _require_config(project_id)
    project_path = GitLabClient.extract_project_path(issue_project_url)
    base_url = gitlab_config.resolve_base_url(issue_project_url)
    iid = str(issue.get("iid") or "")
    if not iid:
        raise ValueError("GitLab issue 缺少 iid")

    missing = _validate_issue(issue)
    existing = _find_link(base_url, project_path, iid)
    if existing:
        link_id = existing.link_id
        _update_link_from_issue(link_id, issue, status=existing.status, stage=existing.stage, message=existing.message)
    else:
        routed = _decide_repos(
            project_id,
            cfg,
            kind=kind,
            title=str(issue.get("title") or ""),
            description=str(issue.get("description") or ""),
            explicit_repo=explicit_repo,
            fallback_repo_connector_id=fallback_repo_connector_id,
        )
        primary_repo = _resolve_repo(project_id, routed[0]["repo_connector_id"] or cfg.default_repo_connector_id)
        link_id = _create_link(
            project_id=project_id,
            repo_connector_id=primary_repo.id,
            issue=issue,
            issue_project_url=issue_project_url,
            source=source,
            kind=kind,
            metadata=metadata,
            routed=routed,
        )
        emit_issue_event(link_id, "issue_seen", "GitLab issue 已进入 Viktor Intake", {"issue_url": issue.get("web_url")}, stage="issue_created")
        if len(routed) > 1:
            emit_issue_event(
                link_id,
                "routed",
                "已自动路由到多个仓库：" + "、".join(r["repo_connector_id"] for r in routed),
                {"routed": routed},
                stage="issue_created",
            )

    if missing:
        message = "Issue 信息不完整：" + "、".join(missing)
        _set_link_status(link_id, "needs_info", "needs_info", message, last_error=message)
        try:
            create_issue_note(project_url=issue_project_url, issue_iid=iid, body=_needs_info_note(link_id, missing))
            add_issue_labels(project_url=issue_project_url, issue_iid=iid, labels=["viktor:needs-info"])
        except Exception as e:  # noqa: BLE001
            logger.warning("[issue-intake] comment needs-info failed link={}: {}", link_id, e)
        emit_issue_event(link_id, "needs_info", message, {"missing": missing}, stage="needs_info")
        return get_issue_intake_link(link_id) or {}

    link = _get_link_model(link_id)
    if not link:
        raise ValueError("Issue link 创建失败")
    from core.temporal import trigger

    if link.status in FINAL_LINK_STATUSES:
        return _link_to_dict(link)

    if link.coding_task_id:
        if link.status in ACTIVE_LINK_STATUSES:
            # 接管模式：由 IssueLinkWorkflow 驱动（幂等启动）；否则旧 watcher 线程。
            if not trigger.start_issue_link_sync(link_id):
                _ensure_task_watcher(link_id, link.coding_task_id)
        return _link_to_dict(link)

    if create_coding_task:
        # 接管模式：启动父 workflow（其 prepare_child_tasks 活动再建各仓 task）；否则旧路径直接建。
        if not trigger.start_issue_link_sync(link_id):
            create_coding_tasks_for_issue(link_id)
    return get_issue_intake_link(link_id) or {}


def _decide_repos(
    project_id: str,
    cfg: IssueIntakeConfigModel,
    *,
    kind: str,
    title: str,
    description: str,
    explicit_repo: str = "",
    fallback_repo_connector_id: str = "",
) -> list[dict[str, str]]:
    """决定 issue 落到哪些代码仓库：显式指定优先，否则用强模型路由，再不行回退默认仓库。"""
    if explicit_repo and registry.get_repository_connector(project_id, explicit_repo):
        return [{"repo_connector_id": explicit_repo, "reason": "issue 显式指定仓库"}]
    routed = route_issue(
        project_id,
        kind=kind,
        title=title,
        description=description,
        default_repo_connector_id=cfg.default_repo_connector_id or fallback_repo_connector_id,
    )
    if routed:
        return routed
    repo = _resolve_repo(project_id, cfg.default_repo_connector_id or fallback_repo_connector_id)
    return [{"repo_connector_id": repo.id, "reason": "默认仓库"}]


def _store_link_blueprint(link_id: str, blueprint: dict[str, Any]) -> None:
    with _RESULT_LOCK:
        db = SessionLocal()
        try:
            row = db.get(IssueIntakeLinkModel, link_id)
            if row:
                row.result = {**(row.result or {}), "blueprint": blueprint}
                db.commit()
        finally:
            db.close()


def prepare_link_blueprint(link_id: str) -> dict[str, Any]:
    """为 link 生成 blueprint（收敛仓库 + 跨仓契约），存 link.result.blueprint（status=pending）。"""
    link = _get_link_model(link_id)
    if not link:
        raise ValueError("Issue link 不存在")
    from core.issue_blueprint import build_blueprint
    routed = (link.result or {}).get("routed") or [{"repo_connector_id": link.repo_connector_id, "reason": ""}]
    bp = build_blueprint(
        link.project_id, kind=link.kind or "", title=link.title or "",
        description=link.description or "", routed=routed,
    )
    bp = {**bp, "status": "pending", "reviewer": "", "comment": ""}
    _store_link_blueprint(link_id, bp)
    emit_issue_event(
        link_id, "blueprint_prepared",
        f"已生成改动蓝图：{len(bp.get('repos') or [])} 个仓库 / {len(bp.get('contracts') or [])} 个跨仓契约，等待人工确认",
        {"blueprint": bp}, stage="blueprint_review",
    )
    _set_link_status(link_id, "blueprint_review", "blueprint_review", "改动蓝图已生成，等待人工确认仓库与契约")
    _notify_blueprint_review(link_id, bp)
    return bp


def _notify_blueprint_review(link_id: str, bp: dict[str, Any]) -> None:
    """蓝图待确认：发钉钉提醒去裁剪仓库/确认契约，@ 候选仓维护人（与 MR 就绪同套路）。"""
    repos = bp.get("repos") or []
    contracts = bp.get("contracts") or []
    repo_lines = [
        f"- `{r.get('repo_connector_id')}`"
        + (f"（{r.get('role')}）" if r.get("role") else "")
        + (f"：{r.get('reason')}" if r.get("reason") else "")
        for r in repos
    ]
    contract_lines = [
        f"- `{c.get('method')} {c.get('path')}`（{c.get('owner_repo')} → {'、'.join(c.get('consumer_repos') or []) or '-'}）"
        for c in contracts
    ]
    body_parts = ["**拟改动仓库**", "\n".join(repo_lines) or "-"]
    if contract_lines:
        body_parts += ["", "**跨仓接口契约**", "\n".join(contract_lines)]
    if bp.get("analysis"):
        body_parts += ["", f"_判断_：{bp.get('analysis')}"]
    link = _get_link_model(link_id)
    url = _blueprint_review_url(link_id, link.project_id if link else "")
    body_parts += ["", f"[前往工作台确认/裁剪改动蓝图]({url})" if url else "请在 Viktor 工作台确认/裁剪仓库与契约后放行。"]
    mobiles = [_repo_maintainer_mobile(link.project_id, str(r.get("repo_connector_id") or "")) for r in repos] if link else []
    _notify_for_link(link_id, "Viktor 待确认改动蓝图", "\n".join(body_parts), extra_mobiles=mobiles)


def apply_link_blueprint(
    link_id: str,
    repos: list[dict[str, Any]] | None,
    contracts: list[dict[str, Any]] | None,
    reviewer: str = "",
    comment: str = "",
) -> None:
    """人审结果落地：收敛后的仓库写回 result.routed、契约写入 result.blueprint（status=approved）。"""
    link = _get_link_model(link_id)
    if not link:
        raise ValueError("Issue link 不存在")
    valid = {r.id for r in (registry.get_repository_connectors(link.project_id) or []) if r.id}
    routed: list[dict[str, str]] = []
    seen: set[str] = set()
    for r in repos or []:
        rid = str((r or {}).get("repo_connector_id") or "").strip()
        if rid in valid and rid not in seen:
            routed.append({"repo_connector_id": rid, "reason": str((r or {}).get("reason") or "")})
            seen.add(rid)
    if not routed:  # 人审清空 → 回退原 routed，避免无仓可建
        routed = (link.result or {}).get("routed") or [{"repo_connector_id": link.repo_connector_id, "reason": ""}]
    prev_bp = (link.result or {}).get("blueprint", {}) if isinstance(link.result, dict) else {}
    bp = {
        "repos": routed,
        "contracts": contracts or [],
        "status": "approved",
        "reviewer": reviewer,
        "comment": comment,
        "analysis": prev_bp.get("analysis", ""),
    }
    with _RESULT_LOCK:
        db = SessionLocal()
        try:
            row = db.get(IssueIntakeLinkModel, link_id)
            if row:
                row.result = {**(row.result or {}), "routed": routed, "blueprint": bp}
                db.commit()
        finally:
            db.close()
    emit_issue_event(
        link_id, "blueprint_approved",
        f"改动蓝图已确认：落 {len(routed)} 个仓库" + (f"（备注：{comment}）" if comment else ""),
        {"repos": [r["repo_connector_id"] for r in routed], "contracts": contracts or []},
        stage="blueprint_review",
    )


def create_coding_tasks_for_issue(link_id: str) -> list[str]:
    """按路由结果为 issue 创建 1..N 个 Coding Task（每个仓库一个），主仓库驱动 link 状态。"""
    link = _get_link_model(link_id)
    if not link:
        raise ValueError("Issue link 不存在")
    if link.coding_task_id:
        existing = [t.get("coding_task_id") for t in (link.result or {}).get("coding_tasks") or [] if t.get("coding_task_id")]
        return existing or [link.coding_task_id]

    from core.coding_service import start_coding_task

    routed = (link.result or {}).get("routed") or [{"repo_connector_id": link.repo_connector_id, "reason": ""}]
    repo_ids = [str(r.get("repo_connector_id") or "").strip() for r in routed]
    repo_ids = [rid for rid in repo_ids if rid] or [link.repo_connector_id]
    target_branch = str((link.result or {}).get("target_branch") or "").strip()

    coding_tasks: list[dict[str, Any]] = []
    for idx, rid in enumerate(repo_ids):
        try:
            task_id = start_coding_task(
                project_id=link.project_id,
                requirement=_coding_requirement(link, this_repo=rid, routed=routed),
                repo_connector_id=rid,
                target_branch=target_branch,
                # 自动接单的 feature issue 经常需要新建表/迁移，默认禁止会让任务在
                # 校验阶段被 blocker 卡死（见 ct_ca2768d180c049ac）。MR 人工审核仍是最终关口。
                policy={"allow_schema_change": True},
                create_mr=True,
                created_by=f"issue-intake:{link.issue_url or link.issue_iid}",
                created_by_mobile=str((link.result or {}).get("reporter_mobile") or "").strip(),
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("[issue-intake] 为仓库 {} 创建 Coding Task 失败 link={}: {}", rid, link_id, e)
            emit_issue_event(link_id, "coding_task_failed", f"为仓库 {rid} 创建 Coding Task 失败：{e}", {"repo_connector_id": rid}, stage="plan_waiting")
            continue
        coding_tasks.append({
            "repo_connector_id": rid,
            "coding_task_id": task_id,
            "coding_task_url": _coding_task_url(task_id, link.project_id),
            "role": "primary" if not coding_tasks else "secondary",
            "reason": next((str(r.get("reason") or "") for r in routed if r.get("repo_connector_id") == rid), ""),
            "mr_url": "",
            "merged": False,
        })

    if not coding_tasks:
        _fail_link(link_id, "所有目标仓库的 Coding Task 创建均失败")
        return []

    primary = coding_tasks[0]
    db = SessionLocal()
    try:
        row = db.get(IssueIntakeLinkModel, link_id)
        if row:
            row.coding_task_id = primary["coding_task_id"]
            row.repo_connector_id = primary["repo_connector_id"]
            row.status = "coding_task_created"
            row.stage = "plan_waiting"
            row.message = (
                f"已路由到 {len(coding_tasks)} 个仓库，Coding Task 创建完成，等待 Plan"
                if len(coding_tasks) > 1 else "Coding Task 已创建，等待 Plan"
            )
            row.result = {**(row.result or {}), "coding_tasks": coding_tasks}
        for t in coding_tasks:
            task = db.get(CodingTaskModel, t["coding_task_id"])
            if task:
                task.result = {**(task.result or {}), "source_issue": _source_issue_payload(link, task_id=t["coding_task_id"])}
        db.commit()
    finally:
        db.close()

    emit_issue_event(
        link_id,
        "coding_task_created",
        f"已创建 {len(coding_tasks)} 个 Coding Task" if len(coding_tasks) > 1 else "Coding Task 已创建",
        {"coding_tasks": [t["coding_task_id"] for t in coding_tasks], "repos": [t["repo_connector_id"] for t in coding_tasks]},
        stage="plan_waiting",
    )
    try:
        if len(coding_tasks) > 1:
            lines = "\n".join(
                f"- `{t['repo_connector_id']}` → Coding Task {_coding_task_markdown(t['coding_task_id'], link.project_id)}"
                for t in coding_tasks
            )
            note = "Viktor 已接单，自动路由到以下仓库：\n" + lines
        else:
            note = f"Viktor 已接单，跟踪 ID / Coding Task: {_coding_task_markdown(primary['coding_task_id'], link.project_id)}。"
        create_issue_note(project_url=_issue_project_url_for_link(link), issue_iid=link.issue_iid, body=note)
    except Exception as e:  # noqa: BLE001
        logger.warning("[issue-intake] comment task created failed link={}: {}", link_id, e)

    # 接管模式：子流程由 IssueLinkWorkflow 派发驱动，不再 spawn watcher 线程。
    # （本函数也被 workflow 的 prepare_child_tasks 活动调用——此时必须不 spawn，避免线程泄漏。）
    from core.temporal import trigger
    if not trigger.enabled():
        _ensure_task_watcher(link_id, primary["coding_task_id"])
        for t in coding_tasks[1:]:
            _ensure_secondary_watcher(link_id, t["coding_task_id"], t["repo_connector_id"])
    return [t["coding_task_id"] for t in coding_tasks]


def auto_drive_coding_task(link_id: str, task_id: str) -> None:
    from core.coding_service import get_task, review_plan, start_execution

    # 重扫 issue 可能重起本 watcher：若 task 已越过 plan/执行阶段，直接收尾，
    # 不再进入等待循环（否则空转到超时会把 status 打回 plan_waiting）。
    task = get_task(task_id)
    if task and str(task.get("status") or "") in {"waiting_code_review", "completed"}:
        _mark_mr_ready(link_id, task)
        return

    deadline = time.time() + max(60, watchdog_config.plan_wait_timeout_sec)
    while time.time() < deadline:
        task = get_task(task_id)
        if not task:
            time.sleep(3)
            continue
        status = str(task.get("status") or "")
        if status in {"waiting_code_review", "completed"}:
            # 等待 plan 期间 task 已被推进到代码审核（如人工/其它路径），直接收尾。
            _mark_mr_ready(link_id, task)
            return
        if status == "waiting_plan_review":
            try:
                review_plan(task_id, decision="approved", comment="Issue Intake 自动审批", reviewer="issue-intake")
                emit_issue_event(link_id, "plan_approved", "Plan 已由 Issue Intake 自动审批", {"coding_task_id": task_id}, stage="plan_approved")
                start_execution(task_id)
                _set_link_status(link_id, "running", "running", "Coding Task 已启动执行")
            except Exception as e:  # noqa: BLE001
                _fail_link(link_id, f"自动审批/启动 Coding Task 失败：{e}")
                return
            break
        if status == "waiting_clarification":
            _set_link_status(link_id, "needs_info", "waiting_clarification", "Coding Task 需要补充信息")
            link = _get_link_model(link_id)
            task_ref = _coding_task_markdown(task_id, link.project_id if link else "")
            _comment_issue_for_link(
                link_id,
                f"Viktor 需要补充信息后才能继续，跟踪 ID: {task_ref}。请在 Viktor Coding 工作台查看澄清问题。",
            )
            return
        if status in {"failed", "cancelled", "plan_rejected"}:
            _fail_link(link_id, f"Coding Task 未能进入执行：{task.get('message') or status}")
            return
        time.sleep(5)
    else:
        # 仅当 link 尚未前进时才标超时；已到 mr_created/waiting_code_review 的不回退。
        cur = _get_link_model(link_id)
        if not cur or cur.status not in {"mr_created", "waiting_code_review", "issue_closed", "completed"}:
            _set_link_status(link_id, "plan_waiting", "plan_waiting", "等待 Coding Task Plan 超时，将由后台继续同步")
        return

    follow_deadline = time.time() + 7200
    while time.time() < follow_deadline:
        task = get_task(task_id)
        if not task:
            time.sleep(10)
            continue
        status = str(task.get("status") or "")
        if status in {"waiting_code_review", "completed"}:
            _mark_mr_ready(link_id, task)
            return
        if status in {"failed", "cancelled", "plan_rejected"}:
            _fail_link(link_id, f"Coding Task 执行失败：{task.get('message') or status}")
            return
        time.sleep(15)


def handle_merge_request_merged(payload: dict[str, Any], task_ids: list[str] | None = None) -> dict[str, Any]:
    attrs = payload.get("object_attributes") if isinstance(payload.get("object_attributes"), dict) else {}
    action = str(attrs.get("action") or "").lower()
    state = str(attrs.get("state") or "").lower()
    if action != "merge" and state != "merged":
        return {"issue_intake_matched": 0, "issue_intake_closed": 0}
    mr_url = str(attrs.get("url") or attrs.get("web_url") or "").strip()
    source_branch = str(attrs.get("source_branch") or "").strip()
    task_set = set(task_ids or [])
    db = SessionLocal()
    try:
        candidates = (
            db.query(IssueIntakeLinkModel)
            .filter(IssueIntakeLinkModel.status.in_([
                "coding_task_created", "plan_waiting", "running", "mr_created", "waiting_code_review",
            ]))
            .limit(500)
            .all()
        )
        # 命中到 (link_id, task_id) 粒度：一个 link 下可能有多个仓库的 task。
        matched: list[tuple[str, str]] = []
        for row in candidates:
            tasks = list((row.result or {}).get("coding_tasks") or [])
            if not tasks and row.coding_task_id:
                tasks = [{"coding_task_id": row.coding_task_id, "mr_url": row.mr_url}]
            for t in tasks:
                tid = str(t.get("coding_task_id") or "")
                if not tid:
                    continue
                task_match = bool(task_set and tid in task_set)
                url_match = bool(mr_url and str(t.get("mr_url") or "") == mr_url)
                branch_match = bool(source_branch and source_branch == f"viktor/{tid}")
                if task_match or url_match or branch_match:
                    matched.append((row.link_id, tid))
    finally:
        db.close()

    closed = 0
    handled_links: set[str] = set()
    for link_id, task_id in matched:
        try:
            all_merged = _mark_task_merged(link_id, task_id, mr_url)
            if all_merged:
                if link_id not in handled_links:
                    _close_issue_for_link(link_id)
                    closed += 1
                    handled_links.add(link_id)
            else:
                emit_issue_event(link_id, "task_merged", "一个仓库的 MR 已合并，等待其余仓库", {"coding_task_id": task_id, "mr_url": mr_url}, stage="waiting_code_review")
        except Exception as e:  # noqa: BLE001
            logger.exception("[issue-intake] close issue failed link={}: {}", link_id, e)
            _fail_link(link_id, f"MR 已合并，但关闭 issue 失败：{e}")
    return {"issue_intake_matched": len({m[0] for m in matched}), "issue_intake_closed": closed}


def handle_merge_request_closed(payload: dict[str, Any], task_ids: list[str] | None = None) -> dict[str, Any]:
    """MR 被关闭（未合并）时，把关联 link 标记为 failed。

    与 handle_merge_request_merged 对称：合并会关闭 issue，而关闭（未合并）说明需求
    未完成，link 置 failed 并在 issue 上评论，issue 本身保留给人工处理。
    """
    attrs = payload.get("object_attributes") if isinstance(payload.get("object_attributes"), dict) else {}
    action = str(attrs.get("action") or "").lower()
    state = str(attrs.get("state") or "").lower()
    if action != "close" and state != "closed":
        return {"issue_intake_matched": 0, "issue_intake_failed": 0}
    mr_url = str(attrs.get("url") or attrs.get("web_url") or "").strip()
    source_branch = str(attrs.get("source_branch") or "").strip()
    task_set = set(task_ids or [])
    db = SessionLocal()
    try:
        candidates = (
            db.query(IssueIntakeLinkModel)
            .filter(IssueIntakeLinkModel.status.in_([
                "coding_task_created", "plan_waiting", "running", "mr_created", "waiting_code_review",
            ]))
            .limit(500)
            .all()
        )
        matched: list[tuple[str, str]] = []
        for row in candidates:
            tasks = list((row.result or {}).get("coding_tasks") or [])
            if not tasks and row.coding_task_id:
                tasks = [{"coding_task_id": row.coding_task_id, "mr_url": row.mr_url}]
            for t in tasks:
                tid = str(t.get("coding_task_id") or "")
                if not tid:
                    continue
                task_match = bool(task_set and tid in task_set)
                url_match = bool(mr_url and str(t.get("mr_url") or "") == mr_url)
                branch_match = bool(source_branch and source_branch == f"viktor/{tid}")
                if task_match or url_match or branch_match:
                    matched.append((row.link_id, tid))
    finally:
        db.close()

    failed = 0
    handled_links: set[str] = set()
    for link_id, task_id in matched:
        if link_id in handled_links:
            continue
        handled_links.add(link_id)
        try:
            _fail_link(link_id, "MR 已关闭（未合并），任务取消")
            failed += 1
        except Exception as e:  # noqa: BLE001
            logger.exception("[issue-intake] mark link failed on mr close link={}: {}", link_id, e)
    return {"issue_intake_matched": len(handled_links), "issue_intake_failed": failed}


def start_issue_intake_task_watcher(link_id: str, task_id: str) -> None:
    _ensure_task_watcher(link_id, task_id)


def _watcher_alive(name: str) -> bool:
    """是否已有同名 watcher 线程存活。后台每 300s 重扫仍 opened 的 issue，
    若不去重会反复重起 watcher（多线程读改 result 丢更新、空转把 status 打回 plan_waiting）。"""
    return any(t.name == name and t.is_alive() for t in threading.enumerate())


def _ensure_task_watcher(link_id: str, task_id: str) -> None:
    name = f"issue-intake-{link_id}"
    if _watcher_alive(name):
        return
    thread = threading.Thread(
        target=lambda: auto_drive_coding_task(link_id, task_id),
        daemon=True,
        name=name,
    )
    thread.start()


def _ensure_secondary_watcher(link_id: str, task_id: str, repo_connector_id: str) -> None:
    name = f"issue-intake-sec-{task_id}"
    if _watcher_alive(name):
        return
    thread = threading.Thread(
        target=lambda: _auto_drive_secondary(link_id, task_id, repo_connector_id),
        daemon=True,
        name=name,
    )
    thread.start()


def _auto_drive_secondary(link_id: str, task_id: str, repo_connector_id: str) -> None:
    """驱动多仓路由里的从仓库任务：自动审批 Plan、启动执行、记录 MR；不改 link 顶层状态。"""
    from core.coding_service import get_task, review_plan, start_execution

    # 重扫可能重起本 watcher：task 已进入代码审核则直接补记 MR 并触发聚合通知，不再空转。
    task = get_task(task_id)
    if task and str(task.get("status") or "") in {"waiting_code_review", "completed"}:
        mr_url = str(task.get("mr_url") or "")
        _record_task_mr(link_id, task_id, mr_url)
        _maybe_notify_all_mr_ready(link_id)
        return

    deadline = time.time() + max(60, watchdog_config.plan_wait_timeout_sec)
    started = False
    while time.time() < deadline:
        task = get_task(task_id)
        if not task:
            time.sleep(3)
            continue
        status = str(task.get("status") or "")
        if status == "waiting_plan_review":
            try:
                review_plan(task_id, decision="approved", comment="Issue Intake 自动审批（多仓路由从仓库）", reviewer="issue-intake")
                start_execution(task_id)
                emit_issue_event(link_id, "secondary_running", f"仓库 {repo_connector_id} 的 Coding Task 已启动执行", {"coding_task_id": task_id, "repo_connector_id": repo_connector_id}, stage="running")
            except Exception as e:  # noqa: BLE001
                emit_issue_event(link_id, "secondary_failed", f"仓库 {repo_connector_id} 的 Coding Task 自动启动失败：{e}", {"coding_task_id": task_id, "repo_connector_id": repo_connector_id}, stage="running")
                return
            started = True
            break
        if status == "waiting_clarification":
            emit_issue_event(link_id, "secondary_needs_info", f"仓库 {repo_connector_id} 的 Coding Task 需要补充信息", {"coding_task_id": task_id, "repo_connector_id": repo_connector_id}, stage="waiting_clarification")
            return
        if status in {"failed", "cancelled", "plan_rejected"}:
            emit_issue_event(link_id, "secondary_failed", f"仓库 {repo_connector_id} 的 Coding Task 未能进入执行：{task.get('message') or status}", {"coding_task_id": task_id, "repo_connector_id": repo_connector_id}, stage="failed")
            return
        time.sleep(5)
    if not started:
        emit_issue_event(link_id, "secondary_timeout", f"仓库 {repo_connector_id} 的 Coding Task 等待 Plan 超时", {"coding_task_id": task_id, "repo_connector_id": repo_connector_id}, stage="plan_waiting")
        return

    follow_deadline = time.time() + 7200
    while time.time() < follow_deadline:
        task = get_task(task_id)
        if not task:
            time.sleep(10)
            continue
        status = str(task.get("status") or "")
        if status in {"waiting_code_review", "completed"}:
            mr_url = str(task.get("mr_url") or "")
            _record_task_mr(link_id, task_id, mr_url)
            emit_issue_event(link_id, "secondary_mr_ready", f"仓库 {repo_connector_id} 的 Coding Task 已进入代码审核", {"coding_task_id": task_id, "repo_connector_id": repo_connector_id, "mr_url": mr_url}, stage="waiting_code_review")
            # 从仓 MR 落库后，检查是否全部到齐 → 触发聚合通知（聚合点对主/从仓都生效）。
            _maybe_notify_all_mr_ready(link_id)
            return
        if status in {"failed", "cancelled", "plan_rejected"}:
            emit_issue_event(link_id, "secondary_failed", f"仓库 {repo_connector_id} 的 Coding Task 执行失败：{task.get('message') or status}", {"coding_task_id": task_id, "repo_connector_id": repo_connector_id}, stage="failed")
            return
        time.sleep(15)


def _record_task_mr(link_id: str, task_id: str, mr_url: str) -> None:
    if not mr_url:
        return
    with _RESULT_LOCK:
        db = SessionLocal()
        try:
            row = db.get(IssueIntakeLinkModel, link_id)
            if not row:
                return
            result = dict(row.result or {})
            tasks = list(result.get("coding_tasks") or [])
            changed = False
            for t in tasks:
                if t.get("coding_task_id") == task_id and t.get("mr_url") != mr_url:
                    t["mr_url"] = mr_url
                    changed = True
            if changed:
                result["coding_tasks"] = tasks
                row.result = result
                db.commit()
        finally:
            db.close()


def _mark_task_merged(link_id: str, task_id: str, mr_url: str = "") -> bool:
    """把某个 coding task 标记为已合并，返回该 link 下所有 task 是否都已合并。"""
    with _RESULT_LOCK:
        db = SessionLocal()
        try:
            row = db.get(IssueIntakeLinkModel, link_id)
            if not row:
                return False
            result = dict(row.result or {})
            tasks = list(result.get("coding_tasks") or [])
            if not tasks and row.coding_task_id:
                tasks = [{
                    "repo_connector_id": row.repo_connector_id,
                    "coding_task_id": row.coding_task_id,
                    "role": "primary",
                    "mr_url": row.mr_url or mr_url,
                    "merged": False,
                }]
            for t in tasks:
                if t.get("coding_task_id") == task_id:
                    t["merged"] = True
                    if mr_url and not t.get("mr_url"):
                        t["mr_url"] = mr_url
            result["coding_tasks"] = tasks
            row.result = result
            db.commit()
            return all(bool(t.get("merged")) for t in tasks) if tasks else True
        finally:
            db.close()


def _coding_task_merged_state(db: Any, task_id: str) -> tuple[bool, str]:
    """从 coding task 表判断某 task 是否已合并。返回 (是否合并, mr_url)。

    合并的判定：task.status==completed 且 code_review.status==merged（webhook/对账写入）。
    仅 completed 不够——人工'完成代码审核'也会 completed，但那不是合并。"""
    task = db.get(CodingTaskModel, task_id)
    if not task:
        return (False, "")
    result = task.result if isinstance(task.result, dict) else {}
    review = result.get("code_review") if isinstance(result.get("code_review"), dict) else {}
    merged = str(task.status or "") == "completed" and str(review.get("status") or "") == "merged"
    mr_url = str(getattr(task, "mr_url", "") or result.get("mr_url") or "").strip()
    return (merged, mr_url)


def reconcile_issue_links_merged() -> dict[str, Any]:
    """link 侧合并对账兜底：扫描 active link，若其所有 coding_task 在 task 表里都已合并，
    但 link 还没走关闭闭环，则补触发关闭 + 通知发起人。

    背景：reconciler 只扫 status==waiting_code_review 的 coding task；一旦 task 被对账成
    completed 而 issue 联动那一步漏掉（webhook 漏发 + 对账时 issue 联动异常/旧代码），
    task 已 completed 后 reconciler 不再触碰，link 永久卡 mr_created、issue 不关、发起人
    收不到通知。本函数以 link 为锚兜底，无论上游哪一步漏都能收尾。
    """
    # Temporal 接管后：合并收尾由 IssueLinkWorkflow 负责，旧对账停手避免双重关 issue/重复通知。
    if temporal_config.enabled:
        return {"checked": 0, "closed": 0}

    db = SessionLocal()
    try:
        rows = (
            db.query(IssueIntakeLinkModel)
            .filter(IssueIntakeLinkModel.status.in_([
                "coding_task_created", "plan_waiting", "running", "mr_created", "waiting_code_review",
            ]))
            .limit(500)
            .all()
        )
        # 先在一个 session 内算出哪些 link 全部 task 已合并，避免边遍历边改。
        ready: list[tuple[str, list[tuple[str, str]]]] = []
        for row in rows:
            tasks = list((row.result or {}).get("coding_tasks") or [])
            if not tasks and row.coding_task_id:
                tasks = [{"coding_task_id": row.coding_task_id}]
            task_ids = [str(t.get("coding_task_id") or "").strip() for t in tasks]
            task_ids = [tid for tid in task_ids if tid]
            if not task_ids:
                continue
            states = [(tid, *_coding_task_merged_state(db, tid)) for tid in task_ids]
            if all(merged for _, merged, _ in states):
                ready.append((row.link_id, [(tid, mr) for tid, _, mr in states]))
    finally:
        db.close()

    closed = 0
    for link_id, task_mrs in ready:
        try:
            # ready 已基于 task 表确认全部 task 合并（可信源），直接标记并收尾。
            # 不依赖 _mark_task_merged 的返回值二次判定——link.result 里的 merged 可能被
            # watcher 重入的整列写回冲脏（同 mr_url 写空的并发问题），否则会卡死永不收尾。
            for tid, mr in task_mrs:
                _mark_task_merged(link_id, tid, mr)
            link = _get_link_model(link_id)
            if link and link.status not in {"issue_closed", "completed", "failed"}:
                _close_issue_for_link(link_id)
                closed += 1
        except Exception as e:  # noqa: BLE001
            logger.exception("[issue-intake] link 侧合并对账收尾失败 link={}: {}", link_id, e)
    if ready:
        logger.info("[issue-intake] link 侧合并对账：candidates={} closed={}", len(ready), closed)
    return {"checked": len(ready), "closed": closed}


def _require_config(project_id: str) -> IssueIntakeConfigModel:
    cfg = _project_config(project_id)
    if not cfg:
        raise ValueError("项目尚未配置 Issue Intake")
    return cfg


def _normalize_target_payloads(
    *,
    project_id: str,
    targets: Any,
    default_repo_connector_id: str,
    legacy_issue_project_url: str,
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in _as_list(targets):
        data = _as_dict(item)
        repo_connector_id = str(data.get("repo_connector_id") or "").strip()
        if not repo_connector_id:
            raise ValueError("Issue Intake 扫描目标缺少 repo_connector_id")
        if repo_connector_id in seen:
            raise ValueError(f"Issue Intake 扫描目标重复: {repo_connector_id}")
        repo = _resolve_repo(project_id, repo_connector_id)
        issue_project_url = str(data.get("issue_project_url") or "").strip() or repo.git_url
        if not issue_project_url:
            raise ValueError(f"Repository Connector {repo_connector_id} 缺少 GitLab URL")
        normalized.append({
            "project_id": project_id,
            "repo_connector_id": repo.id,
            "issue_project_url": issue_project_url,
            "labels": _normalize_labels(data.get("labels") or []),
            "enabled": 1 if data.get("enabled", True) else 0,
        })
        seen.add(repo_connector_id)
    if not normalized and (default_repo_connector_id or legacy_issue_project_url):
        repo = _resolve_repo(project_id, default_repo_connector_id)
        normalized.append({
            "project_id": project_id,
            "repo_connector_id": repo.id,
            "issue_project_url": legacy_issue_project_url or repo.git_url,
            "labels": [],
            "enabled": 1,
        })
    return normalized


def _target_rows(project_id: str, *, enabled_only: bool = False) -> list[IssueIntakeTargetModel]:
    return _list_target_models(project_id, enabled_only=enabled_only)


def _runtime_target_from_row(row: IssueIntakeTargetModel) -> dict[str, Any]:
    return {
        "repo_connector_id": row.repo_connector_id,
        "issue_project_url": row.issue_project_url,
        "labels": row.labels or [],
        "enabled": bool(row.enabled),
    }


def _legacy_target_from_config(cfg: IssueIntakeConfigModel, *, repo_connector_id: str = "") -> dict[str, Any]:
    repo = _resolve_repo(cfg.project_id, repo_connector_id or cfg.default_repo_connector_id)
    issue_project_url = cfg.issue_project_url or repo.git_url
    if not issue_project_url:
        raise ValueError(f"Repository Connector {repo.id} 缺少 GitLab Issue 项目 URL")
    return {
        "repo_connector_id": repo.id,
        "issue_project_url": issue_project_url,
        "labels": [],
        "enabled": True,
    }


def _scan_targets(project_id: str, *, repo_connector_id: str = "") -> list[dict[str, Any]]:
    cfg = _require_config(project_id)
    rows = _target_rows(project_id, enabled_only=True)
    if repo_connector_id:
        matched = [row for row in rows if row.repo_connector_id == repo_connector_id]
        if matched:
            return [_runtime_target_from_row(row) for row in matched]
        if rows:
            raise ValueError(f"Repository Connector {repo_connector_id} 未配置 Issue Intake 扫描目标")
        return [_legacy_target_from_config(cfg, repo_connector_id=repo_connector_id)]
    # 默认只扫描项目默认仓库的 issue board（项目级单一入口），代码仓库由路由决定。
    default_repo = cfg.default_repo_connector_id
    if rows:
        if default_repo:
            for row in rows:
                if row.repo_connector_id == default_repo:
                    return [_runtime_target_from_row(row)]
        return [_runtime_target_from_row(rows[0])]
    return [_legacy_target_from_config(cfg)]


def _resolve_issue_target(project_id: str, repo_connector_id: str = "") -> dict[str, Any]:
    cfg = _require_config(project_id)
    rows = _target_rows(project_id, enabled_only=False)
    enabled_rows = [row for row in rows if row.enabled == 1]
    if repo_connector_id:
        for row in enabled_rows:
            if row.repo_connector_id == repo_connector_id:
                return _runtime_target_from_row(row)
        if rows:
            raise ValueError(f"Repository Connector {repo_connector_id} 未启用 Issue Intake target")
        return _legacy_target_from_config(cfg, repo_connector_id=repo_connector_id)
    default_repo = cfg.default_repo_connector_id
    if default_repo:
        for row in enabled_rows:
            if row.repo_connector_id == default_repo:
                return _runtime_target_from_row(row)
    if enabled_rows:
        return _runtime_target_from_row(enabled_rows[0])
    return _legacy_target_from_config(cfg)


def _resolve_repo(project_id: str, repo_connector_id: str = ""):
    if repo_connector_id:
        repo = registry.get_repository_connector(project_id, repo_connector_id)
        if not repo:
            raise ValueError(f"Repository Connector {repo_connector_id} 不存在")
        return repo
    repos = registry.get_repository_connectors(project_id)
    if repos:
        return repos[0]
    project = registry.get_project(project_id)
    if not project or not project.git_url:
        raise ValueError(f"项目 {project_id} 未配置代码仓库")
    # RepositoryConnectorItem 不是必须落库；这里构造最小兼容对象。
    from core.registry import RepositoryConnectorItem

    return RepositoryConnectorItem(
        id="",
        project_id=project_id,
        display_name=project.name,
        git_url=project.git_url,
        default_branch=project.default_branch,
    )


def _normalize_labels(value: Any) -> list[str]:
    if isinstance(value, str):
        raw = value.split(",")
    else:
        raw = _as_list(value)
    return [str(item).strip() for item in raw if str(item).strip()]


def _normalize_kind(kind: str) -> str:
    lowered = (kind or "").strip().lower()
    return "feature" if lowered == "feature" else "bug"


def _safe_title(title: str, kind: str) -> str:
    text = (title or "").strip()
    if not text:
        text = "待处理需求" if kind == "feature" else "待处理 Bug"
    return text[:255]


def _issue_labels(config_labels: Any, extra_labels: list[str], kind: str, repo_connector_id: str, source: str) -> list[str]:
    labels = _normalize_labels(config_labels)
    labels.extend(extra_labels)
    labels.extend(["viktor:auto", f"type:{kind}", f"source:{source}"])
    if repo_connector_id:
        labels.append(f"repo:{repo_connector_id}")
    return list(dict.fromkeys(label for label in labels if label))


def _build_issue_description(
    *,
    project_id: str,
    repo_connector_id: str,
    kind: str,
    reporter: str,
    reporter_mobile: str = "",
    source: str,
    target_branch: str,
    description: str,
    attachments: list[dict[str, Any]],
) -> str:
    attachment_lines: list[str] = []
    for item in attachments:
        filename = str(item.get("filename") or "attachment")
        url = str(item.get("download_url") or "")
        preview = str(item.get("extracted_preview") or item.get("extracted_text") or "").strip()
        attachment_lines.append(f"- [{filename}]({url})" if url else f"- {filename}")
        if preview:
            attachment_lines.append(f"  - 摘要: {preview[:500]}")
    attachments_block = "\n".join(attachment_lines) or "- 无"
    metadata = {
        "project_id": project_id,
        "repo_connector_id": repo_connector_id,
        "kind": kind,
        "submitter_display_name": reporter,
        "reporter_mobile": reporter_mobile,
        "source": source,
        "target_branch": target_branch,
    }
    return f"""{description.strip()}

## 附件
{attachments_block}

<!-- viktor-issue-intake
{json.dumps(metadata, ensure_ascii=False, indent=2)}
-->
"""


def _kind_from_labels(labels: list[str]) -> str:
    lowered = {item.lower() for item in labels}
    if "type:feature" in lowered:
        return "feature"
    if "type:bug" in lowered:
        return "bug"
    return ""


def _repo_from_labels(labels: list[str]) -> str:
    for label in labels:
        if label.startswith("repo:"):
            return label.split(":", 1)[1].strip()
    return ""


def _validate_issue(issue: dict[str, Any]) -> list[str]:
    # 目标仓库不再要求人工指定：由路由自动决定，因此不校验 repo。
    missing: list[str] = []
    if not str(issue.get("title") or "").strip():
        missing.append("标题")
    if not str(issue.get("description") or "").strip():
        missing.append("描述")
    labels = [str(item) for item in issue.get("labels") or []]
    if "viktor:auto" not in labels:
        missing.append("label viktor:auto")
    if not _kind_from_labels(labels):
        missing.append("label type:feature 或 type:bug")
    return missing


def _metadata_from_issue(issue: dict[str, Any]) -> dict[str, Any]:
    description = str(issue.get("description") or "")
    marker = "<!-- viktor-issue-intake"
    start = description.find(marker)
    if start < 0:
        return {}
    start += len(marker)
    end = description.find("-->", start)
    if end < 0:
        return {}
    raw = description[start:end].strip()
    try:
        value = json.loads(raw)
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _find_link(base_url: str, project_path: str, issue_iid: str) -> IssueIntakeLinkModel | None:
    db = SessionLocal()
    try:
        return (
            db.query(IssueIntakeLinkModel)
            .filter(
                IssueIntakeLinkModel.gitlab_base_url == base_url,
                IssueIntakeLinkModel.gitlab_project_path == project_path,
                IssueIntakeLinkModel.issue_iid == str(issue_iid),
            )
            .first()
        )
    finally:
        db.close()


def _create_link(
    *,
    project_id: str,
    repo_connector_id: str,
    issue: dict[str, Any],
    issue_project_url: str,
    source: str,
    kind: str,
    metadata: dict[str, Any] | None = None,
    routed: list[dict[str, str]] | None = None,
) -> str:
    project_path = GitLabClient.extract_project_path(issue_project_url)
    base_url = gitlab_config.resolve_base_url(issue_project_url)
    iid = str(issue.get("iid") or "")
    link_id = _new_id("il")
    labels = [str(item) for item in issue.get("labels") or []]
    assignees = issue.get("assignees") or ([] if not issue.get("assignee") else [issue.get("assignee")])
    dedupe_key = hashlib.sha256(f"{base_url}|{project_path}|{iid}".encode("utf-8")).hexdigest()[:32]
    meta = metadata or _metadata_from_issue(issue)
    result: dict[str, Any] = {}
    if routed:
        result["routed"] = routed
    target_branch = str(meta.get("target_branch") or "").strip()
    if target_branch:
        result["target_branch"] = target_branch
    reporter_mobile = str(meta.get("reporter_mobile") or "").strip()
    if reporter_mobile:
        result["reporter_mobile"] = reporter_mobile
    row = IssueIntakeLinkModel(
        link_id=link_id,
        project_id=project_id,
        repo_connector_id=repo_connector_id,
        source=source,
        kind=kind,
        reporter=_reporter_from_issue(issue, meta),
        title=str(issue.get("title") or "")[:512],
        description=str(issue.get("description") or ""),
        status="issue_created",
        stage="issue_created",
        message="GitLab issue 已创建/发现",
        gitlab_base_url=base_url,
        gitlab_project_path=project_path,
        gitlab_project_id=str(issue.get("project_id") or ""),
        issue_id=str(issue.get("id") or ""),
        issue_iid=iid,
        issue_url=str(issue.get("web_url") or ""),
        issue_state=str(issue.get("state") or ""),
        issue_labels=labels,
        issue_payload=issue,
        assignees=assignees,
        dedupe_key=dedupe_key,
        result=result,
    )
    db = SessionLocal()
    try:
        db.add(row)
        db.commit()
        return link_id
    except IntegrityError:
        db.rollback()
        existing = _find_link(base_url, project_path, iid)
        if existing:
            return existing.link_id
        raise
    finally:
        db.close()


def _update_link_from_issue(link_id: str, issue: dict[str, Any], *, status: str, stage: str, message: str) -> None:
    db = SessionLocal()
    try:
        row = db.get(IssueIntakeLinkModel, link_id)
        if not row:
            return
        row.title = str(issue.get("title") or row.title)[:512]
        row.description = str(issue.get("description") or row.description)
        row.issue_state = str(issue.get("state") or row.issue_state)
        row.issue_labels = [str(item) for item in issue.get("labels") or []]
        row.issue_payload = issue
        row.status = status
        row.stage = stage
        row.message = message
        db.commit()
    finally:
        db.close()


def _get_link_model(link_id: str) -> IssueIntakeLinkModel | None:
    db = SessionLocal()
    try:
        return db.get(IssueIntakeLinkModel, link_id)
    finally:
        db.close()


def _set_link_status(
    link_id: str,
    status: str,
    stage: str,
    message: str,
    *,
    last_error: str = "",
    result_patch: dict[str, Any] | None = None,
) -> None:
    db = SessionLocal()
    try:
        row = db.get(IssueIntakeLinkModel, link_id)
        if not row:
            return
        row.status = status
        row.stage = stage
        row.message = message
        if last_error:
            row.last_error = last_error
        if result_patch:
            row.result = {**(row.result or {}), **result_patch}
        db.commit()
    finally:
        db.close()


def _fail_link(link_id: str, message: str) -> None:
    _set_link_status(link_id, "failed", "failed", message, last_error=message)
    emit_issue_event(link_id, "failed", message, {"error": message}, stage="failed")
    _comment_issue_for_link(link_id, f"Viktor 处理失败：{message}")


def _reporter_from_issue(issue: dict[str, Any], metadata: dict[str, Any] | None = None) -> str:
    meta_reporter = str((metadata or {}).get("submitter_display_name") or "").strip()
    if meta_reporter:
        return meta_reporter
    author = issue.get("author") if isinstance(issue.get("author"), dict) else {}
    return str(author.get("name") or author.get("username") or "").strip()


def _needs_info_note(link_id: str, missing: list[str]) -> str:
    return (
        f"Viktor 无法接单，跟踪 ID: `{link_id}`。\n\n"
        "请补充以下信息后移除 `viktor:needs-info` 或重新触发扫描：\n"
        + "\n".join(f"- {item}" for item in missing)
    )


def _coding_requirement(
    link: IssueIntakeLinkModel,
    *,
    this_repo: str = "",
    routed: list[dict[str, Any]] | None = None,
) -> str:
    target_branch = str((link.result or {}).get("target_branch") or "").strip()
    target_branch_line = f"- 目标分支: {target_branch}\n" if target_branch else ""
    this_repo = this_repo or link.repo_connector_id
    repo_line = f"- 目标仓库: {this_repo}\n" if this_repo else ""
    multi = [str(r.get("repo_connector_id") or "").strip() for r in (routed or []) if str(r.get("repo_connector_id") or "").strip()]
    coordination_line = ""
    if len(multi) > 1:
        coordination_line = (
            f"- 多仓协同: 本需求已被自动路由到多个仓库（{'、'.join(multi)}），"
            f"本任务只负责仓库 `{this_repo}` 内的改动；其它仓库由各自独立的 Coding Task 处理，无需在本仓库改动它们。\n"
        )
    # blueprint 已确认的跨仓接口契约（若有）：注入本仓相关部分，避免前后端各猜 schema。
    contract_section = ""
    _contracts = (link.result or {}).get("blueprint", {}).get("contracts") if isinstance(link.result, dict) else None
    if _contracts:
        try:
            from core.issue_blueprint import render_contracts_for_repo
            _block = render_contracts_for_repo(_contracts, this_repo)
            if _block:
                contract_section = "\n" + _block
        except Exception:  # noqa: BLE001
            contract_section = ""
    return f"""[GitLab Issue Intake] {link.title}

## 跟踪信息
- Viktor tracking id: {link.coding_task_id or link.link_id}
- GitLab issue: {link.issue_url}
- Issue IID: {link.issue_iid}
- 类型: {link.kind}
- 提交人: {link.reporter or '-'}
{repo_line}{coordination_line}{target_branch_line}
{contract_section}
## Issue 原文
{link.description}
"""


def _source_issue_payload(link: IssueIntakeLinkModel, *, task_id: str) -> dict[str, Any]:
    return {
        "tracking_id": task_id,
        "link_id": link.link_id,
        "issue_iid": link.issue_iid,
        "issue_id": link.issue_id,
        "issue_url": link.issue_url,
        "issue_state": link.issue_state,
        "kind": link.kind,
        "source": link.source,
        "reporter": link.reporter,
        "repo_connector_id": link.repo_connector_id,
        "status": link.status,
        "target_branch": str((link.result or {}).get("target_branch") or ""),
    }


def _issue_project_url_for_link(link: IssueIntakeLinkModel) -> str:
    if link.gitlab_base_url and link.gitlab_project_path:
        return f"{link.gitlab_base_url.rstrip('/')}/{link.gitlab_project_path.strip('/')}"
    return _resolve_issue_target(link.project_id, link.repo_connector_id)["issue_project_url"]


def _comment_issue_for_link(link_id: str, body: str) -> None:
    link = _get_link_model(link_id)
    if not link or not link.issue_iid:
        return
    create_issue_note(project_url=_issue_project_url_for_link(link), issue_iid=link.issue_iid, body=body)


def _mark_mr_ready(link_id: str, task: dict[str, Any]) -> None:
    mr_url = str(task.get("mr_url") or "")
    report_id = str(task.get("report_id") or "")
    result = _as_dict(task.get("result"))
    patch = {
        "source_issue": _as_dict(result.get("source_issue")),
        "work_branch": task.get("work_branch") or result.get("branch") or "",
        "target_branch": task.get("target_branch") or result.get("target_branch") or "",
        "task_status": task.get("status"),
    }
    with _RESULT_LOCK:
        db = SessionLocal()
        try:
            row = db.get(IssueIntakeLinkModel, link_id)
            if row:
                row.status = "mr_created" if mr_url else "waiting_code_review"
                row.stage = "waiting_code_review"
                row.message = "MR 已创建，等待开发合并" if mr_url else "执行完成，等待代码审核"
                row.mr_url = mr_url
                row.report_id = report_id
                merged_result = {**(row.result or {}), **patch}
                tasks = list(merged_result.get("coding_tasks") or [])
                for t in tasks:
                    if t.get("coding_task_id") == row.coding_task_id and mr_url:
                        t["mr_url"] = mr_url
                if tasks:
                    merged_result["coding_tasks"] = tasks
                row.result = merged_result
                db.commit()
        finally:
            db.close()
    emit_issue_event(link_id, "mr_ready", "Coding Task 已进入代码审核", {"mr_url": mr_url, "report_id": report_id}, stage="waiting_code_review")
    if mr_url:
        task_id = str(task.get("task_id") or task.get("id") or "")
        task_ref = _coding_task_markdown(task_id, str(task.get("project_id") or ""))
        # issue 评论按本仓即时记一条（不聚合，便于追溯）。
        _comment_issue_for_link(link_id, f"Viktor 已创建 MR：{mr_url}\n\n跟踪 ID / Coding Task: {task_ref}。")
        # 钉钉通知聚合：多仓时等全部仓库的 MR 都 ready 才发一条，列全部 MR 并 @ 各仓维护开发。
        _maybe_notify_all_mr_ready(link_id)


def _repo_maintainer_mobile(project_id: str, repo_connector_id: str) -> str:
    """取某仓库固定维护开发的钉钉手机号（registry 内存单例）。"""
    if not repo_connector_id:
        return ""
    conn = registry.get_repository_connector(project_id, repo_connector_id)
    return str(getattr(conn, "maintainer_mobile", "") or "").strip() if conn else ""


def _backfill_missing_mr_urls(db: Any, tasks: list[dict[str, Any]]) -> bool:
    """对 coding_tasks 中 mr_url 为空的子项，按 coding_task_id 从 task 表回查补齐。

    watcher 时序错乱 / 多副本下整列写回丢更新，会让某些 task 的 mr_url 在 link.result
    里空着，而 task 表其实已有真实 MR。聚合通知前据 task 表自愈，避免通知永久卡住。
    返回是否发生了补齐（用于决定是否要回写 result）。"""
    changed = False
    for t in tasks:
        if str(t.get("mr_url") or "").strip():
            continue
        tid = str(t.get("coding_task_id") or "").strip()
        if not tid:
            continue
        task_row = db.get(CodingTaskModel, tid)
        if not task_row:
            continue
        real_mr = str(getattr(task_row, "mr_url", "") or "").strip()
        if not real_mr:
            real_mr = str(_as_dict(task_row.result).get("mr_url") or "").strip()
        if real_mr:
            t["mr_url"] = real_mr
            changed = True
    return changed


def _maybe_notify_all_mr_ready(link_id: str) -> None:
    """当 link 下所有 coding_task 都已出 MR 时，发一条聚合钉钉通知（列全部 MR + @ 全部维护开发）。

    幂等：用 result['mr_ready_notified'] 标记防止 webhook 重投导致重复发。
    """
    with _RESULT_LOCK:
        db = SessionLocal()
        try:
            row = db.get(IssueIntakeLinkModel, link_id)
            if not row:
                return
            result = dict(row.result or {})
            if result.get("mr_ready_notified"):
                return
            tasks = list(result.get("coding_tasks") or [])
            # 无 coding_tasks 数组（理论不该发生）时退回单仓：用 row.mr_url。
            if not tasks and row.mr_url:
                tasks = [{"repo_connector_id": row.repo_connector_id, "mr_url": row.mr_url}]
            if not tasks:
                return
            # 空 mr_url 的 task 先据 task 表回查补齐（自愈写丢/时序乱）。
            if _backfill_missing_mr_urls(db, tasks):
                result["coding_tasks"] = tasks
                row.result = result
                db.commit()
            # 仍有仓库没出 MR → 先不发，等最后一个到齐。
            if any(not str(t.get("mr_url") or "").strip() for t in tasks):
                return
            project_id = row.project_id
            mr_lines = []
            mobiles: list[str] = []
            for t in tasks:
                rid = str(t.get("repo_connector_id") or "").strip() or "-"
                mr = str(t.get("mr_url") or "").strip()
                mr_lines.append(f"- `{rid}`: {mr}")
                mobiles.append(_repo_maintainer_mobile(project_id, rid))
        finally:
            db.close()
        # 发送在标记之前：只有发送成功才置 mr_ready_notified，避免发送失败后永不重发。
        # 失败时不置标记 → 下个周期/下次触发会重试，且 send 已把它入 DLQ 兜底。
        body = "\n".join(mr_lines)
        ok = _notify_for_link(link_id, "Viktor 已创建 MR", body, extra_mobiles=mobiles)
        if ok:
            db = SessionLocal()
            try:
                row = db.get(IssueIntakeLinkModel, link_id)
                if row:
                    result = dict(row.result or {})
                    result["mr_ready_notified"] = True
                    row.result = result
                    db.commit()
            finally:
                db.close()


def _close_issue_for_link(link_id: str) -> None:
    link = _get_link_model(link_id)
    if not link:
        return
    issue_project_url = _issue_project_url_for_link(link)
    try:
        create_issue_note(
            project_url=issue_project_url,
            issue_iid=link.issue_iid,
            body=f"MR 已合并，Viktor 自动关闭该 issue。\n\n跟踪 ID / Coding Task: {_coding_task_markdown(link.coding_task_id, link.project_id)}。",
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("[issue-intake] comment merge close failed link={}: {}", link_id, e)
    try:
        add_issue_labels(project_url=issue_project_url, issue_iid=link.issue_iid, labels=["viktor:merged"])
    except Exception as e:  # noqa: BLE001
        logger.warning("[issue-intake] add merged label failed link={}: {}", link_id, e)
    close_issue(project_url=issue_project_url, issue_iid=link.issue_iid)
    _set_link_status(link_id, "issue_closed", "issue_closed", "MR 已合并，GitLab issue 已关闭")
    emit_issue_event(link_id, "issue_closed", "MR 已合并，GitLab issue 已关闭", {"mr_url": link.mr_url}, stage="issue_closed")
    # MR 已合并：@ 需求人（提交时存进 result['reporter_mobile']）。
    reporter_mobile = str(_as_dict(link.result).get("reporter_mobile") or "").strip()
    _notify_for_link(
        link_id,
        "Viktor 已关闭 Issue",
        f"- Issue: {link.issue_url}\n- MR: {link.mr_url or '-'}",
        extra_mobiles=[reporter_mobile] if reporter_mobile else None,
    )


def _notify_for_link(
    link_id: str, title: str, body: str, *, extra_mobiles: list[str] | None = None
) -> bool:
    """发送 link 级钉钉通知。返回是否发送成功（无 link/无 webhook 视为非成功 → False）。"""
    link = _get_link_model(link_id)
    if not link:
        return False
    cfg = _project_config(link.project_id)
    notification = _as_dict(cfg.notification if cfg else {})
    webhook_url = str(notification.get("webhook_url") or "").strip()
    if not webhook_url:
        return False
    at_mobiles = _as_list(notification.get("at_mobiles"))
    assignee_mobiles = _as_dict(cfg.assignee_mobiles if cfg else {})
    for assignee in _as_list(link.assignees):
        if isinstance(assignee, dict):
            username = str(assignee.get("username") or assignee.get("name") or "").strip()
            mobile = assignee_mobiles.get(username)
            if mobile:
                at_mobiles.append(str(mobile))
    # 直传手机号（开发维护人 / 需求人）：已是现成号码，无需映射。
    for mobile in extra_mobiles or []:
        m = str(mobile or "").strip()
        if m:
            at_mobiles.append(m)
    # 去重保序，避免同一人被 @ 多次。
    at_mobiles = list(dict.fromkeys(at_mobiles))
    try:
        send_dingtalk_markdown_sync(
            webhook_url=webhook_url,
            sign_secret=str(notification.get("sign_secret") or ""),
            title=title,
            text=_build_link_notification_text(link, title, body),
            at_mobiles=at_mobiles,
            at_all=bool(notification.get("at_all")),
        )
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("[issue-intake] notify failed link={}: {}", link_id, e)
        return False


def _notify_project_dingtalk(project_id: str, title: str, text: str, *, extra_mobiles: list[str] | None = None) -> None:
    """按项目通知配置发钉钉（无 link 的独立 coding task 用）。"""
    cfg = _project_config(project_id)
    notification = _as_dict(cfg.notification if cfg else {})
    webhook_url = str(notification.get("webhook_url") or "").strip()
    if not webhook_url:
        return
    at_mobiles = _as_list(notification.get("at_mobiles"))
    for m in extra_mobiles or []:
        m = str(m or "").strip()
        if m:
            at_mobiles.append(m)
    at_mobiles = list(dict.fromkeys(at_mobiles))
    try:
        send_dingtalk_markdown_sync(
            webhook_url=webhook_url, sign_secret=str(notification.get("sign_secret") or ""),
            title=title, text=text, at_mobiles=at_mobiles, at_all=bool(notification.get("at_all")),
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("[coding-gate] notify failed project={}: {}", project_id, e)


def notify_coding_task_gate(task_id: str, gate: str) -> None:
    """coding task 进入人审 gate 时发钉钉提醒。gate ∈ {clarification, plan_review, code_review}。

    有 link 的 task：走 link 通知通道（@ 仓库维护人 + 需求侧）；code_review 由 link 级
    聚合通知（_maybe_notify_all_mr_ready）覆盖，这里跳过避免重复。独立 task：项目级通知。
    """
    titles = {
        "clarification": "Viktor 待澄清",
        "plan_review": "Viktor 待审核 Plan",
        "code_review": "Viktor 待处理 MR Review",
    }
    title = titles.get(gate)
    if not title:
        return
    db = SessionLocal()
    try:
        row = db.get(CodingTaskModel, task_id)
        if not row:
            return
        project_id = row.project_id
        repo = row.repo_connector_id or ""
        head = next((ln for ln in (row.requirement or "").splitlines() if ln.strip()), "").strip()[:60]
        result = row.result if isinstance(row.result, dict) else {}
        src = result.get("source_issue") if isinstance(result.get("source_issue"), dict) else {}
        link_id = str((src or {}).get("link_id") or "")
        mr_url = row.mr_url or ""
    finally:
        db.close()

    body_lines = [f"- 仓库: `{repo or '-'}`", f"- 任务: {head or task_id}"]
    if gate == "code_review" and mr_url:
        body_lines.append(f"- MR: {mr_url}")
    body_lines.append(f"- 工作台: {_coding_task_url(task_id, project_id)}")
    body = "\n".join(body_lines)
    mobiles = [_repo_maintainer_mobile(project_id, repo)] if repo else []

    if link_id and _get_link_model(link_id):
        if gate == "code_review":
            return  # link 级聚合 MR 通知已覆盖
        _notify_for_link(link_id, title, body, extra_mobiles=mobiles)
    else:
        _notify_project_dingtalk(project_id, title, f"### {title}\n\n{body}", extra_mobiles=mobiles)


def _feature_template() -> str:
    return """# 标题

## 背景/问题

## 目标用户或使用场景

## 业务目标

## 需求范围
- 做什么：
- 不做什么：

## 验收标准
-

## 影响页面/API/业务流程

## 优先级/期望时间

## 提交人
"""


def _bug_template() -> str:
    return """# 标题

## 复现步骤
1.

## 实际结果

## 期望结果

## 环境

## 证据截图/日志

## 影响范围

## 提交人
"""


async def _noop() -> None:
    return None
