"""CodingTaskWorkflow：单个 coding task 的全生命周期 durable 编排。

设计：workflow 不搬重活——planning/execution 仍是现有 K8s Job，Job 内部已写好
所有 DB 状态/事件/MR。workflow 只做"编排大脑"：
- 按 DB 真实 status 决定下一步（每轮重读，像 controller，但 durable + signal 驱动）；
- 人审 gate（澄清 / plan 审批 / 合并）= wait_condition(signal) + durable timer + 超时升级；
- Job 完成靠轮询 get_task_status；派发前查 job_exists 防重复；Job 中途丢失则重派（孤儿恢复）。

workflow id == task_id，故 HTTP 路由 / GitLab webhook 可用 task_id 精确发 signal。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Optional

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from activities.coding_activities import (
        TASK_FINAL,
        apply_clarification_answer,
        apply_plan_review,
        apply_plan_revision,
        close_code_review_activity,
        complete_code_review_activity,
        continue_execution_activity,
        dispatch_job,
        emit_task_event,
        get_task_status,
        job_exists,
        mark_task_failed,
        notify_task_gate,
        poll_mr_state,
        request_cancel_activity,
        start_execution_activity,
    )
    from activities.issue_activities import mark_link_mr_ready
    from settings import temporal_config

# 活跃计算态：这些状态下应有一个 K8s Job 在跑；状态不变又无 Job → 判为 Job 丢失。
_ACTIVE_COMPUTE = {"created", "planning", "queued", "running", "reviewing_code", "plan_approved"}

_SHORT = timedelta(seconds=30)
_DISPATCH = timedelta(seconds=60)
_RETRY = RetryPolicy(maximum_interval=timedelta(seconds=60))
# 普通 activity 重试封顶：耗尽后向 workflow 抛 ActivityError → 主循环落 failed。
_STC = timedelta(seconds=temporal_config.activity_schedule_to_close_sec)
# dispatch_job 专用：并发满是合法长等待，封顶远大于普通。
_DISPATCH_STC = timedelta(seconds=temporal_config.dispatch_schedule_to_close_sec)
# mark_task_failed 兜底用：有限重试 + 独立封顶，避免兜底本身无限重试。
_FAILSAFE_RETRY = RetryPolicy(maximum_attempts=3)
_FAILSAFE_STC = timedelta(seconds=120)


@dataclass
class ClarificationAnswer:
    answers: dict[str, Any]
    reviewer: str = ""


@dataclass
class PlanReview:
    decision: str  # approved | rejected
    comment: str = ""
    reviewer: str = ""


@dataclass
class ContinueExecution:
    comment: str = ""


@dataclass
class MrState:
    state: str  # merged | closed | opened
    mr_url: str = ""


@workflow.defn(name="CodingTaskWorkflow")
class CodingTaskWorkflow:
    def __init__(self) -> None:
        self._clarification: Optional[ClarificationAnswer] = None
        self._plan_review: Optional[PlanReview] = None
        self._continue: Optional[ContinueExecution] = None
        self._mr_state: Optional[MrState] = None
        self._cancel = False
        self._status = ""
        self._link_id = ""
        self._mr_ready_marked = False
        self._code_review_notified = False

    # ---------------- signals ----------------
    @workflow.signal
    def clarification_answered(self, answers: dict[str, Any], reviewer: str = "") -> None:
        self._clarification = ClarificationAnswer(answers=answers, reviewer=reviewer)

    @workflow.signal
    def plan_reviewed(self, decision: str, comment: str = "", reviewer: str = "") -> None:
        self._plan_review = PlanReview(decision=decision, comment=comment, reviewer=reviewer)

    @workflow.signal
    def plan_revision_requested(self, comment: str = "", reviewer: str = "") -> None:
        # 复用 plan_review gate：decision="revise" 走重做分支
        self._plan_review = PlanReview(decision="revise", comment=comment, reviewer=reviewer)

    @workflow.signal
    def execution_continue(self, comment: str = "") -> None:
        self._continue = ContinueExecution(comment=comment)

    @workflow.signal
    def mr_state_changed(self, state: str, mr_url: str = "") -> None:
        self._mr_state = MrState(state=state, mr_url=mr_url)

    @workflow.signal
    def cancel(self) -> None:
        self._cancel = True

    # ---------------- queries ----------------
    @workflow.query
    def get_state(self) -> dict[str, Any]:
        return {
            "status": self._status,
            "cancel_requested": self._cancel,
            "pending_clarification": self._clarification is not None,
            "pending_plan_review": self._plan_review is not None,
        }

    # ---------------- run ----------------
    @workflow.run
    async def run(self, task_id: str, link_id: str = "") -> dict[str, Any]:
        self._link_id = link_id or ""
        try:
            return await self._run_loop(task_id)
        except ActivityError as e:
            # 任何 activity 重试被 schedule_to_close 截断 → 落 failed 终态。
            # mark_task_failed 用独立有限 retry + 独立封顶，且不在本 try 内（见下），避免递归。
            await self._fail(task_id, f"编排 activity 重试耗尽：{e}")
            return {"task_id": task_id, "result": "failed"}

    async def _run_loop(self, task_id: str) -> dict[str, Any]:
        while True:
            st = await workflow.execute_activity(
                get_task_status, task_id, start_to_close_timeout=_SHORT,
                retry_policy=_RETRY, schedule_to_close_timeout=_STC,
            )
            if not st.get("exists"):
                return {"task_id": task_id, "result": "missing"}
            status = str(st.get("status") or "")
            self._status = status

            if status in TASK_FINAL:
                return {"task_id": task_id, "result": status, "mr_url": st.get("mr_url") or ""}

            if self._cancel:
                await workflow.execute_activity(
                    request_cancel_activity, task_id, start_to_close_timeout=_SHORT,
                    retry_policy=_RETRY, schedule_to_close_timeout=_STC,
                )
                await self._await_status_leaves(task_id, status)
                continue

            if status == "created":
                await self._ensure_job(task_id, "planning")
                await self._await_status_leaves(task_id, status)

            elif status in ("planning", "queued", "running", "reviewing_code"):
                await self._await_status_leaves(task_id, status)

            elif status == "waiting_clarification":
                if self._clarification is None:
                    await workflow.execute_activity(
                        notify_task_gate, args=[task_id, "clarification"],
                        start_to_close_timeout=_SHORT, retry_policy=_RETRY,
                        schedule_to_close_timeout=_STC,
                    )
                ans = await self._gate(
                    task_id, "clarification", lambda: self._clarification,
                    temporal_config.clarification_timeout_sec,
                )
                if ans is None:
                    # cancel 抢先：回主循环顶，下轮走 cancel 分支驱动 cancelled 终态。
                    continue
                self._clarification = None
                await workflow.execute_activity(
                    apply_clarification_answer, args=[task_id, ans.answers, ans.reviewer],
                    start_to_close_timeout=_DISPATCH, retry_policy=_RETRY,
                    schedule_to_close_timeout=_STC,
                )

            elif status == "waiting_plan_review":
                if self._plan_review is None:
                    await workflow.execute_activity(
                        notify_task_gate, args=[task_id, "plan_review"],
                        start_to_close_timeout=_SHORT, retry_policy=_RETRY,
                        schedule_to_close_timeout=_STC,
                    )
                rev = await self._gate(
                    task_id, "plan_review", lambda: self._plan_review,
                    temporal_config.plan_review_timeout_sec,
                )
                if rev is None:
                    # cancel 抢先：回主循环顶，下轮走 cancel 分支驱动 cancelled 终态。
                    continue
                self._plan_review = None
                if rev.decision == "revise":
                    # 重做 plan：回 planning + 重派 Job；下轮循环再次进入审批 gate
                    await workflow.execute_activity(
                        apply_plan_revision, args=[task_id, rev.comment, rev.reviewer],
                        start_to_close_timeout=_DISPATCH, retry_policy=_RETRY,
                        schedule_to_close_timeout=_STC,
                    )
                else:
                    await workflow.execute_activity(
                        apply_plan_review, args=[task_id, rev.decision, rev.comment, rev.reviewer],
                        start_to_close_timeout=_DISPATCH, retry_policy=_RETRY,
                        schedule_to_close_timeout=_STC,
                    )
                    if rev.decision == "approved":
                        # 一步走：批准即自动启动执行
                        await workflow.execute_activity(
                            start_execution_activity, task_id,
                            start_to_close_timeout=_DISPATCH, retry_policy=_RETRY,
                            schedule_to_close_timeout=_STC,
                        )
                    # rejected → 下轮 get_task_status 见 plan_rejected → 终态返回

            elif status == "plan_approved":
                await self._ensure_job(task_id, "execution")
                await self._await_status_leaves(task_id, status)

            elif status == "waiting_code_review":
                # MR 就绪：记到 link 并在全部就绪时触发聚合钉钉（仅多仓 link 场景，幂等）。
                if self._link_id and not self._mr_ready_marked:
                    await workflow.execute_activity(
                        mark_link_mr_ready, args=[self._link_id, task_id, st.get("mr_url") or ""],
                        start_to_close_timeout=_DISPATCH, retry_policy=_RETRY,
                        schedule_to_close_timeout=_STC,
                    )
                    self._mr_ready_marked = True
                elif not self._link_id and not self._code_review_notified:
                    # 独立 task（无 link 聚合通知）：单独发 MR 待处理钉钉。
                    await workflow.execute_activity(
                        notify_task_gate, args=[task_id, "code_review"],
                        start_to_close_timeout=_SHORT, retry_policy=_RETRY,
                        schedule_to_close_timeout=_STC,
                    )
                    self._code_review_notified = True
                outcome = await self._merge_gate(task_id)
                if outcome[0] == "merged":
                    await workflow.execute_activity(
                        complete_code_review_activity,
                        args=[task_id, "temporal", "MR 已合并", {"mr_url": outcome[1]}],
                        start_to_close_timeout=_DISPATCH, retry_policy=_RETRY,
                        schedule_to_close_timeout=_STC,
                    )
                    return {"task_id": task_id, "result": "merged", "mr_url": outcome[1]}
                elif outcome[0] == "closed":
                    await workflow.execute_activity(
                        close_code_review_activity,
                        args=[task_id, "temporal", "MR 已关闭（未合并）", {}],
                        start_to_close_timeout=_DISPATCH, retry_policy=_RETRY,
                        schedule_to_close_timeout=_STC,
                    )
                    return {"task_id": task_id, "result": "closed"}
                else:  # continue：review 修复，复用同一 MR 分支
                    await workflow.execute_activity(
                        continue_execution_activity, args=[task_id, outcome[1]],
                        start_to_close_timeout=_DISPATCH, retry_policy=_RETRY,
                        schedule_to_close_timeout=_STC,
                    )
            else:
                # 未知/暂停态：等状态变化，避免空转
                await self._await_status_leaves(task_id, status)

    # ---------------- helpers ----------------
    async def _fail(self, task_id: str, message: str) -> None:
        """activity 耗尽兜底：落 failed。用独立有限 retry + 独立封顶，避免兜底自身无限重试/递归。"""
        await workflow.execute_activity(
            mark_task_failed, args=[task_id, message],
            start_to_close_timeout=_DISPATCH, retry_policy=_FAILSAFE_RETRY,
            schedule_to_close_timeout=_FAILSAFE_STC,
        )

    async def _ensure_job(self, task_id: str, mode: str) -> None:
        exists = await workflow.execute_activity(
            job_exists, task_id, start_to_close_timeout=_SHORT,
            retry_policy=_RETRY, schedule_to_close_timeout=_STC,
        )
        if not exists:
            await workflow.execute_activity(
                dispatch_job, args=[task_id, mode], start_to_close_timeout=_DISPATCH,
                retry_policy=_RETRY, schedule_to_close_timeout=_DISPATCH_STC,
            )

    async def _await_status_leaves(self, task_id: str, from_status: str) -> None:
        """轮询直到 status 离开 from_status；活跃计算态下若 Job 丢失则重派（孤儿恢复）。"""
        poll = timedelta(seconds=temporal_config.job_poll_interval_sec)
        while True:
            await workflow.sleep(poll)
            if self._cancel:
                return
            st = await workflow.execute_activity(
                get_task_status, task_id, start_to_close_timeout=_SHORT,
                retry_policy=_RETRY, schedule_to_close_timeout=_STC,
            )
            if not st.get("exists") or str(st.get("status") or "") != from_status:
                return
            if from_status in _ACTIVE_COMPUTE:
                alive = await workflow.execute_activity(
                    job_exists, task_id, start_to_close_timeout=_SHORT,
                    retry_policy=_RETRY, schedule_to_close_timeout=_STC,
                )
                if not alive:
                    mode = "planning" if from_status in ("created", "planning") else "execution"
                    await workflow.execute_activity(
                        dispatch_job, args=[task_id, mode],
                        start_to_close_timeout=_DISPATCH, retry_policy=_RETRY,
                        schedule_to_close_timeout=_DISPATCH_STC,
                    )

    async def _gate(self, task_id: str, name: str, getter, timeout_sec: int):
        """人审 gate：等 signal；超时周期性升级；监听 cancel。

        返回 signal 值；若 cancel 抢先则返回 None（调用方据此回主循环走 cancel 分支）。
        """
        escalated = False
        while True:
            if self._cancel:
                return None
            wait_sec = timeout_sec if not escalated else temporal_config.gate_escalation_interval_sec
            try:
                await workflow.wait_condition(
                    lambda: getter() is not None or self._cancel,
                    timeout=timedelta(seconds=wait_sec),
                )
            except asyncio.TimeoutError:
                await workflow.execute_activity(
                    emit_task_event,
                    args=[task_id, f"{name}_wait_timeout", "等待人工处理超时，请尽快在 Viktor 工作台处理", {}, ""],
                    start_to_close_timeout=_SHORT, retry_policy=_RETRY,
                    schedule_to_close_timeout=_STC,
                )
                escalated = True
                continue
            if self._cancel:
                return None
            if getter() is not None:
                return getter()

    async def _merge_gate(self, task_id: str) -> tuple[str, str]:
        """合并 gate：等 mr_state_changed / execution_continue signal；超时轮询 GitLab 兜底。"""
        poll = timedelta(seconds=temporal_config.merge_poll_interval_sec)
        while True:
            if self._cancel:
                return ("closed", "")
            try:
                await workflow.wait_condition(
                    lambda: self._mr_state is not None or self._continue is not None, timeout=poll
                )
            except asyncio.TimeoutError:
                state = await workflow.execute_activity(
                    poll_mr_state, task_id, start_to_close_timeout=_SHORT, retry_policy=_RETRY
                )
                if state == "merged":
                    self._mr_state = MrState(state="merged")
                elif state == "closed":
                    self._mr_state = MrState(state="closed")
            if self._continue is not None:
                c = self._continue
                self._continue = None
                return ("continue", c.comment)
            if self._mr_state is not None:
                s = self._mr_state
                self._mr_state = None
                return (s.state, s.mr_url)
