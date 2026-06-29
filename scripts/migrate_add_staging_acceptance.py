#!/usr/bin/env python3
"""迁移：新增单测试环境 staging 验收表。

用法:
    python scripts/migrate_add_staging_acceptance.py

幂等：表已存在时跳过。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect  # noqa: E402

from core.database import engine  # noqa: E402
from core.models import StagingEventModel, StagingLockModel, StagingRunModel  # noqa: E402


TABLES = [StagingRunModel, StagingLockModel, StagingEventModel]


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


if __name__ == "__main__":
    main()
