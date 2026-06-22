"""
GitLab 上下文自动导入 API 路由。

提供仓库分析任务的提交和查询接口。
"""
from typing import Optional

from fastapi import APIRouter, HTTPException
from loguru import logger
from pydantic import BaseModel

from core.database import SessionLocal
from core.models import GitLabTaskModel
from core.registry import registry
from gitlab.service import start_analyze_task

router = APIRouter(prefix="/api/v1/gitlab", tags=["GitLab"])


class AnalyzeRequest(BaseModel):
    """提交分析任务的请求体。"""
    project_id: str
    repo_url: str
    branch: str = "master"
    gitlab_token: Optional[str] = None


@router.post("/analyze", summary="提交仓库分析任务", status_code=202)
def submit_analyze(req: AnalyzeRequest) -> dict:
    """
    提交一个 GitLab 仓库分析任务。

    分析将在后台异步执行，完成后自动生成 context 写入对应项目。
    返回 task_id 用于查询任务进度。
    """
    project = registry.get_project(req.project_id)
    if not project:
        raise HTTPException(status_code=404, detail=f"项目 '{req.project_id}' 不存在，请先注册项目")

    task_id = start_analyze_task(
        project_id=req.project_id,
        repo_url=req.repo_url,
        branch=req.branch,
        gitlab_token=req.gitlab_token,
    )

    logger.info("[GitLab API] 分析任务已提交: task_id={}, project={}", task_id, req.project_id)
    return {
        "task_id": task_id,
        "project_id": req.project_id,
        "status": "pending",
        "message": "分析任务已提交，请通过 GET /api/v1/gitlab/tasks/{task_id} 查询进度",
    }


@router.get("/tasks/{task_id}", summary="查询分析任务状态")
def get_task(task_id: str) -> dict:
    """查询指定分析任务的状态和结果。"""
    db = SessionLocal()
    try:
        task = db.query(GitLabTaskModel).filter_by(task_id=task_id).first()
        if not task:
            raise HTTPException(status_code=404, detail=f"任务 '{task_id}' 不存在")
        return {
            "task_id": task.task_id,
            "project_id": task.project_id,
            "repo_url": task.repo_url,
            "branch": task.branch,
            "status": task.status,
            "message": task.message,
            "contexts_generated": task.contexts_generated or [],
            "created_at": task.created_at.isoformat() if task.created_at else None,
            "updated_at": task.updated_at.isoformat() if task.updated_at else None,
        }
    finally:
        db.close()


@router.get("/tasks", summary="查询项目的所有分析任务")
def list_tasks(project_id: Optional[str] = None) -> dict:
    """
    查询分析任务列表。

    可选传入 project_id 过滤特定项目的任务，不传则返回全部。
    """
    db = SessionLocal()
    try:
        query = db.query(GitLabTaskModel).order_by(GitLabTaskModel.created_at.desc())
        if project_id:
            query = query.filter_by(project_id=project_id)

        tasks = query.limit(50).all()
        return {
            "total": len(tasks),
            "tasks": [
                {
                    "task_id": t.task_id,
                    "project_id": t.project_id,
                    "repo_url": t.repo_url,
                    "branch": t.branch,
                    "status": t.status,
                    "message": t.message,
                    "contexts_generated": t.contexts_generated or [],
                    "created_at": t.created_at.isoformat() if t.created_at else None,
                    "updated_at": t.updated_at.isoformat() if t.updated_at else None,
                }
                for t in tasks
            ],
        }
    finally:
        db.close()
