"""IssueLinkWorkflow：一个 GitLab issue 的编排父流程（多仓 fan-out / join）。

- （可选）blueprint 阶段：fan-out 前先收敛路由 + 定跨仓契约，过一道人审 gate，
  避免过度路由（白跑 task）和前后端各猜接口 schema。由 temporal_config.blueprint_enabled
  灰度控制，关闭时走原 fan-out（只影响新 link）。
- 为每个路由仓库起一个 CodingTaskWorkflow 子流程（child id == task_id）；
- await 全部子流程（merge-completion join）；
- 全部合并 → 关 issue（子流程在各自到 waiting_code_review 时已触发聚合 MR-ready 通知）；
- 任一子仓未合并（失败/关闭）→ 显式 fail_link（避免缺口B 的静默死锁），交人工。

workflow id == link_id。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Optional

from temporalio import workflow
from temporalio.common import RetryPolicy

from workflows.coding_task_workflow import CodingTaskWorkflow

with workflow.unsafe.imports_passed_through():
    from activities.issue_activities import (
        apply_blueprint,
        close_issue_for_link,
        emit_link_event,
        fail_link,
        get_link_state,
        mark_task_merged,
        prepare_blueprint,
        prepare_child_tasks,
        set_link_status,
    )
    from settings import temporal_config

_SHORT = timedelta(seconds=30)
_DISPATCH = timedelta(seconds=60)
_RETRY = RetryPolicy(maximum_interval=timedelta(seconds=60))


@dataclass
class BlueprintReview:
    decision: str  # approved | cancelled
    repos: list[dict[str, Any]] = field(default_factory=list)
    contracts: list[dict[str, Any]] = field(default_factory=list)
    reviewer: str = ""
    comment: str = ""


@workflow.defn(name="IssueLinkWorkflow")
class IssueLinkWorkflow:
    def __init__(self) -> None:
        self._blueprint_review: Optional[BlueprintReview] = None

    # ---------------- signals ----------------
    @workflow.signal
    def blueprint_reviewed(
        self,
        decision: str,
        repos: Optional[list[dict[str, Any]]] = None,
        contracts: Optional[list[dict[str, Any]]] = None,
        reviewer: str = "",
        comment: str = "",
    ) -> None:
        self._blueprint_review = BlueprintReview(
            decision=decision, repos=repos or [], contracts=contracts or [],
            reviewer=reviewer, comment=comment,
        )

    @workflow.run
    async def run(self, link_id: str) -> dict[str, Any]:
        # 0) blueprint 阶段（flag 守卫）：收敛路由 + 定契约 + 人审 gate，再 fan-out。
        if temporal_config.blueprint_enabled:
            cancelled = await self._blueprint_phase(link_id)
            if cancelled:
                return {"link_id": link_id, "result": "cancelled", "tasks": []}

        # 1) 建/取各路由仓库的 coding task（幂等；blueprint 已把收敛清单写回 result.routed）
        task_ids = await workflow.execute_activity(
            prepare_child_tasks, link_id, start_to_close_timeout=_DISPATCH, retry_policy=_RETRY
        )
        if not task_ids:
            await workflow.execute_activity(
                fail_link, args=[link_id, "未能为任何仓库创建 Coding Task"],
                start_to_close_timeout=_SHORT, retry_policy=_RETRY,
            )
            return {"link_id": link_id, "result": "failed", "tasks": []}

        await workflow.execute_activity(
            set_link_status, args=[link_id, "running", "running", f"已启动 {len(task_ids)} 个 Coding Task"],
            start_to_close_timeout=_SHORT, retry_policy=_RETRY,
        )

        # 2) fan-out：每个仓库一个子 workflow（child id == task_id，传 link_id 以便子流程做 MR-ready 聚合）
        async def _run_child(tid: str) -> dict[str, Any]:
            return await workflow.execute_child_workflow(
                CodingTaskWorkflow.run, args=[tid, link_id], id=tid
            )

        # 3) join：等全部子流程结束
        results: list[dict[str, Any]] = await asyncio.gather(*[_run_child(tid) for tid in task_ids])

        merged = [r for r in results if r.get("result") == "merged"]
        for r in merged:
            await workflow.execute_activity(
                mark_task_merged, args=[link_id, r.get("task_id") or "", r.get("mr_url") or ""],
                start_to_close_timeout=_SHORT, retry_policy=_RETRY,
            )

        if len(merged) == len(results):
            # 4) 全部合并 → 关 issue + 通知需求人
            await workflow.execute_activity(
                close_issue_for_link, link_id, start_to_close_timeout=_DISPATCH, retry_policy=_RETRY
            )
            return {"link_id": link_id, "result": "issue_closed", "tasks": results}

        # 部分仓库未合并 → 显式失败，交人工（不静默死锁）
        bad = [f"{r.get('task_id')}:{r.get('result')}" for r in results if r.get("result") != "merged"]
        await workflow.execute_activity(
            fail_link, args=[link_id, f"部分仓库未合并完成：{', '.join(bad)}"],
            start_to_close_timeout=_SHORT, retry_policy=_RETRY,
        )
        return {"link_id": link_id, "result": "partial_failed", "tasks": results}

    # ---------------- helpers ----------------
    async def _blueprint_phase(self, link_id: str) -> bool:
        """生成蓝图 → 等 blueprint_reviewed signal（超时升级后无限等）→ 落地。返回是否被取消。"""
        await workflow.execute_activity(
            prepare_blueprint, link_id, start_to_close_timeout=_DISPATCH, retry_policy=_RETRY
        )
        timeout = timedelta(seconds=temporal_config.blueprint_review_timeout_sec)
        try:
            await workflow.wait_condition(lambda: self._blueprint_review is not None, timeout=timeout)
        except asyncio.TimeoutError:
            await workflow.execute_activity(
                emit_link_event,
                args=[link_id, "blueprint_wait_timeout", "改动蓝图等待人工确认超时，请尽快在 Viktor 工作台处理", {}, "blueprint_review"],
                start_to_close_timeout=_SHORT, retry_policy=_RETRY,
            )
            await workflow.wait_condition(lambda: self._blueprint_review is not None)
        rev = self._blueprint_review
        self._blueprint_review = None
        if rev and rev.decision == "cancelled":
            await workflow.execute_activity(
                fail_link, args=[link_id, f"改动蓝图被取消{('：' + rev.comment) if rev.comment else ''}"],
                start_to_close_timeout=_SHORT, retry_policy=_RETRY,
            )
            return True
        await workflow.execute_activity(
            apply_blueprint,
            args=[link_id, rev.repos if rev else [], rev.contracts if rev else [],
                  rev.reviewer if rev else "", rev.comment if rev else ""],
            start_to_close_timeout=_DISPATCH, retry_policy=_RETRY,
        )
        return False
