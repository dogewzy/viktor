"""Watchdog CRUD API 路由。

提供 Watchdog 的注册/更新/删除/查询，以及手动触发和事件历史查询。
"""
from __future__ import annotations

import threading
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from loguru import logger
from pydantic import BaseModel, Field

from core.database import SessionLocal
from core.models import CodingTaskModel, WatchdogEventModel, WatchdogModel
from core.registry import WatchdogItem, WatchdogNotificationTarget, registry
from core.watchdog import watchdog_scheduler

router = APIRouter(prefix="/api/v1/watchdog", tags=["Watchdog"])


# ────────────────────────────────────────────────────────────
# Request / Response 模型
# ────────────────────────────────────────────────────────────

class WatchdogCreateRequest(BaseModel):
    """创建/更新 Watchdog 请求体。"""
    watchdog_id: str
    project_id: str
    name: str = ""
    description: str = ""
    probe: dict[str, Any]
    schedule: str  # cron 表达式 (5段)
    skill_ids: list[str] = Field(default_factory=list)
    notification: dict[str, Any]
    severity_filter: list[str] = Field(default_factory=lambda: ["critical"])
    auto_coding_plan: bool = False
    coding_repo_connector_id: str = ""
    cooldown_minutes: int = 30
    max_execution_sec: int = 300
    enabled: bool = True


class WatchdogTriggerRequest(BaseModel):
    """手动触发 Watchdog 请求体。"""
    project_id: str
    watchdog_id: str


def _watchdog_payload(item: WatchdogItem) -> dict[str, Any]:
    """Return a UI/API friendly payload while preserving the registry model fields."""
    payload = item.model_dump()
    payload["watchdog_id"] = item.id
    payload["enabled"] = item.status == "enabled"
    return payload


# ────────────────────────────────────────────────────────────
# CRUD 接口
# ────────────────────────────────────────────────────────────

@router.post("", summary="注册/更新 Watchdog")
def create_or_update_watchdog(req: WatchdogCreateRequest) -> dict:
    """注册或更新一个 Watchdog 监控项。同时写入内存 Registry 和 DB。"""
    # 验证项目存在
    if not registry.get_project(req.project_id):
        raise HTTPException(status_code=404, detail=f"项目 '{req.project_id}' 不存在")

    # 验证 cron 表达式
    try:
        from apscheduler.triggers.cron import CronTrigger
        CronTrigger.from_crontab(req.schedule)
    except (ValueError, TypeError) as e:
        raise HTTPException(status_code=400, detail=f"无效的 cron 表达式: {e}")

    # 构建 WatchdogItem 并注册到 Registry
    try:
        item = WatchdogItem(
            id=req.watchdog_id,
            project_id=req.project_id,
            name=req.name,
            description=req.description,
            probe=req.probe,
            schedule=req.schedule,
            skill_ids=req.skill_ids,
            notification=req.notification,
            severity_filter=req.severity_filter,
            auto_coding_plan=req.auto_coding_plan,
            coding_repo_connector_id=req.coding_repo_connector_id,
            cooldown_minutes=req.cooldown_minutes,
            max_execution_sec=req.max_execution_sec,
            status="enabled" if req.enabled else "disabled",
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"参数校验失败: {e}")

    registry.register_watchdog(item)
    probe_payload = item.probe.model_dump(exclude_none=True)

    # 持久化到 DB
    db = SessionLocal()
    try:
        row = db.get(WatchdogModel, (req.project_id, req.watchdog_id))
        if row:
            row.name = req.name
            row.description = req.description
            row.probe = probe_payload
            row.schedule = req.schedule
            row.skill_ids = req.skill_ids
            row.notification = req.notification
            row.severity_filter = req.severity_filter
            row.auto_coding_plan = int(req.auto_coding_plan)
            row.coding_repo_connector_id = req.coding_repo_connector_id
            row.cooldown_minutes = req.cooldown_minutes
            row.max_execution_sec = req.max_execution_sec
            row.enabled = int(req.enabled)
        else:
            db.add(WatchdogModel(
                project_id=req.project_id,
                watchdog_id=req.watchdog_id,
                name=req.name,
                description=req.description,
                probe=probe_payload,
                schedule=req.schedule,
                skill_ids=req.skill_ids,
                notification=req.notification,
                severity_filter=req.severity_filter,
                auto_coding_plan=int(req.auto_coding_plan),
                coding_repo_connector_id=req.coding_repo_connector_id,
                cooldown_minutes=req.cooldown_minutes,
                max_execution_sec=req.max_execution_sec,
                enabled=int(req.enabled),
            ))
        db.commit()
    finally:
        db.close()

    # 更新调度器 job
    if req.enabled:
        watchdog_scheduler.add_job(item)
    else:
        watchdog_scheduler.remove_job(req.project_id, req.watchdog_id)

    return {"ok": True, "message": f"Watchdog '{req.watchdog_id}' 注册成功"}


@router.delete("/{project_id}/{watchdog_id}", summary="删除 Watchdog")
def delete_watchdog(project_id: str, watchdog_id: str) -> dict:
    """删除一个 Watchdog 并移除其调度 job。"""
    # 从调度器移除
    watchdog_scheduler.remove_job(project_id, watchdog_id)
    # 从 Registry 移除
    registry.unregister_watchdog(project_id, watchdog_id)
    # 从 DB 删除
    db = SessionLocal()
    try:
        row = db.get(WatchdogModel, (project_id, watchdog_id))
        if row:
            db.delete(row)
            db.commit()
    finally:
        db.close()
    return {"ok": True, "message": f"Watchdog '{watchdog_id}' 已删除"}


@router.get("/{project_id}", summary="列出项目的所有 Watchdog")
def list_watchdogs(project_id: str) -> dict:
    """获取指定项目的所有 Watchdog 列表。"""
    items = registry.get_watchdogs(project_id)
    return {
        "ok": True,
        "items": [_watchdog_payload(item) for item in items],
        "total": len(items),
    }


@router.get("/{project_id}/{watchdog_id}", summary="获取单个 Watchdog 详情")
def get_watchdog(project_id: str, watchdog_id: str) -> dict:
    """获取 Watchdog 详细信息。"""
    item = registry.get_watchdog(project_id, watchdog_id)
    if not item:
        raise HTTPException(status_code=404, detail=f"Watchdog '{watchdog_id}' 不存在")
    return {"ok": True, "item": _watchdog_payload(item)}


# ────────────────────────────────────────────────────────────
# 手动触发
# ────────────────────────────────────────────────────────────

@router.post("/trigger", summary="手动触发 Watchdog")
def trigger_watchdog(req: WatchdogTriggerRequest) -> dict:
    """立即触发一次 Watchdog 执行（绕过调度器，不检查冷却）。"""
    import asyncio
    from core.watchdog import _run_single_watchdog

    item = registry.get_watchdog(req.project_id, req.watchdog_id)
    if not item:
        raise HTTPException(status_code=404, detail=f"Watchdog '{req.watchdog_id}' 不存在")

    def _run():
        asyncio.run(_run_single_watchdog(item))

    thread = threading.Thread(target=_run, daemon=True, name=f"watchdog-trigger-{req.watchdog_id}")
    thread.start()
    return {"ok": True, "message": f"Watchdog '{req.watchdog_id}' 已触发执行"}


@router.post("/{project_id}/{watchdog_id}/trigger", summary="手动触发 Watchdog")
def trigger_watchdog_by_path(project_id: str, watchdog_id: str) -> dict:
    """Path-style trigger endpoint used by the admin UI."""
    return trigger_watchdog(WatchdogTriggerRequest(project_id=project_id, watchdog_id=watchdog_id))


# ────────────────────────────────────────────────────────────
# 事件历史
# ────────────────────────────────────────────────────────────

@router.get("/{project_id}/{watchdog_id}/events", summary="查询 Watchdog 事件历史")
def list_watchdog_events(
    project_id: str,
    watchdog_id: str,
    limit: int = Query(default=20, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """获取 Watchdog 的执行事件历史。"""
    db = SessionLocal()
    try:
        query = (
            db.query(WatchdogEventModel)
            .filter(
                WatchdogEventModel.project_id == project_id,
                WatchdogEventModel.watchdog_id == watchdog_id,
            )
            .order_by(WatchdogEventModel.started_at.desc())
        )
        total = query.count()
        rows = query.offset(offset).limit(limit).all()
        items = []
        for row in rows:
            coding_task_info = {}
            probe_result = row.probe_result if isinstance(row.probe_result, dict) else {}
            if isinstance(probe_result.get("coding_task"), dict):
                coding_task_info = dict(probe_result.get("coding_task") or {})
            if row.coding_task_id:
                task = db.get(CodingTaskModel, row.coding_task_id)
                if task:
                    current_task_info = {
                        "task_id": task.task_id,
                        "status": task.status,
                        "stage": task.stage,
                        "message": task.message,
                    }
                    if coding_task_info:
                        coding_task_info["current_status"] = task.status
                        coding_task_info["current_stage"] = task.stage
                        coding_task_info["current_message"] = task.message
                    else:
                        coding_task_info.update(current_task_info)
            items.append({
                "id": row.id,
                "project_id": row.project_id,
                "watchdog_id": row.watchdog_id,
                "status": row.status,
                "is_anomaly": bool(row.is_anomaly),
                "severity": row.severity,
                "conclusion": row.conclusion,
                "evidence": row.evidence or [],
                "action_type": row.action_type,
                "coding_task_id": row.coding_task_id,
                "coding_task": coding_task_info,
                "notification_sent": bool(row.notification_sent),
                "duration_ms": row.duration_ms,
                "started_at": str(row.started_at) if row.started_at else None,
                "completed_at": str(row.completed_at) if row.completed_at else None,
            })
        return {"ok": True, "items": items, "total": total}
    finally:
        db.close()
