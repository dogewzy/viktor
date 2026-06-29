"""IssueLinkWorkflow 测试：fan-out/join + 全合并关 issue / 部分失败显式 fail。

用 stub 子 workflow（同名覆盖 CodingTaskWorkflow）隔离父逻辑——子流程自身在
test_coding_task_workflow.py 已单测。
"""
import uuid

import pytest
from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from workflows.issue_link_workflow import IssueLinkWorkflow

# prepare_child_tasks（activity，跑在 host）读这个全局；workflow 沙箱内读不到 host 全局，
# 故子流程结果改由 task_id 命名约定推导（含 "__closed" → closed，否则 merged）。
_TASK_IDS: list[str] = []
_CALLS: list[str] = []


@workflow.defn(name="CodingTaskWorkflow")
class StubChild:
    @workflow.run
    async def run(self, task_id: str, link_id: str = "") -> dict:
        result = "closed" if "__closed" in task_id else "merged"
        return {"task_id": task_id, "result": result, "mr_url": f"mr-{task_id}"}


@activity.defn
async def prepare_child_tasks(link_id: str) -> list[str]:
    return list(_TASK_IDS)


@activity.defn
async def set_link_status(link_id: str, status: str, stage: str, message: str) -> None:
    _CALLS.append(f"set:{status}")


@activity.defn
async def emit_link_event(link_id: str, event_type: str, message: str, payload: dict, stage: str) -> None:
    _CALLS.append(f"event:{event_type}")


@activity.defn
async def mark_task_merged(link_id: str, task_id: str, mr_url: str) -> bool:
    _CALLS.append(f"merged:{task_id}")
    return True


@activity.defn
async def close_issue_for_link(link_id: str) -> None:
    _CALLS.append("close_issue")


@activity.defn
async def fail_link(link_id: str, reason: str) -> None:
    _CALLS.append(f"fail:{reason}")


_MOCKS = [prepare_child_tasks, set_link_status, emit_link_event, mark_task_merged, close_issue_for_link, fail_link]


async def _run(task_ids: list[str]) -> dict:
    global _TASK_IDS, _CALLS
    _TASK_IDS = list(task_ids)
    _CALLS = []
    tq = f"test-{uuid.uuid4()}"
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(env.client, task_queue=tq, workflows=[IssueLinkWorkflow, StubChild], activities=_MOCKS):
            return await env.client.execute_workflow(
                IssueLinkWorkflow.run, "il_test", id=f"il_{uuid.uuid4()}", task_queue=tq
            )


@pytest.mark.asyncio
async def test_all_merged_closes_issue():
    result = await _run(["ct_a", "ct_b"])
    assert result["result"] == "issue_closed"
    assert "close_issue" in _CALLS
    assert "merged:ct_a" in _CALLS and "merged:ct_b" in _CALLS
    assert not any(c.startswith("fail:") for c in _CALLS)


@pytest.mark.asyncio
async def test_partial_failure_fails_link():
    result = await _run(["ct_a", "ct_b__closed"])
    assert result["result"] == "partial_failed"
    assert any(c.startswith("fail:") for c in _CALLS)
    assert "close_issue" not in _CALLS
