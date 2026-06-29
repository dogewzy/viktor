"""Coding 自维护调度器。

职责（从 WatchdogScheduler / IssueIntakeScheduler 收归）：
- coding task 孤儿回收（reconcile_orphaned_coding_tasks）：周期 + 启动首跑
- MR 状态对账（reconcile_waiting_code_review）+ link 合并对账（reconcile_issue_links_merged）
- coding task 卡死扫描（scan_stuck_coding_tasks）

被调函数全部在 job 内部 lazy import，避免与 core.coding_service /
core.coding_review_reconciler / core.issue_intake_service 之间的循环 import。
"""
from __future__ import annotations

import threading

from apscheduler.schedulers.background import BackgroundScheduler
from loguru import logger

from settings import coding_agent_config


class CodingMaintenanceScheduler:
    """Coding 周期自维护调度器（独立于 watchdog / intake）。"""

    def __init__(self) -> None:
        self._scheduler: BackgroundScheduler | None = None
        self._lock = threading.Lock()
        self._started = False

    def start(self) -> None:
        """启动调度器：启动首跑 + 注册周期 job（幂等）。"""
        with self._lock:
            if self._started:
                return
            self._scheduler = BackgroundScheduler()
            self._scheduler.start()
            self._started = True

        # 启动首跑：立刻回收前任 pod 崩留的孤儿 coding task（Job 已不存在却卡在 running）。
        # grace=0：Job 仍存在的活任务会被 job_exists_for_task 跳过，安全。
        if coding_agent_config.reconcile_enabled:
            try:
                from core.coding_service import reconcile_orphaned_coding_tasks
                reconcile_orphaned_coding_tasks(grace_seconds=0)
            except Exception as e:  # noqa: BLE001
                logger.warning("启动时回收孤儿 coding task 失败，忽略: {}", e)

        registered: list[str] = []

        # 1. 孤儿回收周期任务（无参，走默认 grace）。
        if coding_agent_config.reconcile_enabled:
            self._scheduler.add_job(
                self._orphan_reconcile,
                "interval",
                seconds=coding_agent_config.reconcile_interval_seconds,
                id="coding_orphan_reconcile",
                max_instances=1,
                coalesce=True,
                replace_existing=True,
            )
            registered.append(
                f"coding_orphan_reconcile/{coding_agent_config.reconcile_interval_seconds}s"
            )

        # 2. MR 状态对账 + link 合并对账（webhook 漏发兜底）。
        self._scheduler.add_job(
            self._review_reconcile,
            "interval",
            seconds=600,
            id="coding_review_reconcile",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
        registered.append("coding_review_reconcile/600s")

        # 3. 卡死扫描（不必太频繁，300s）。
        self._scheduler.add_job(
            self._stuck_scan,
            "interval",
            seconds=300,
            id="coding_stuck_scan",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
        registered.append("coding_stuck_scan/300s")

        # 4. staging 锁租约兜底释放。
        self._scheduler.add_job(
            self._staging_lock_reconcile,
            "interval",
            seconds=300,
            id="staging_lock_reconcile",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
        registered.append("staging_lock_reconcile/300s")

        logger.info("Coding 自维护调度器已启动，注册 job: {}", ", ".join(registered))

    def _orphan_reconcile(self) -> None:
        try:
            from core.coding_service import reconcile_orphaned_coding_tasks
            reconcile_orphaned_coding_tasks()
        except Exception as e:  # noqa: BLE001
            logger.warning("[coding] 周期 reconcile 失败，忽略: {}", e)

    def _review_reconcile(self) -> None:
        # MR 状态对账兜底：webhook 漏发时纠正卡在 waiting_code_review 的任务。
        try:
            from core.coding_review_reconciler import reconcile_waiting_code_review
            reconcile_waiting_code_review()
        except Exception as e:  # noqa: BLE001
            logger.warning("MR 状态对账失败: {}", e)
        # link 侧合并对账：task 已合并但 issue 联动漏掉时，以 link 为锚补收尾（关 issue + 通知发起人）。
        try:
            from core.issue_intake_service import reconcile_issue_links_merged
            reconcile_issue_links_merged()
        except Exception as e:  # noqa: BLE001
            logger.warning("Issue link 合并对账失败: {}", e)

    def _stuck_scan(self) -> None:
        try:
            from core.coding_service import scan_stuck_coding_tasks
            scan_stuck_coding_tasks()
        except Exception as e:  # noqa: BLE001
            logger.warning("[coding] 卡死扫描失败，忽略: {}", e)

    def _staging_lock_reconcile(self) -> None:
        try:
            from core.staging_acceptance_service import reconcile_stale_staging_locks
            reconcile_stale_staging_locks()
        except Exception as e:  # noqa: BLE001
            logger.warning("[staging] 锁租约对账失败，忽略: {}", e)

    def shutdown(self) -> None:
        """停止调度器。"""
        with self._lock:
            if self._scheduler and self._started:
                self._scheduler.shutdown(wait=False)
                self._started = False
                logger.info("Coding 自维护调度器已停止")


coding_maintenance_scheduler = CodingMaintenanceScheduler()
