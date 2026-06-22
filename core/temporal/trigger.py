"""从 web/scheduler 进程触发 Temporal：start workflow / 发 signal。

全部 gated by temporal_config.enabled —— 关闭时这些函数为 no-op 返回 False，
调用方据此回退旧链路（迁移期逃生开关）。

提供 async 核心 + sync 包装：FastAPI async 路由 await async 版；
apscheduler/同步路由用 sync 版（内部新建事件循环跑）。
"""
from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from temporalio.exceptions import WorkflowAlreadyStartedError

from core.temporal.client import (
    CODING_TASK_WORKFLOW,
    ISSUE_LINK_WORKFLOW,
    get_temporal_client,
    link_workflow_id,
    task_workflow_id,
)
from settings import temporal_config


def enabled() -> bool:
    return bool(temporal_config.enabled)


# ---------------- async 核心 ----------------
async def astart_issue_link(link_id: str) -> bool:
    """启动（或复用）某 issue link 的父 workflow。幂等：已存在则不重复启动。"""
    if not enabled():
        return False
    client = await get_temporal_client()
    try:
        await client.start_workflow(
            ISSUE_LINK_WORKFLOW,
            args=[link_id],
            id=link_workflow_id(link_id),
            task_queue=temporal_config.task_queue,
        )
    except WorkflowAlreadyStartedError:
        pass  # 幂等：已在编排中即视为成功，绝不能回退去 spawn 旧 watcher
    return True


async def astart_coding_task(task_id: str, link_id: str = "") -> bool:
    """启动（或复用）单个 coding task 的 workflow。幂等：已存在则不重复启动。

    手动创建的 task（非 issue-intake 路由）用此入口；workflow 启动后按 DB 现状自行推进。
    """
    if not enabled():
        return False
    client = await get_temporal_client()
    try:
        await client.start_workflow(
            CODING_TASK_WORKFLOW,
            args=[task_id, link_id],
            id=task_workflow_id(task_id),
            task_queue=temporal_config.task_queue,
        )
    except WorkflowAlreadyStartedError:
        pass  # 幂等：已在编排中即视为成功
    return True


async def asignal_coding_task(task_id: str, signal: str, *args: Any, start_if_absent: bool = False) -> bool:
    """给 CodingTaskWorkflow 发 signal。

    start_if_absent=True 时用 signal-with-start：workflow 不在则先起再发（防竞态）。
    默认 False：仅给已存在的 workflow 发（控制端点场景，workflow 应已由父流程/接管拉起）。
    """
    if not enabled():
        return False
    client = await get_temporal_client()
    if start_if_absent:
        await client.start_workflow(
            CODING_TASK_WORKFLOW,
            args=[task_id, ""],
            id=task_workflow_id(task_id),
            task_queue=temporal_config.task_queue,
            start_signal=signal,
            start_signal_args=list(args),
        )
        return True
    handle = client.get_workflow_handle(task_workflow_id(task_id))
    await handle.signal(signal, args=list(args))
    return True


async def asignal_issue_link(link_id: str, signal: str, *args: Any) -> bool:
    """给 IssueLinkWorkflow 发 signal（如 blueprint_reviewed）。仅给已存在的 workflow 发。"""
    if not enabled():
        return False
    client = await get_temporal_client()
    handle = client.get_workflow_handle(link_workflow_id(link_id))
    await handle.signal(signal, args=list(args))
    return True


# ---------------- sync 包装（无运行中事件循环的调用方用） ----------------
def _run_sync(coro) -> bool:
    try:
        return bool(asyncio.run(coro))
    except Exception as e:  # noqa: BLE001 - 触发失败不应打断主流程；记日志由上层决定回退
        logger.warning("[temporal] 触发失败: {}", e)
        return False


def start_issue_link_sync(link_id: str) -> bool:
    if not enabled():
        return False
    return _run_sync(astart_issue_link(link_id))


def start_coding_task_sync(task_id: str, link_id: str = "") -> bool:
    if not enabled():
        return False
    return _run_sync(astart_coding_task(task_id, link_id))


def signal_coding_task_sync(task_id: str, signal: str, *args: Any, start_if_absent: bool = False) -> bool:
    if not enabled():
        return False
    return _run_sync(asignal_coding_task(task_id, signal, *args, start_if_absent=start_if_absent))


def signal_issue_link_sync(link_id: str, signal: str, *args: Any) -> bool:
    if not enabled():
        return False
    return _run_sync(asignal_issue_link(link_id, signal, *args))
