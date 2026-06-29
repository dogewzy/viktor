#!/usr/bin/env python3
"""迁移：新增 Coding Agent 后台任务相关表。

用法:
    python scripts/migrate_add_coding_agent.py

幂等：表已存在时跳过。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect

from core.database import engine
from core.models import (
    CodingArtifactModel,
    CodingAttemptModel,
    CodingEventModel,
    CodingTaskModel,
)


TABLES = [
    CodingTaskModel,
    CodingAttemptModel,
    CodingEventModel,
    CodingArtifactModel,
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


if __name__ == "__main__":
    main()
