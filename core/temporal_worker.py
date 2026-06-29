"""Temporal worker 进程入口：python -m core.temporal_worker

注册 workflows + activities，连接 Temporal，长驻 poll task_queue。
作为独立 K8s Deployment 运行（同 viktor 镜像，命令换成本模块）。
"""
from __future__ import annotations

import asyncio

from loguru import logger
from temporalio.worker import Worker

from activities import coding_activities as ca
from activities import issue_activities as ia
from activities import staging_activities as sa
from core.temporal.client import get_temporal_client
from settings import temporal_config
from workflows.coding_task_workflow import CodingTaskWorkflow
from workflows.issue_link_workflow import IssueLinkWorkflow
from workflows.staging_acceptance_workflow import StagingCoordinatorWorkflow, StagingRunWorkflow

ACTIVITIES = [
    # coding task
    ca.get_task_status, ca.job_exists, ca.dispatch_job,
    ca.apply_clarification_answer, ca.apply_plan_review, ca.apply_plan_revision,
    ca.start_execution_activity, ca.continue_execution_activity,
    ca.requeue_after_rate_limit,
    ca.complete_code_review_activity, ca.close_code_review_activity,
    ca.poll_mr_state, ca.emit_task_event, ca.request_cancel_activity,
    ca.notify_task_gate, ca.mark_task_failed,
    # issue link
    ia.prepare_blueprint, ia.apply_blueprint,
    ia.prepare_child_tasks, ia.get_link_state, ia.set_link_status,
    ia.emit_link_event, ia.mark_link_mr_ready, ia.mark_task_merged,
    ia.close_issue_for_link, ia.fail_link,
    # staging acceptance
    sa.get_next_staging_run, sa.get_staging_run_activity,
    sa.acquire_staging_lock, sa.heartbeat_staging_lock, sa.release_staging_lock,
    sa.hold_staging_lock_for_manual_intervention,
    sa.refresh_staging_candidates, sa.integrate_staging_to_dev,
    sa.wait_staging_deploy, sa.run_staging_playwright,
    sa.create_staging_feedback, sa.restore_staging_dev,
    sa.mark_staging_failed_activity, sa.mark_staging_passed_activity,
]

WORKFLOWS = [CodingTaskWorkflow, IssueLinkWorkflow, StagingCoordinatorWorkflow, StagingRunWorkflow]


async def run_worker() -> None:
    # 填充内存 registry 单例：activity（_resolve_repo / create_coding_tasks_for_issue 等）依赖它，
    # 否则报“项目 不存在”。与 web(main.py) / coding_job_runner 一致。
    from core.registry import registry
    registry.load_from_db()

    client = await get_temporal_client()
    logger.info(
        "Temporal worker 启动：target={} namespace={} task_queue={}",
        f"{temporal_config.host}:{temporal_config.port}",
        temporal_config.namespace,
        temporal_config.task_queue,
    )
    # big-bang 存量接管：一次性把切换前在飞的 link/task 拉进编排（幂等，多副本安全）。
    try:
        from core.temporal.adopt import adopt_inflight
        await adopt_inflight()
    except Exception as e:  # noqa: BLE001 - 接管失败不阻断 worker 起动
        logger.warning("[temporal] 存量接管异常（忽略，继续起 worker）: {}", e)
    worker = Worker(
        client,
        task_queue=temporal_config.task_queue,
        workflows=WORKFLOWS,
        activities=ACTIVITIES,
    )
    await worker.run()


def main() -> int:
    asyncio.run(run_worker())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
