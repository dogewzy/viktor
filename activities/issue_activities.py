"""IssueLinkWorkflow 用的 activities：包 issue_intake_service。

复用现有函数（已封装 DB 写 + GitLab 评论/关闭 + 钉钉通知），workflow 成为唯一写者。
"""
from __future__ import annotations

import asyncio
from typing import Any

from temporalio import activity


@activity.defn
async def prepare_child_tasks(link_id: str) -> list[str]:
    """为 link 创建/取回各路由仓库的 coding task（返回 task_id 列表）。

    复用 create_coding_tasks_for_issue：已存在则返回现有，幂等。
    （big-bang 后该函数已不再 spawn watcher 线程，仅建任务 + 派 planning Job。）
    """
    def _run() -> list[str]:
        from core.issue_intake_service import create_coding_tasks_for_issue
        return list(create_coding_tasks_for_issue(link_id) or [])
    return await asyncio.to_thread(_run)


@activity.defn
async def prepare_blueprint(link_id: str) -> dict[str, Any]:
    """生成改动蓝图（收敛仓库 + 跨仓契约），存 link.result.blueprint（status=pending）。"""
    def _run() -> dict[str, Any]:
        from core.issue_intake_service import prepare_link_blueprint
        return dict(prepare_link_blueprint(link_id) or {})
    return await asyncio.to_thread(_run)


@activity.defn
async def apply_blueprint(
    link_id: str,
    repos: list[dict[str, Any]],
    contracts: list[dict[str, Any]],
    reviewer: str,
    comment: str,
    test_plan: dict[str, Any] | None = None,
) -> None:
    """人审结果落地：收敛仓库写回 routed、契约写入 blueprint。"""
    def _run() -> None:
        from core.issue_intake_service import apply_link_blueprint
        apply_link_blueprint(link_id, repos, contracts, reviewer, comment, test_plan)
    await asyncio.to_thread(_run)


@activity.defn
async def get_link_state(link_id: str) -> dict[str, Any]:
    """读 issue link 当前状态（含 result.coding_tasks）。"""
    def _run() -> dict[str, Any]:
        from core.issue_intake_service import get_issue_intake_link
        link = get_issue_intake_link(link_id)
        if not link:
            return {"exists": False}
        result = link.get("result") if isinstance(link.get("result"), dict) else {}
        tasks = result.get("coding_tasks") if isinstance(result.get("coding_tasks"), list) else []
        return {
            "exists": True,
            "status": str(link.get("status") or ""),
            "coding_tasks": [
                {
                    "coding_task_id": str(t.get("coding_task_id") or ""),
                    "repo_connector_id": str(t.get("repo_connector_id") or ""),
                    "mr_url": str(t.get("mr_url") or ""),
                    "merged": bool(t.get("merged")),
                }
                for t in tasks if isinstance(t, dict) and t.get("coding_task_id")
            ],
        }
    return await asyncio.to_thread(_run)


@activity.defn
async def set_link_status(link_id: str, status: str, stage: str, message: str) -> None:
    def _run() -> None:
        from core.issue_intake_service import _set_link_status
        _set_link_status(link_id, status, stage, message)
    await asyncio.to_thread(_run)


@activity.defn
async def emit_link_event(link_id: str, event_type: str, message: str, payload: dict[str, Any], stage: str) -> None:
    def _run() -> None:
        from core.issue_intake_service import emit_issue_event
        emit_issue_event(link_id, event_type, message, payload or {}, stage=stage)
    await asyncio.to_thread(_run)


@activity.defn
async def mark_link_mr_ready(link_id: str, task_id: str, mr_url: str) -> None:
    """某子仓 MR 就绪：记 MR 到 link，并在全部就绪时发聚合钉钉（幂等，靠 mr_ready_notified 标记）。"""
    def _run() -> None:
        from core.issue_intake_service import _maybe_notify_all_mr_ready, _record_task_mr
        if mr_url:
            _record_task_mr(link_id, task_id, mr_url)
        _maybe_notify_all_mr_ready(link_id)
        try:
            from core.staging_acceptance_service import enqueue_staging_for_link
            enqueue_staging_for_link(link_id)
        except Exception:
            # staging 入队失败不应阻断 MR ready 读模型；由事件/日志与后续重扫兜底。
            pass
    await asyncio.to_thread(_run)


@activity.defn
async def mark_task_merged(link_id: str, task_id: str, mr_url: str) -> bool:
    """标记某子仓已合并，返回该 link 下是否全部已合并。"""
    def _run() -> bool:
        from core.issue_intake_service import _mark_task_merged
        return bool(_mark_task_merged(link_id, task_id, mr_url))
    return await asyncio.to_thread(_run)


@activity.defn
async def close_issue_for_link(link_id: str) -> None:
    """全部子仓合并 → 关 GitLab issue + 打标 + 通知需求人（复用现有收尾）。"""
    def _run() -> None:
        from core.issue_intake_service import _close_issue_for_link
        _close_issue_for_link(link_id)
    await asyncio.to_thread(_run)


@activity.defn
async def fail_link(link_id: str, reason: str) -> None:
    def _run() -> None:
        from core.issue_intake_service import _fail_link
        _fail_link(link_id, reason)
    await asyncio.to_thread(_run)
