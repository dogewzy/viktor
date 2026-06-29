"""CodingTaskWorkflow 用的 activities：包 coding_service / coding_job_dispatch / gitlab。

这些函数已封装全部 DB 写入与副作用（读模型投影即在其中），workflow 只负责按序调用，
从而成为读模型的唯一写者（消除并发丢更新）。
"""
from __future__ import annotations

import asyncio
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

# coding task 的“非活跃/终态”集合（workflow 据此判断某阶段是否已结束）。
TASK_FINAL = {"completed", "failed", "cancelled", "plan_rejected"}


@activity.defn
async def get_task_status(task_id: str) -> dict[str, Any]:
    """读 coding task 当前状态（workflow 分支用的最小字段集）。"""
    def _run() -> dict[str, Any]:
        from core.coding_service import get_task
        t = get_task(task_id)
        if not t:
            return {"exists": False, "status": "missing"}
        result = t.get("result") if isinstance(t.get("result"), dict) else {}
        clarification = t.get("clarification") if isinstance(t.get("clarification"), dict) else {}
        mr = result.get("mr") if isinstance(result.get("mr"), dict) else {}
        return {
            "exists": True,
            "status": str(t.get("status") or ""),
            "stage": str(t.get("stage") or ""),
            "message": str(t.get("message") or ""),
            "mr_url": str(t.get("mr_url") or ""),
            "mr_iid": mr.get("iid"),
            "report_id": str(t.get("report_id") or ""),
            "project_id": str(t.get("project_id") or ""),
            "repo_connector_id": str(t.get("repo_connector_id") or ""),
            "clarification_status": str(clarification.get("status") or ""),
        }
    return await asyncio.to_thread(_run)


@activity.defn
async def job_exists(task_id: str) -> bool:
    """是否已有该 task 的活跃 K8s Job（派发前防重复）。"""
    def _run() -> bool:
        from core.coding_job_dispatch import job_exists_for_task
        return bool(job_exists_for_task(task_id))
    return await asyncio.to_thread(_run)


@activity.defn
async def dispatch_job(task_id: str, mode: str) -> str:
    """派发一个 coding Job（planning/execution）。并发已满时抛可重试错误，由 Temporal 退避重投。"""
    def _run() -> str:
        from core.coding_job_dispatch import JobConcurrencyError, create_coding_job
        try:
            return str(create_coding_job(task_id, mode))
        except JobConcurrencyError as e:
            # 可重试：并发名额释放后自然成功（非业务失败）。
            raise ApplicationError(f"job concurrency full: {e}", type="JobConcurrencyError")
    return await asyncio.to_thread(_run)


@activity.defn
async def apply_clarification_answer(task_id: str, answers: dict[str, Any], reviewer: str) -> None:
    """提交澄清答案（写 DB + 重新派发 planning Job）。"""
    def _run() -> None:
        from core.coding_service import answer_clarification
        answer_clarification(task_id, answers=answers, reviewer=reviewer)
    await asyncio.to_thread(_run)


@activity.defn
async def apply_plan_review(task_id: str, decision: str, comment: str, reviewer: str) -> dict[str, Any]:
    """审核 plan（approved/rejected）。返回审核后的 task 状态。"""
    def _run() -> dict[str, Any]:
        from core.coding_service import review_plan
        t = review_plan(task_id, decision=decision, comment=comment, reviewer=reviewer)
        return {"status": str((t or {}).get("status") or "")}
    return await asyncio.to_thread(_run)


@activity.defn
async def apply_plan_revision(task_id: str, comment: str, reviewer: str) -> None:
    """要求 Agent 按审核意见重做 plan（重置回 planning + 重派 planning Job）。"""
    def _run() -> None:
        from core.coding_service import request_plan_revision
        request_plan_revision(task_id, comment=comment, reviewer=reviewer)
    await asyncio.to_thread(_run)


@activity.defn
async def start_execution_activity(task_id: str) -> None:
    """启动执行（写 DB + 派发 execution Job）。"""
    def _run() -> None:
        from core.coding_service import start_execution
        start_execution(task_id)
    await asyncio.to_thread(_run)


@activity.defn
async def requeue_after_rate_limit(task_id: str) -> str:
    """LLM 限流退避结束：把 rate_limited 翻回可执行态，返回应重派的 mode（planning/execution）。"""
    def _run() -> str:
        from core.coding_service import requeue_after_rate_limit as _requeue
        return str(_requeue(task_id))
    return await asyncio.to_thread(_run)


@activity.defn
async def continue_execution_activity(task_id: str, comment: str) -> None:
    """继续执行（review 修复指令，复用同一 MR 分支）。"""
    def _run() -> None:
        from core.coding_service import continue_execution
        continue_execution(task_id, comment)
    await asyncio.to_thread(_run)


@activity.defn
async def complete_code_review_activity(
    task_id: str, reviewer: str, comment: str, merge_payload: dict[str, Any] | None
) -> None:
    """MR 合并 → 任务 completed。"""
    def _run() -> None:
        from core.coding_service import complete_code_review
        complete_code_review(task_id, reviewer=reviewer, comment=comment, merge_payload=merge_payload)
    await asyncio.to_thread(_run)


@activity.defn
async def close_code_review_activity(
    task_id: str, reviewer: str, comment: str, close_payload: dict[str, Any] | None
) -> None:
    """MR 关闭（未合并）→ 任务取消。"""
    def _run() -> None:
        from core.coding_service import close_code_review_by_mr_closed
        close_code_review_by_mr_closed(task_id, reviewer=reviewer, comment=comment, close_payload=close_payload)
    await asyncio.to_thread(_run)


@activity.defn
async def poll_mr_state(task_id: str) -> str:
    """合并 gate 的轮询兜底：直接查 GitLab MR 真实 state（webhook 漏发时用）。

    返回 'merged' / 'closed' / 'opened' / ''（无 MR 信息）。
    """
    def _run() -> str:
        from core.coding_service import _resolve_repo, get_task
        from gitlab.merge_request_service import get_merge_request
        t = get_task(task_id)
        if not t:
            return ""
        result = t.get("result") if isinstance(t.get("result"), dict) else {}
        mr = result.get("mr") if isinstance(result.get("mr"), dict) else {}
        iid = mr.get("iid")
        if not iid:
            return ""
        try:
            git_url, _default_branch, _resolved = _resolve_repo(
                str(t.get("project_id") or ""), str(t.get("repo_connector_id") or "")
            )
            info = get_merge_request(repo_url=git_url, merge_request_iid=iid)
            return str(info.get("state") or "")
        except Exception:  # noqa: BLE001 - 轮询兜底失败不应中断 workflow，下个周期重试
            return ""
    return await asyncio.to_thread(_run)


@activity.defn
async def emit_task_event(task_id: str, event_type: str, message: str, payload: dict[str, Any], stage: str) -> None:
    """向 coding task 事件流写一条（用于 gate 超时升级等编排级提示）。"""
    def _run() -> None:
        from core.coding_service import emit_event
        emit_event(task_id, event_type, message, payload or {}, stage=stage)
    await asyncio.to_thread(_run)


@activity.defn
async def request_cancel_activity(task_id: str) -> None:
    """请求取消（置 control.cancel_requested；Job 在安全点退出）。"""
    def _run() -> None:
        from core.coding_service import update_control
        update_control(task_id, cancel_requested=True)
    await asyncio.to_thread(_run)


@activity.defn
async def mark_task_failed(task_id: str, message: str) -> None:
    """activity 重试耗尽兜底：把 task 落 failed 终态并写事件流。"""
    def _run() -> None:
        from core.coding_service import _update_task, emit_event
        _update_task(task_id, status="failed", stage="failed", message=message)
        emit_event(task_id, "failed", message, {"reason": "activity_exhausted"}, stage="failed")
    await asyncio.to_thread(_run)


@activity.defn
async def notify_task_gate(task_id: str, gate: str) -> None:
    """进入人审 gate 时发钉钉提醒。gate ∈ {clarification, plan_review, code_review}。"""
    def _run() -> None:
        from core.issue_intake_service import notify_coding_task_gate
        notify_coding_task_gate(task_id, gate)
    await asyncio.to_thread(_run)
