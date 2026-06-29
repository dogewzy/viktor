"""Coding Job 入口：在独立 K8s Job 里执行 coding task 的 planning / execution。

由 `core/coding_job_dispatch.create_coding_job` 派发，容器内以
`python -m core.coding_job_runner <task_id> <planning|execution>` 启动。

职责（搬自原 web pod 的 `_run_planning_thread` / `_run_task_thread`）：
- 先 `registry.load_from_db()` 填充内存单例（_resolve_repo / _resolve_test_flow 依赖它）。
- 跑对应协程，复刻状态映射：InterruptedError→cancelled/paused、Exception→failed，均写 DB。
- 正常路径（含 cancel/paused/failed，只要 DB 写成功）一律退出码 0：DB 是事实源，
  backoffLimit=0 下非 0 退出会把 Job 标 Failed 干扰孤儿回收。只有连 DB 都没写成才非 0。
"""
from __future__ import annotations

import asyncio
import sys

from loguru import logger

from core.coding_service import (
    _control,
    _update_task,
    emit_event,
    mark_rate_limited,
    run_coding_planning,
    run_coding_task,
)
from core.llm_client import is_rate_limit_error
from core.registry import registry
from settings import temporal_config


def _handle_attempt_failure(task_id: str, mode: str, e: Exception) -> None:
    """attempt 异常收尾：限流类（且 Temporal 接管）→ rate_limited 可重试态，由 workflow 退避重派；
    其余 → failed 终态。Temporal 关闭时一律 failed（无编排重试，回退老行为）。"""
    if temporal_config.enabled and is_rate_limit_error(e):
        logger.warning("[coding-job] {} task {} 限流，标记 rate_limited 待编排退避重试: {}", mode, task_id, e)
        mark_rate_limited(task_id, mode, f"LLM 限流，等待退避重试：{e}")
        return
    logger.exception("[coding-job] {} task {} failed: {}", mode, task_id, e)
    _update_task(task_id, status="failed", stage="failed", message=str(e))
    emit_event(task_id, "failed", str(e), {"error": str(e)}, stage="failed")


def _run_planning(task_id: str) -> int:
    try:
        asyncio.run(run_coding_planning(task_id))
    except InterruptedError as e:
        control = _control(task_id)
        if control.get("cancel_requested"):
            _update_task(task_id, status="cancelled", stage="cancelled", message="任务已取消")
            emit_event(task_id, "cancelled", "任务已取消", {"reason": str(e)}, stage="cancelled")
        else:
            _handle_attempt_failure(task_id, "planning", e)
    except Exception as e:  # noqa: BLE001
        _handle_attempt_failure(task_id, "planning", e)
    return 0


def _run_execution(task_id: str) -> int:
    try:
        asyncio.run(run_coding_task(task_id))
    except InterruptedError as e:
        control = _control(task_id)
        if control.get("cancel_requested"):
            _update_task(task_id, status="cancelled", stage="cancelled", message="任务已取消")
            emit_event(task_id, "cancelled", "任务已取消", {"reason": str(e)}, stage="cancelled")
        elif control.get("pause_requested"):
            _update_task(task_id, status="paused", stage="paused", message="任务已暂停，可恢复后重新执行 attempt")
            emit_event(task_id, "paused", "任务已暂停，可恢复后重新执行 attempt", {"reason": str(e)}, stage="paused")
        else:
            _handle_attempt_failure(task_id, "execution", e)
    except Exception as e:  # noqa: BLE001
        _handle_attempt_failure(task_id, "execution", e)
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        logger.error("usage: python -m core.coding_job_runner <task_id> <planning|execution>")
        return 2
    task_id, mode = argv
    logger.info("[coding-job] start task={} mode={}", task_id, mode)
    # registry 是内存单例，只由 load_from_db 填充；新 Job 进程必须先加载。
    registry.load_from_db()
    if mode == "planning":
        return _run_planning(task_id)
    if mode == "execution":
        return _run_execution(task_id)
    logger.error("[coding-job] unknown mode: {}", mode)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
