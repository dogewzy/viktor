"""Issue Intake 后台扫描调度器。"""
from __future__ import annotations

from apscheduler.schedulers.background import BackgroundScheduler
from loguru import logger

from core.database import SessionLocal
from core.issue_intake_service import scan_project_issues
from core.models import IssueIntakeConfigModel


class IssueIntakeScheduler:
    def __init__(self) -> None:
        self._scheduler: BackgroundScheduler | None = None

    def start(self) -> None:
        if self._scheduler and self._scheduler.running:
            return
        self._scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
        self._scheduler.add_job(
            self._scan_all_enabled,
            "interval",
            seconds=300,
            id="issue_intake_scan_all",
            max_instances=1,
            coalesce=True,
        )
        # 通知 DLQ redrive：周期重投递死信队列中的失败通知。
        self._scheduler.add_job(
            self._redrive_dlq,
            "interval",
            seconds=300,
            id="notification_dlq_redrive",
            max_instances=1,
            coalesce=True,
        )
        self._scheduler.start()
        logger.info("Issue Intake 调度器已启动 (job: issue_intake_scan_all/300s, notification_dlq_redrive/300s)")

    def _redrive_dlq(self) -> None:
        try:
            from core.dingtalk_notifier import redrive_notification_dlq
            redrive_notification_dlq()
        except Exception as e:  # noqa: BLE001
            logger.warning("通知 DLQ redrive 失败: {}", e)

    def _scan_all_enabled(self) -> None:
        db = SessionLocal()
        try:
            rows = db.query(IssueIntakeConfigModel.project_id).filter(IssueIntakeConfigModel.enabled == 1).all()
            project_ids = [row[0] for row in rows]
        finally:
            db.close()
        for project_id in project_ids:
            try:
                scan_project_issues(project_id)
            except Exception as e:  # noqa: BLE001
                logger.warning("Issue Intake 扫描失败 project={}: {}", project_id, e)


issue_intake_scheduler = IssueIntakeScheduler()
