#!/usr/bin/env python3
"""迁移：新增 GitLab Issue Intake 闭环相关表。

用法:
    python scripts/migrate_add_issue_intake.py

幂等：表已存在时跳过。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect  # noqa: E402

from core.database import SessionLocal  # noqa: E402
from core.database import engine  # noqa: E402
from core.models import (  # noqa: E402
    IssueIntakeConfigModel,
    IssueIntakeEventModel,
    IssueIntakeLinkModel,
    IssueIntakeTargetModel,
)


TABLES = [
    IssueIntakeConfigModel,
    IssueIntakeTargetModel,
    IssueIntakeLinkModel,
    IssueIntakeEventModel,
]


def main() -> None:
    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    for model in TABLES:
        table_name = model.__tablename__
        if table_name in existing:
            print(f"{table_name} 已存在，跳过")
            continue
        print(f"创建表: {table_name}")
        model.__table__.create(bind=engine)
        print(f"{table_name} 创建完成")
    _backfill_targets()


def _backfill_targets() -> None:
    db = SessionLocal()
    try:
        configs = db.query(IssueIntakeConfigModel).all()
        inserted = 0
        for cfg in configs:
            exists = (
                db.query(IssueIntakeTargetModel)
                .filter(IssueIntakeTargetModel.project_id == cfg.project_id)
                .first()
            )
            if exists or not cfg.default_repo_connector_id:
                continue
            db.add(IssueIntakeTargetModel(
                project_id=cfg.project_id,
                repo_connector_id=cfg.default_repo_connector_id,
                issue_project_url=cfg.issue_project_url,
                labels=[],
                enabled=cfg.enabled,
            ))
            inserted += 1
        db.commit()
        if inserted:
            print(f"已从旧配置回填 {inserted} 个 repo 级扫描目标")
    finally:
        db.close()


if __name__ == "__main__":
    main()
