#!/usr/bin/env python3
"""迁移：新增 Watchdog 监控子系统相关表，以及 Skill 表新增 scope 字段。

用法:
    python scripts/migrate_add_watchdog.py

幂等：表/列已存在时跳过。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import Column, String, inspect, text  # noqa: E402

from core.database import engine  # noqa: E402
from core.models import WatchdogEventModel, WatchdogModel  # noqa: E402

TABLES = [WatchdogModel, WatchdogEventModel]


def _add_column_if_not_exists(
    inspector, table_name: str, column_name: str, column: Column
) -> None:
    """如果列不存在则添加。"""
    existing_columns = {c["name"] for c in inspector.get_columns(table_name)}
    if column_name in existing_columns:
        print(f"  列 {table_name}.{column_name} 已存在，跳过")
        return
    col_type = column.type.compile(dialect=engine.dialect)
    default = ""
    if column.default is not None:
        raw = column.default.arg
        if isinstance(raw, str):
            default = f" DEFAULT '{raw}'"
        else:
            default = f" DEFAULT '{raw}'" if raw else ""
    nullable = "" if column.nullable else " NOT NULL"
    ddl = f"ALTER TABLE {table_name} ADD COLUMN {column_name} {col_type}{nullable}{default}"
    with engine.begin() as conn:
        conn.execute(text(ddl))
    print(f"  列 {table_name}.{column_name} 已添加")


def main() -> None:
    inspector = inspect(engine)
    existing = set(inspector.get_table_names())

    # 1. 创建新表
    for model in TABLES:
        table_name = model.__tablename__
        if table_name in existing:
            print(f"{table_name} 已存在，跳过")
            continue
        print(f"创建表: {table_name}")
        model.__table__.create(bind=engine)
        print(f"{table_name} 创建完成")

    # 2. 给 viktor_skills 表添加 scope 列（如果表存在）
    skills_table = "viktor_skills"
    if skills_table in existing:
        _add_column_if_not_exists(
            inspector,
            skills_table,
            "scope",
            Column("scope", String(16), nullable=False, default="project"),
        )


if __name__ == "__main__":
    main()
