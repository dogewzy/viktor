"""big-bang 存量接管：把切换前已在飞的 link / coding task 拉进 Temporal 编排。

切换是 big-bang（无灰度），旧 watcher/reconciler 被同版本移除，故部署后必须一次性为
所有非终态 link / 独立 task 启动对应 workflow。workflow 启动后按 DB 现状自行跳到正确
await 点（派发前查 job_exists 防重复派发）。

幂等：workflow id == link_id / task_id；已在运行则 start 抛 WorkflowAlreadyStartedError，忽略。
在 worker 启动时调用一次（main 进程 temporal_config.enabled 时）。
"""
from __future__ import annotations

from loguru import logger

from core.database import SessionLocal
from core.temporal.client import (
    CODING_TASK_WORKFLOW,
    ISSUE_LINK_WORKFLOW,
    get_temporal_client,
    link_workflow_id,
    task_workflow_id,
)
from settings import temporal_config

# 终态：无需接管
_LINK_TERMINAL = {"issue_closed", "completed", "failed", "cancelled"}
_TASK_TERMINAL = {"completed", "failed", "cancelled", "plan_rejected"}


def _scan() -> tuple[list[str], list[str]]:
    """返回 (待接管 link_id 列表, 待接管的独立 task_id 列表)。

    独立 task = 非终态、且不属于任何 link（link 的子任务由父 workflow 负责，避免重复管理）。
    """
    from core.models import CodingTaskModel, IssueIntakeLinkModel

    db = SessionLocal()
    try:
        link_rows = db.query(IssueIntakeLinkModel).all()
        link_ids: list[str] = []
        child_task_ids: set[str] = set()
        for row in link_rows:
            if str(row.status or "") in _LINK_TERMINAL:
                continue
            link_ids.append(row.link_id)
            result = row.result if isinstance(row.result, dict) else {}
            for t in (result.get("coding_tasks") or []):
                if isinstance(t, dict) and t.get("coding_task_id"):
                    child_task_ids.add(str(t["coding_task_id"]))
            if row.coding_task_id:
                child_task_ids.add(str(row.coding_task_id))

        task_rows = db.query(CodingTaskModel).all()
        standalone: list[str] = [
            r.task_id for r in task_rows
            if str(r.status or "") not in _TASK_TERMINAL and r.task_id not in child_task_ids
        ]
    finally:
        db.close()
    return link_ids, standalone


async def adopt_inflight() -> dict[str, int]:
    """启动时一次性接管。返回 {links, tasks} 接管计数。"""
    if not temporal_config.enabled:
        return {"links": 0, "tasks": 0}

    from temporalio.exceptions import WorkflowAlreadyStartedError

    link_ids, task_ids = _scan()
    client = await get_temporal_client()
    tq = temporal_config.task_queue

    adopted_links = 0
    for link_id in link_ids:
        try:
            await client.start_workflow(
                ISSUE_LINK_WORKFLOW, args=[link_id], id=link_workflow_id(link_id), task_queue=tq
            )
            adopted_links += 1
        except WorkflowAlreadyStartedError:
            pass
        except Exception as e:  # noqa: BLE001 - 单条失败不阻断整体接管
            logger.warning("[temporal] 接管 link {} 失败: {}", link_id, e)

    adopted_tasks = 0
    for task_id in task_ids:
        try:
            await client.start_workflow(
                CODING_TASK_WORKFLOW, args=[task_id, ""], id=task_workflow_id(task_id), task_queue=tq
            )
            adopted_tasks += 1
        except WorkflowAlreadyStartedError:
            pass
        except Exception as e:  # noqa: BLE001
            logger.warning("[temporal] 接管 task {} 失败: {}", task_id, e)

    logger.info(
        "[temporal] 存量接管完成：links={}/{} tasks={}/{}",
        adopted_links, len(link_ids), adopted_tasks, len(task_ids),
    )
    return {"links": adopted_links, "tasks": adopted_tasks}
