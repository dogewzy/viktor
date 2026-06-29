"""GitLab Issue Intake API。"""
from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from core.auth import CurrentUser, get_current_user
from core.issue_intake_service import (
    generate_local_agent_skill,
    get_issue_intake_config,
    get_issue_intake_link,
    list_issue_intake_events,
    list_issue_intake_links,
    scan_project_issues,
    submit_local_agent_issue,
    upsert_issue_intake_config,
)

router = APIRouter(prefix="/api/v1/issue-intake", tags=["Issue Intake"])


class IssueIntakeTargetRequest(BaseModel):
    repo_connector_id: str
    issue_project_url: str = ""
    labels: list[str] = Field(default_factory=list)
    enabled: bool = True


class IssueIntakeConfigRequest(BaseModel):
    issue_project_url: str = ""
    default_repo_connector_id: str = ""
    default_labels: list[str] = Field(default_factory=list)
    targets: list[IssueIntakeTargetRequest] | None = None
    submit_token: str = ""
    notification: dict[str, Any] = Field(default_factory=dict)
    assignee_mobiles: dict[str, str] = Field(default_factory=dict)
    scan_interval_sec: int = 300
    enabled: bool = True


class IssueSubmitRequest(BaseModel):
    project_id: str
    submit_token: str = ""
    kind: str = "bug"
    title: str = Field(default="", max_length=255)
    description: str = Field(min_length=1)
    repo_connector_id: str = ""
    target_branch: str = ""
    reporter_display_name: str = ""
    # 需求人钉钉手机号：MR 合并后 @ 此人。强制必填（service 层校验）。
    reporter_mobile: str = ""
    labels: list[str] = Field(default_factory=list)
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    create_coding_task: bool = True


@router.get("/config/{project_id}", summary="查询项目 Issue Intake 配置")
def get_config(project_id: str, _: CurrentUser = Depends(get_current_user)) -> dict:
    cfg = get_issue_intake_config(project_id)
    if not cfg:
        raise HTTPException(status_code=404, detail="项目尚未配置 Issue Intake")
    return {"ok": True, "config": cfg}


@router.put("/config/{project_id}", summary="保存项目 Issue Intake 配置")
def put_config(
    project_id: str,
    body: IssueIntakeConfigRequest,
    _: CurrentUser = Depends(get_current_user),
) -> dict:
    try:
        cfg = upsert_issue_intake_config(project_id, body.model_dump())
        return {"ok": True, "config": cfg}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/config/{project_id}/local-agent-skill", summary="生成本地 Agent 录入 Skill")
def get_local_agent_skill(
    project_id: str,
    kind: str = Query(default="feature", pattern="^(feature|bug)$"),
    repo_connector_id: str = "",
    current: CurrentUser = Depends(get_current_user),
) -> dict:
    try:
        return {
            "ok": True,
            **generate_local_agent_skill(
                project_id,
                kind=kind,
                repo_connector_id=repo_connector_id,
                reporter_display_name=current.display_name or current.username,
                reporter_mobile=current.mobile,
            ),
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/submit", summary="本地 Agent / WebChat 提交 Issue")
def submit_issue(body: IssueSubmitRequest) -> dict:
    try:
        link = submit_local_agent_issue(
            project_id=body.project_id.strip(),
            submit_token=body.submit_token.strip(),
            kind=body.kind,
            title=body.title,
            description=body.description,
            repo_connector_id=body.repo_connector_id.strip(),
            target_branch=body.target_branch.strip(),
            reporter_display_name=body.reporter_display_name.strip(),
            reporter_mobile=body.reporter_mobile.strip(),
            labels=body.labels,
            attachments=body.attachments,
            source="local_agent",
            require_token=True,
            create_coding_task=body.create_coding_task,
        )
        return {"ok": True, "link": link}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/config/{project_id}/scan", summary="手动扫描项目 GitLab Issues")
def post_scan(
    project_id: str,
    repo_connector_id: str = "",
    _: CurrentUser = Depends(get_current_user),
) -> dict:
    try:
        return scan_project_issues(project_id, repo_connector_id=repo_connector_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/links", summary="Issue Intake 处理列表")
def get_links(
    project_id: str | None = None,
    status: str | None = None,
    q: str | None = None,
    reported_by_me: bool = False,
    limit: int = Query(default=100, ge=1, le=300),
    offset: int = Query(default=0, ge=0),
    current: CurrentUser = Depends(get_current_user),
) -> dict:
    if reported_by_me and not current.mobile.strip():
        return {"items": [], "total": 0, "limit": limit, "offset": offset}
    return list_issue_intake_links(
        project_id=project_id,
        status=status,
        q=q,
        reporter_mobile=current.mobile if reported_by_me else None,
        limit=limit,
        offset=offset,
    )


@router.get("/links/{link_id}", summary="Issue Intake 详情")
def get_link(link_id: str, _: CurrentUser = Depends(get_current_user)) -> dict:
    link = get_issue_intake_link(link_id)
    if not link:
        raise HTTPException(status_code=404, detail="Issue Intake link 不存在")
    return {"ok": True, "link": link}


class BlueprintReviewRequest(BaseModel):
    decision: str = Field(default="approved", pattern="^(approved|cancelled)$")
    repos: list[dict[str, Any]] = Field(default_factory=list)
    contracts: list[dict[str, Any]] = Field(default_factory=list)
    test_plan: dict[str, Any] = Field(default_factory=dict)
    reviewer: str = ""
    comment: str = ""


@router.post("/links/{link_id}/blueprint/review", summary="确认/取消多仓改动蓝图")
def review_blueprint(link_id: str, body: BlueprintReviewRequest, current: CurrentUser = Depends(get_current_user)) -> dict:
    if not get_issue_intake_link(link_id):
        raise HTTPException(status_code=404, detail="Issue Intake link 不存在")
    from core.temporal import trigger
    reviewer = body.reviewer.strip() or current.mobile or current.display_name or current.username
    ok = trigger.signal_issue_link_sync(
        link_id, "blueprint_reviewed",
        body.decision, body.repos, body.contracts, reviewer, body.comment, body.test_plan,
    )
    if not ok:
        raise HTTPException(status_code=409, detail="蓝图确认失败：Temporal 未启用或父流程不在编排中")
    return {"ok": True}


@router.get("/links/{link_id}/events", summary="Issue Intake 事件")
def get_link_events(
    link_id: str,
    after_seq: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=1000),
    _: CurrentUser = Depends(get_current_user),
) -> dict:
    if not get_issue_intake_link(link_id):
        raise HTTPException(status_code=404, detail="Issue Intake link 不存在")
    return list_issue_intake_events(link_id, after_seq=after_seq, limit=limit)


@router.get("/links/{link_id}/events/stream", summary="Issue Intake 事件 SSE")
async def stream_link_events(
    link_id: str,
    after_seq: int = 0,
    _: CurrentUser = Depends(get_current_user),
) -> StreamingResponse:
    if not get_issue_intake_link(link_id):
        raise HTTPException(status_code=404, detail="Issue Intake link 不存在")

    async def gen() -> AsyncIterator[str]:
        seq = after_seq
        idle = 0
        while idle < 3600:
            data = list_issue_intake_events(link_id, after_seq=seq, limit=100)
            items = data["items"]
            if items:
                idle = 0
                for item in items:
                    seq = max(seq, int(item["seq"]))
                    yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
            else:
                idle += 1
                yield ": heartbeat\n\n"
            link = get_issue_intake_link(link_id)
            if link and link.get("status") in {"failed", "issue_closed", "completed"} and not items:
                yield "data: [DONE]\n\n"
                break
            await asyncio.sleep(1.0)

    return StreamingResponse(gen(), media_type="text/event-stream")
