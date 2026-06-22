"""Coding Agent API：创建、观察、控制后台 coding task。"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from core.coding_service import (
    answer_clarification,
    append_message,
    complete_code_review,
    continue_execution,
    get_latest_diff,
    get_task,
    list_attempts,
    list_events,
    list_tasks,
    request_plan_revision,
    review_plan,
    resume_coding_task,
    start_execution,
    start_coding_task,
    submit_automated_review_response,
    update_control,
)
from core.auth import CurrentUser, get_current_user
from core.temporal import trigger

router = APIRouter(prefix="/api/v1/coding", tags=["Coding Agent"])


def _current_reviewer(current: CurrentUser, explicit: str = "") -> str:
    return explicit.strip() or current.mobile or current.display_name or current.username


class CodingTaskCreateRequest(BaseModel):
    project_id: str
    requirement: str = Field(min_length=1)
    repo_connector_id: str = ""
    target_branch: str = ""
    policy: dict[str, Any] = Field(default_factory=dict)
    create_mr: bool | None = None
    created_by: str = ""
    created_by_mobile: str = ""


class CodingMessageRequest(BaseModel):
    message: str = Field(min_length=1)


class PlanReviewRequest(BaseModel):
    comment: str = ""
    reviewer: str = ""


class ClarificationAnswerRequest(BaseModel):
    answers: dict[str, Any] = Field(default_factory=dict)
    reviewer: str = ""


class ContinueExecutionRequest(BaseModel):
    comment: str = ""


class CodeReviewCompleteRequest(BaseModel):
    comment: str = ""
    reviewer: str = ""


class AutomatedReviewItemResponse(BaseModel):
    number: int
    decision: str = Field(pattern="^(accept|custom|ignore)$")
    comment: str = ""


class AutomatedReviewResponseRequest(BaseModel):
    responses: list[AutomatedReviewItemResponse] = Field(default_factory=list)
    reviewer: str = ""
    additional_comment: str = ""


@router.post("/tasks", summary="创建 Coding Task")
def create_task(req: CodingTaskCreateRequest, current: CurrentUser = Depends(get_current_user)) -> dict:
    try:
        created_by = req.created_by.strip() or current.display_name or current.username
        created_by_mobile = req.created_by_mobile.strip() or current.mobile
        task_id = start_coding_task(
            project_id=req.project_id.strip(),
            requirement=req.requirement.strip(),
            repo_connector_id=req.repo_connector_id.strip(),
            target_branch=req.target_branch.strip(),
            policy=req.policy,
            create_mr=req.create_mr,
            created_by=created_by,
            created_by_mobile=created_by_mobile,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # Temporal 接管：手动创建的 task 也交给 CodingTaskWorkflow 编排（幂等）。
    trigger.start_coding_task_sync(task_id)
    task = get_task(task_id) or {}
    return {"ok": True, "task_id": task_id, "status": task.get("status") or "created"}


@router.get("/tasks", summary="Coding Task 列表")
def get_tasks(
    project_id: str | None = None,
    pending_for_mobile: str | None = None,
    pending_for_me: bool = False,
    created_by_me: bool = False,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current: CurrentUser = Depends(get_current_user),
) -> dict:
    if (pending_for_me or created_by_me) and not current.mobile.strip():
        return {"items": [], "total": 0, "limit": limit, "offset": offset}
    pending_mobile = current.mobile.strip() if pending_for_me else (pending_for_mobile or "")
    created_mobile = current.mobile.strip() if created_by_me else ""
    return list_tasks(
        project_id=project_id,
        limit=limit,
        offset=offset,
        pending_for_mobile=pending_mobile,
        created_by_mobile=created_mobile,
    )


@router.get("/tasks/{task_id}", summary="Coding Task 详情")
def get_task_detail(task_id: str) -> dict:
    row = get_task(task_id)
    if not row:
        raise HTTPException(status_code=404, detail="Coding Task 不存在")
    return row


@router.get("/tasks/{task_id}/attempts", summary="Coding Task attempts")
def get_task_attempts(task_id: str) -> dict:
    if not get_task(task_id):
        raise HTTPException(status_code=404, detail="Coding Task 不存在")
    return list_attempts(task_id)


@router.get("/tasks/{task_id}/events", summary="Coding Task 事件")
def get_task_events(
    task_id: str,
    after_seq: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=1000),
) -> dict:
    if not get_task(task_id):
        raise HTTPException(status_code=404, detail="Coding Task 不存在")
    return list_events(task_id, after_seq=after_seq, limit=limit)


@router.get("/tasks/{task_id}/events/stream", summary="Coding Task 事件 SSE")
async def stream_task_events(task_id: str, after_seq: int = 0) -> StreamingResponse:
    if not get_task(task_id):
        raise HTTPException(status_code=404, detail="Coding Task 不存在")

    async def gen():
        seq = after_seq
        idle = 0
        while idle < 3600:
            data = list_events(task_id, after_seq=seq, limit=100)
            items = data["items"]
            if items:
                idle = 0
                for item in items:
                    seq = max(seq, int(item["seq"]))
                    yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
            else:
                idle += 1
                yield ": heartbeat\n\n"
            task = get_task(task_id)
            if task and task.get("status") in {"waiting_clarification", "waiting_plan_review", "waiting_code_review", "completed", "failed", "cancelled", "plan_rejected"} and not items:
                yield "data: [DONE]\n\n"
                break
            await asyncio.sleep(1.0)

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.get("/tasks/{task_id}/diff", summary="Coding Task 当前 diff")
def get_task_diff(task_id: str) -> dict:
    if not get_task(task_id):
        raise HTTPException(status_code=404, detail="Coding Task 不存在")
    return get_latest_diff(task_id)


def _signaled_response(task_id: str) -> dict:
    """Temporal 接管时控制端点的统一返回：signal 已发，回当前 task 快照。"""
    task = get_task(task_id)
    return {"ok": True, "task_id": task_id, "status": (task or {}).get("status"), "task": task}


@router.post("/tasks/{task_id}/plan/approve", summary="兼容：审批通过 plan")
def approve_plan(
    task_id: str,
    req: PlanReviewRequest | None = None,
    current: CurrentUser = Depends(get_current_user),
) -> dict:
    comment = req.comment if req else ""
    reviewer = _current_reviewer(current, req.reviewer if req else "")
    # 接管模式：发 signal，由 workflow 应用审核并（一步走）自动启动执行
    if trigger.signal_coding_task_sync(task_id, "plan_reviewed", "approved", comment, reviewer):
        return _signaled_response(task_id)
    try:
        task = review_plan(task_id, decision="approved", comment=comment, reviewer=reviewer)
        return {"ok": True, "task_id": task_id, "status": task["status"], "task": task}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/tasks/{task_id}/clarification/answer", summary="提交 Plan 前置澄清答案")
def post_clarification_answer(
    task_id: str,
    req: ClarificationAnswerRequest,
    current: CurrentUser = Depends(get_current_user),
) -> dict:
    reviewer = _current_reviewer(current, req.reviewer)
    if trigger.signal_coding_task_sync(task_id, "clarification_answered", req.answers, reviewer):
        return _signaled_response(task_id)
    try:
        task = answer_clarification(task_id, answers=req.answers, reviewer=reviewer)
        return {"ok": True, "task_id": task_id, "status": task["status"], "task": task}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/tasks/{task_id}/plan/reject", summary="兼容：驳回 plan")
def reject_plan(
    task_id: str,
    req: PlanReviewRequest | None = None,
    current: CurrentUser = Depends(get_current_user),
) -> dict:
    comment = req.comment if req else ""
    reviewer = _current_reviewer(current, req.reviewer if req else "")
    if trigger.signal_coding_task_sync(task_id, "plan_reviewed", "rejected", comment, reviewer):
        return _signaled_response(task_id)
    try:
        task = review_plan(task_id, decision="rejected", comment=comment, reviewer=reviewer)
        return {"ok": True, "task_id": task_id, "status": task["status"], "task": task}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/tasks/{task_id}/plan/revise", summary="要求 Agent 根据审核意见重新生成 plan")
def revise_plan(
    task_id: str,
    req: PlanReviewRequest,
    current: CurrentUser = Depends(get_current_user),
) -> dict:
    reviewer = _current_reviewer(current, req.reviewer)
    if trigger.signal_coding_task_sync(task_id, "plan_revision_requested", req.comment, reviewer):
        return _signaled_response(task_id)
    try:
        task = request_plan_revision(task_id, comment=req.comment, reviewer=reviewer)
        return {"ok": True, "task_id": task_id, "status": task["status"], "task": task}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/tasks/{task_id}/execution/start", summary="兼容：启动审批后的 workspace")
def post_start_execution(task_id: str) -> dict:
    # 接管模式：一步走（批准即自动启动），此端点为兼容保留 → no-op 返回当前快照
    if trigger.enabled():
        return _signaled_response(task_id)
    try:
        task = start_execution(task_id)
        return {"ok": True, "task_id": task_id, "status": task["status"], "task": task}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/tasks/{task_id}/execution/continue", summary="兼容：继续执行 coding 任务")
def post_continue_execution(task_id: str, req: ContinueExecutionRequest | None = None) -> dict:
    comment = req.comment if req else ""
    if trigger.signal_coding_task_sync(task_id, "execution_continue", comment):
        return _signaled_response(task_id)
    try:
        task = continue_execution(task_id, comment=comment)
        return {"ok": True, "task_id": task_id, "status": task["status"], "task": task}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/tasks/{task_id}/automated-review/respond", summary="提交 Kimi Review 处理意见并按需继续修复")
def post_automated_review_response(
    task_id: str,
    req: AutomatedReviewResponseRequest,
    current: CurrentUser = Depends(get_current_user),
) -> dict:
    try:
        task = submit_automated_review_response(
            task_id,
            responses=[item.model_dump() for item in req.responses],
            reviewer=_current_reviewer(current, req.reviewer),
            additional_comment=req.additional_comment,
        )
        return {"ok": True, "task_id": task_id, "status": task["status"], "task": task}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/tasks/{task_id}/code-review/complete", summary="完成 Coding Task 代码审核")
def post_complete_code_review(
    task_id: str,
    req: CodeReviewCompleteRequest | None = None,
    current: CurrentUser = Depends(get_current_user),
) -> dict:
    try:
        task = complete_code_review(
            task_id,
            reviewer=_current_reviewer(current, req.reviewer if req else ""),
            comment=(req.comment if req else ""),
        )
        return {"ok": True, "task_id": task_id, "status": task["status"], "task": task}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/tasks/{task_id}/pause", summary="暂停 Coding Task")
def pause_task(task_id: str) -> dict:
    return {"ok": True, "task": update_control(task_id, pause_requested=True)}


@router.post("/tasks/{task_id}/resume", summary="恢复 Coding Task")
def resume_task(task_id: str) -> dict:
    return {"ok": True, "task": resume_coding_task(task_id)}


@router.post("/tasks/{task_id}/cancel", summary="取消 Coding Task")
def cancel_task(task_id: str) -> dict:
    # 接管模式：发 cancel signal（workflow 驱动取消，置 control.cancel_requested）。
    # 同时仍写 DB control 标记，保证已在跑的 Job 能在安全点退出。
    trigger.signal_coding_task_sync(task_id, "cancel")
    return {"ok": True, "task": update_control(task_id, cancel_requested=True)}


@router.post("/tasks/{task_id}/interrupt", summary="向 Coding Task 追加中断指令")
def interrupt_task(task_id: str, req: CodingMessageRequest) -> dict:
    append_message(task_id, req.message)
    return {"ok": True}


@router.post("/tasks/{task_id}/message", summary="向 Coding Task 追加消息")
def message_task(task_id: str, req: CodingMessageRequest) -> dict:
    append_message(task_id, req.message)
    return {"ok": True}
