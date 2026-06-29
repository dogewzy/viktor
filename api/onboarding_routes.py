"""项目接入审核 API。

面向前端「开始接入 Viktor」流程：提交仓库、审核候选知识、采纳并落地正式项目。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from core.database import SessionLocal
from core.models import OnboardingArtifactModel, OnboardingTaskModel
from core.onboarding_service import apply_artifacts, start_onboarding_task, _prepare_artifact_content

router = APIRouter(prefix="/api/v1/onboarding", tags=["项目接入"])


class OnboardingStartRequest(BaseModel):
    """创建项目接入审核任务。"""

    project_id: str
    repo_url: Any = ""
    repo_urls: list[Any] = Field(default_factory=list)
    repositories: list[Any] = Field(default_factory=list)
    repos: list[Any] = Field(default_factory=list)
    branch: str = "master"
    project_name: str = ""
    project_description: str = ""
    analysis_level: str = Field(default="standard", pattern="^(quick|standard|deep)$")
    connector_config_files: list[str] = Field(
        default_factory=list,
        description="可选：由注册者指定的配置文件路径/文件名/glob，用于让 LLM 提取数据库和外部连接器候选",
    )
    profile: dict[str, Any] = Field(default_factory=dict)


class ArtifactUpdateRequest(BaseModel):
    """更新候选产物。"""

    title: Optional[str] = None
    content: Optional[str] = None
    payload: Optional[dict[str, Any]] = None
    status: Optional[str] = Field(default=None, pattern="^(pending|accepted|rejected|applied)$")


class ApplyArtifactsRequest(BaseModel):
    """采纳候选产物并落地正式项目。"""

    artifact_ids: list[str] | None = None


def _dt(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return None


def _task_to_dict(row: OnboardingTaskModel) -> dict[str, Any]:
    profile = row.profile or {}
    repositories = profile.get("repositories") if isinstance(profile.get("repositories"), list) else []
    return {
        "id": row.task_id,
        "task_id": row.task_id,
        "project_id": row.project_id,
        "repo_url": row.repo_url,
        "repo_urls": profile.get("repo_urls") or [row.repo_url],
        "repositories": repositories,
        "branch": row.branch,
        "status": row.status,
        "stage": row.stage,
        "message": row.message,
        "analysis_level": row.analysis_level,
        "profile": profile,
        "stats": row.stats or {},
        "created_at": _dt(row.created_at),
        "updated_at": _dt(row.updated_at),
    }


def _artifact_to_dict(row: OnboardingArtifactModel) -> dict[str, Any]:
    return {
        "id": row.artifact_id,
        "artifact_id": row.artifact_id,
        "task_id": row.task_id,
        "project_id": row.project_id,
        "artifact_type": row.artifact_type,
        "target_id": row.target_id,
        "title": row.title,
        "content": row.content,
        "payload": row.payload or {},
        "status": row.status,
        "created_at": _dt(row.created_at),
        "updated_at": _dt(row.updated_at),
    }


@router.post("/tasks", summary="开始项目接入审核")
def create_onboarding_task(req: OnboardingStartRequest) -> dict:
    if not req.project_id.strip():
        raise HTTPException(status_code=400, detail="project_id 必填")
    repo_url = req.repo_url.strip() if isinstance(req.repo_url, str) else ""
    repo_urls = [item.strip() if isinstance(item, str) else item for item in req.repo_urls]
    repositories = list(req.repositories)
    if isinstance(req.repo_url, list):
        repo_urls.extend(req.repo_url)
    elif isinstance(req.repo_url, dict):
        repositories.insert(0, req.repo_url)
    try:
        task_id = start_onboarding_task(
            project_id=req.project_id.strip(),
            repo_url=repo_url,
            repo_urls=repo_urls,
            repositories=repositories,
            repos=req.repos,
            branch=req.branch.strip() or "master",
            project_name=req.project_name.strip(),
            project_description=req.project_description.strip(),
            analysis_level=req.analysis_level,
            profile=req.profile,
            connector_config_files=[item.strip() for item in req.connector_config_files if item.strip()],
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True, "task_id": task_id, "status": "created", "message": "接入分析已提交，完成后请审核并落地"}


@router.get("/tasks", summary="接入审核任务列表")
def list_onboarding_tasks(
    project_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    db = SessionLocal()
    try:
        query = db.query(OnboardingTaskModel)
        if project_id:
            query = query.filter(OnboardingTaskModel.project_id == project_id)
        total = query.count()
        rows = query.order_by(OnboardingTaskModel.created_at.desc()).offset(offset).limit(limit).all()
        return {"items": [_task_to_dict(row) for row in rows], "total": total, "limit": limit, "offset": offset}
    finally:
        db.close()


@router.get("/tasks/{task_id}", summary="接入审核任务详情")
def get_onboarding_task(task_id: str) -> dict:
    db = SessionLocal()
    try:
        row = db.get(OnboardingTaskModel, task_id)
        if not row:
            raise HTTPException(status_code=404, detail="接入审核任务不存在")
        return _task_to_dict(row)
    finally:
        db.close()


@router.get("/tasks/{task_id}/artifacts", summary="候选产物列表")
def list_artifacts(task_id: str) -> dict:
    db = SessionLocal()
    try:
        rows = (
            db.query(OnboardingArtifactModel)
            .filter(OnboardingArtifactModel.task_id == task_id)
            .order_by(OnboardingArtifactModel.artifact_type, OnboardingArtifactModel.created_at)
            .all()
        )
        return {"items": [_artifact_to_dict(row) for row in rows], "total": len(rows)}
    finally:
        db.close()


@router.patch("/artifacts/{artifact_id}", summary="编辑候选产物")
def update_artifact(artifact_id: str, req: ArtifactUpdateRequest) -> dict:
    db = SessionLocal()
    try:
        row = db.get(OnboardingArtifactModel, artifact_id)
        if not row:
            raise HTTPException(status_code=404, detail="候选产物不存在")
        if req.title is not None:
            row.title = req.title
        if req.content is not None:
            stored_content, stored_payload = _prepare_artifact_content(
                row.artifact_type,
                req.title if req.title is not None else row.title,
                req.content,
                dict(req.payload if req.payload is not None else (row.payload or {})),
            )
            row.content = stored_content
            row.payload = stored_payload
        if req.payload is not None:
            if req.content is None:
                row.payload = req.payload
        if req.status is not None:
            row.status = req.status
        db.commit()
        db.refresh(row)
        return {"ok": True, "artifact": _artifact_to_dict(row)}
    finally:
        db.close()


@router.post("/tasks/{task_id}/apply", summary="采纳候选产物并落地")
def apply_task_artifacts(task_id: str, req: ApplyArtifactsRequest) -> dict:
    try:
        result = apply_artifacts(task_id, req.artifact_ids)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return {"ok": True, **result}
