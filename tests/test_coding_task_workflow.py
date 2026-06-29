"""CodingTaskWorkflow 集成测试：time-skipping 环境 + mock activities，不碰真实 DB/K8s。

验证：created → 澄清 gate → plan 审批 gate → 执行 → 合并 gate → completed 的全链路，
以及 rejected / cancel 旁路。
"""
import asyncio
import uuid

import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from workflows.coding_task_workflow import CodingTaskWorkflow


class FakeBackend:
    """用内存状态模拟 coding task 在 DB 里的状态推进（替代真实 Job + DB）。"""

    def __init__(self, rate_limit_times: int = 0) -> None:
        self.status = "created"
        self.job = False
        self.planning_rounds = 0
        self.exec_attempts = 0
        self.rate_limit_times = rate_limit_times  # 前 N 次 execution 派发模拟限流落 rate_limited
        self.mr_url = "http://gitlab/mr/1"
        self.calls: list[str] = []

    def snapshot(self) -> dict:
        # running 被读到时模拟 Job 跑完 → waiting_code_review
        if self.status == "running":
            self.status = "waiting_code_review"
            self.job = False
        return {
            "exists": True, "status": self.status, "stage": self.status,
            "message": "", "mr_url": self.mr_url, "mr_iid": 1, "report_id": "",
            "project_id": "p", "repo_connector_id": "default", "clarification_status": "",
        }

    def dispatch(self, mode: str) -> None:
        self.calls.append(f"dispatch:{mode}")
        if mode == "planning":
            self.planning_rounds += 1
            self.status = "waiting_clarification" if self.planning_rounds == 1 else "waiting_plan_review"
        else:
            self.exec_attempts += 1
            if self.exec_attempts <= self.rate_limit_times:
                self.status = "rate_limited"  # 模拟 runner 限流落可重试态
                self.job = False
            else:
                self.status = "running"
                self.job = True


_B: FakeBackend


@activity.defn
async def get_task_status(task_id: str) -> dict:
    return _B.snapshot()


@activity.defn
async def job_exists(task_id: str) -> bool:
    return _B.job


@activity.defn
async def dispatch_job(task_id: str, mode: str) -> str:
    _B.dispatch(mode)
    return f"job-{mode}"


@activity.defn
async def apply_clarification_answer(task_id: str, answers: dict, reviewer: str) -> None:
    _B.calls.append("clarify")
    _B.status = "waiting_plan_review"  # 模拟重跑 planning 后直接出 plan


@activity.defn
async def apply_plan_review(task_id: str, decision: str, comment: str, reviewer: str) -> dict:
    _B.calls.append(f"review:{decision}")
    if decision == "rejected":
        _B.status = "plan_rejected"
    return {"status": _B.status}


@activity.defn
async def start_execution_activity(task_id: str) -> None:
    _B.dispatch("execution")


@activity.defn
async def continue_execution_activity(task_id: str, comment: str) -> None:
    _B.calls.append("continue")
    _B.dispatch("execution")


@activity.defn
async def requeue_after_rate_limit(task_id: str) -> str:
    _B.calls.append("requeue")
    return "execution"  # 退避结束，重派 execution（status 由后续 dispatch 推进）


@activity.defn
async def complete_code_review_activity(task_id: str, reviewer: str, comment: str, merge_payload) -> None:
    _B.calls.append("complete")
    _B.status = "completed"


@activity.defn
async def close_code_review_activity(task_id: str, reviewer: str, comment: str, close_payload) -> None:
    _B.calls.append("close")
    _B.status = "cancelled"


@activity.defn
async def poll_mr_state(task_id: str) -> str:
    return ""


@activity.defn
async def emit_task_event(task_id: str, event_type: str, message: str, payload: dict, stage: str) -> None:
    _B.calls.append(f"event:{event_type}")


@activity.defn
async def request_cancel_activity(task_id: str) -> None:
    _B.calls.append("cancel")
    _B.status = "cancelled"


@activity.defn
async def notify_task_gate(task_id: str, gate: str) -> None:
    _B.calls.append(f"gate:{gate}")


@activity.defn
async def mark_task_failed(task_id: str, message: str) -> None:
    _B.calls.append(f"failed:{message}")
    _B.status = "failed"


_MOCK_ACTIVITIES = [
    get_task_status, job_exists, dispatch_job, apply_clarification_answer,
    apply_plan_review, start_execution_activity, continue_execution_activity,
    requeue_after_rate_limit,
    complete_code_review_activity, close_code_review_activity, poll_mr_state,
    emit_task_event, request_cancel_activity, notify_task_gate, mark_task_failed,
]


async def _run_with(backend: FakeBackend, drive):
    global _B
    _B = backend
    tq = f"test-{uuid.uuid4()}"
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(env.client, task_queue=tq, workflows=[CodingTaskWorkflow], activities=_MOCK_ACTIVITIES):
            handle = await env.client.start_workflow(
                CodingTaskWorkflow.run, "ct_test", id=f"ct_test_{uuid.uuid4()}", task_queue=tq
            )
            await drive(handle)
            return await handle.result()


@pytest.mark.asyncio
async def test_happy_path_to_merged():
    backend = FakeBackend()

    async def drive(handle):
        # 三个 gate 的 signal 预先发：各自只在对应 gate 被消费、且 set-once
        await handle.signal(CodingTaskWorkflow.clarification_answered, args=[{"q1": "a"}, "alice"])
        await handle.signal(CodingTaskWorkflow.plan_reviewed, args=["approved", "lgtm", "bob"])
        await handle.signal(CodingTaskWorkflow.mr_state_changed, args=["merged", backend.mr_url])

    result = await _run_with(backend, drive)
    assert result["result"] == "merged"
    assert result["mr_url"] == backend.mr_url
    assert "dispatch:planning" in backend.calls
    assert "clarify" in backend.calls
    assert "review:approved" in backend.calls
    assert "dispatch:execution" in backend.calls
    assert "complete" in backend.calls


@pytest.mark.asyncio
async def test_plan_rejected_terminal():
    backend = FakeBackend()

    async def drive(handle):
        await handle.signal(CodingTaskWorkflow.clarification_answered, args=[{"q1": "a"}, "alice"])
        await handle.signal(CodingTaskWorkflow.plan_reviewed, args=["rejected", "no", "bob"])

    result = await _run_with(backend, drive)
    assert result["result"] == "plan_rejected"
    assert "review:rejected" in backend.calls
    assert "dispatch:execution" not in backend.calls


@pytest.mark.asyncio
async def test_rate_limited_retry_then_succeeds():
    # 首次 execution 限流落 rate_limited，编排退避后重派，第二次成功 → merged。
    backend = FakeBackend(rate_limit_times=1)

    async def drive(handle):
        await handle.signal(CodingTaskWorkflow.clarification_answered, args=[{"q1": "a"}, "alice"])
        await handle.signal(CodingTaskWorkflow.plan_reviewed, args=["approved", "lgtm", "bob"])
        await handle.signal(CodingTaskWorkflow.mr_state_changed, args=["merged", backend.mr_url])

    result = await _run_with(backend, drive)
    assert result["result"] == "merged"
    assert "event:rate_limit_backoff" in backend.calls
    assert "requeue" in backend.calls
    assert backend.exec_attempts == 2  # 限流 1 次 + 重试成功 1 次
    assert "complete" in backend.calls


@pytest.mark.asyncio
async def test_rate_limited_exhausts_to_failed():
    # 持续限流：重试次数耗尽后落 failed 终态。
    backend = FakeBackend(rate_limit_times=99)

    async def drive(handle):
        await handle.signal(CodingTaskWorkflow.clarification_answered, args=[{"q1": "a"}, "alice"])
        await handle.signal(CodingTaskWorkflow.plan_reviewed, args=["approved", "lgtm", "bob"])

    result = await _run_with(backend, drive)
    assert result["result"] == "failed"
    assert any(c.startswith("failed:") for c in backend.calls)
