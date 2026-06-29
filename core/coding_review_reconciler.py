"""Coding Task ↔ GitLab MR 状态对账（轮询兜底）。

webhook 是 MR 状态变更同步到 coding task 的主路径；但 webhook 可能漏发或未配置，
导致任务永久卡在 waiting_code_review。本模块定期拉取这些任务对应 MR 的真实状态，
按 merged / closed 修正，复用与 webhook 相同的下游闭环逻辑。
"""
from __future__ import annotations

from typing import Any

from loguru import logger

from core.coding_service import (
    _resolve_repo,
    close_code_review_by_mr_closed,
    complete_code_review,
)
from core.database import SessionLocal
from core.models import CodingTaskModel
from gitlab.merge_request_service import get_merge_request


def _synthetic_payload(mr: dict[str, Any], action: str, state: str) -> dict[str, Any]:
    """构造与 GitLab MR webhook 对齐的 payload，供 issue 侧 handler 复用。"""
    return {
        "object_kind": "merge_request",
        "object_attributes": {
            "action": action,
            "state": state,
            "iid": mr.get("iid"),
            "url": mr.get("web_url"),
            "web_url": mr.get("web_url"),
            "source_branch": mr.get("source_branch"),
            "target_project_id": mr.get("project_id"),
            "project_id": mr.get("project_id"),
            "merge_commit_sha": mr.get("merge_commit_sha"),
            "updated_at": mr.get("updated_at"),
        },
    }


def reconcile_waiting_code_review() -> dict[str, Any]:
    """扫描所有 waiting_code_review 任务，按 MR 真实状态对账。"""
    # Temporal 接管后：MR 合并/关闭由 CodingTaskWorkflow 的合并 gate（webhook signal + 轮询兜底）
    # 负责，旧对账停手避免双重 complete/close。
    from settings import temporal_config
    if temporal_config.enabled:
        return {"checked": 0, "updated": 0}

    db = SessionLocal()
    try:
        rows = (
            db.query(CodingTaskModel)
            .filter(CodingTaskModel.status == "waiting_code_review")
            .limit(500)
            .all()
        )
        targets = [
            (row.task_id, row.project_id, row.repo_connector_id, dict(row.result or {}))
            for row in rows
        ]
    finally:
        db.close()

    checked = 0
    completed = 0
    cancelled = 0
    for task_id, project_id, repo_connector_id, result in targets:
        mr = result.get("mr") if isinstance(result.get("mr"), dict) else {}
        mr_iid = mr.get("iid")
        if not mr_iid:
            continue
        try:
            repo_url, _, _ = _resolve_repo(project_id, repo_connector_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("[reconcile] 解析仓库失败 task={}: {}", task_id, e)
            continue
        try:
            remote = get_merge_request(repo_url=repo_url, merge_request_iid=mr_iid)
        except Exception as e:  # noqa: BLE001
            logger.warning("[reconcile] 查询 MR 失败 task={} iid={}: {}", task_id, mr_iid, e)
            continue
        checked += 1
        remote_state = str(remote.get("state") or "").lower()
        if remote_state == "merged":
            payload = _synthetic_payload(remote, "merge", "merged")
            try:
                complete_code_review(
                    task_id,
                    reviewer="reconciler",
                    comment="对账：MR 已合并",
                    merge_payload=payload["object_attributes"],
                )
                completed += 1
                _sync_issue_merged(payload, task_id)
            except ValueError:
                continue
        elif remote_state == "closed":
            payload = _synthetic_payload(remote, "close", "closed")
            try:
                close_code_review_by_mr_closed(
                    task_id,
                    reviewer="reconciler",
                    comment="对账：MR 已关闭（未合并）",
                    close_payload=payload["object_attributes"],
                )
                cancelled += 1
                _sync_issue_closed(payload, task_id)
            except ValueError:
                continue
        # opened / locked 等：保持 waiting_code_review，不动。

    if checked:
        logger.info(
            "[reconcile] waiting_code_review 对账完成 checked={} completed={} cancelled={}",
            checked, completed, cancelled,
        )
    return {"checked": checked, "completed": completed, "cancelled": cancelled}


def _sync_issue_merged(payload: dict[str, Any], task_id: str) -> None:
    try:
        from core.issue_intake_service import handle_merge_request_merged

        handle_merge_request_merged(payload, [task_id])
    except Exception as e:  # noqa: BLE001
        logger.exception("[reconcile] issue merged 联动失败 task={}: {}", task_id, e)


def _sync_issue_closed(payload: dict[str, Any], task_id: str) -> None:
    try:
        from core.issue_intake_service import handle_merge_request_closed

        handle_merge_request_closed(payload, [task_id])
    except Exception as e:  # noqa: BLE001
        logger.exception("[reconcile] issue closed 联动失败 task={}: {}", task_id, e)
