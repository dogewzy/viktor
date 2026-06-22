#!/usr/bin/env python3
"""回填 Coding Task 当前人工 gate 处理人。

迁移脚本只补了字段，已有任务仍可能没有 pending_owner_mobile，导致“待我处理”
筛不到历史等待中的任务。本脚本按当前状态幂等回填：
- waiting_clarification：回到任务发起人手机号。
- waiting_plan_review：交给仓库 maintainer。
- plan_approved：等待 maintainer 启动执行。
- waiting_code_review：交给仓库 maintainer。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.database import SessionLocal
from core.models import CodingTaskModel
from core.registry import registry


def _repo_maintainer_mobile(project_id: str, repo_connector_id: str = "") -> str:
    connector = registry.get_repository_connector(project_id, repo_connector_id) if repo_connector_id else None
    if not connector:
        repos = registry.get_repository_connectors(project_id)
        connector = repos[0] if repos else None
    return str(getattr(connector, "maintainer_mobile", "") or "").strip() if connector else ""


def _repo_maintainer_label(row: CodingTaskModel, mobile: str) -> str:
    if not mobile:
        return ""
    return f"{row.repo_connector_id} maintainer" if row.repo_connector_id else "repo maintainer"


def _pending_for(row: CodingTaskModel) -> tuple[str, str, str]:
    status = str(row.status or "").strip()
    if status == "waiting_clarification":
        return "clarification", str(row.created_by_mobile or "").strip(), str(row.created_by or "").strip()
    if status == "waiting_plan_review":
        mobile = _repo_maintainer_mobile(row.project_id, row.repo_connector_id)
        return "plan_review", mobile, _repo_maintainer_label(row, mobile)
    if status == "plan_approved":
        mobile = _repo_maintainer_mobile(row.project_id, row.repo_connector_id)
        return "execution_start", mobile, _repo_maintainer_label(row, mobile)
    if status == "waiting_code_review":
        mobile = _repo_maintainer_mobile(row.project_id, row.repo_connector_id)
        return "code_review", mobile, _repo_maintainer_label(row, mobile)
    return "", "", ""


def main() -> None:
    db = SessionLocal()
    try:
        rows = (
            db.query(CodingTaskModel)
            .filter(CodingTaskModel.status.in_(["waiting_clarification", "waiting_plan_review", "plan_approved", "waiting_code_review"]))
            .all()
        )
        changed = 0
        skipped = 0
        for row in rows:
            gate, mobile, label = _pending_for(row)
            if not gate:
                skipped += 1
                continue
            if row.pending_gate == gate and row.pending_owner_mobile == mobile and row.pending_owner_label == label:
                skipped += 1
                continue
            row.pending_gate = gate
            row.pending_owner_mobile = mobile
            row.pending_owner_label = label
            changed += 1
        db.commit()
        print(f"回填完成：更新 {changed} 条，跳过 {skipped} 条")
    finally:
        db.close()


if __name__ == "__main__":
    main()
