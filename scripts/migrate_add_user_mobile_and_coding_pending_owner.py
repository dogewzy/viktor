#!/usr/bin/env python3
"""迁移：补用户手机号与 Coding Task 当前处理人字段。

第一期先不做页面 RBAC，但后端需要具备角色/手机号/待办责任人的概念：
- viktor_users.mobile：注册用户的钉钉手机号，用于关联通知与“待我处理”。
- viktor_coding_tasks.created_by_mobile：任务发起人的手机号。
- viktor_coding_tasks.pending_gate / pending_owner_mobile / pending_owner_label：当前人工 gate 由谁处理。

用法:
    python scripts/migrate_add_user_mobile_and_coding_pending_owner.py

幂等：列或索引已存在时跳过。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect, text

from core.database import engine


def _columns(table: str) -> set[str]:
    return {c["name"] for c in inspect(engine).get_columns(table)}


def _indexes(table: str) -> set[str]:
    return {i["name"] for i in inspect(engine).get_indexes(table)}


def _add_column(table: str, column: str, ddl: str) -> None:
    existing = _columns(table)
    if column in existing:
        print(f"{table}.{column} 已存在，跳过")
        return
    sql = f"ALTER TABLE {table} ADD COLUMN {ddl}"
    print(f"执行: {sql}")
    with engine.begin() as conn:
        conn.execute(text(sql))
    print(f"{table}.{column} 添加完成")


def _add_index(table: str, index: str, ddl: str) -> None:
    existing = _indexes(table)
    if index in existing:
        print(f"{table}.{index} 已存在，跳过")
        return
    sql = f"ALTER TABLE {table} ADD INDEX {index} {ddl}"
    print(f"执行: {sql}")
    with engine.begin() as conn:
        conn.execute(text(sql))
    print(f"{table}.{index} 添加完成")


def main() -> None:
    _add_column("viktor_users", "mobile", "mobile VARCHAR(32) NOT NULL DEFAULT '' AFTER display_name")
    _add_index("viktor_users", "ix_viktor_users_mobile", "(mobile)")

    _add_column(
        "viktor_coding_tasks",
        "created_by_mobile",
        "created_by_mobile VARCHAR(32) NOT NULL DEFAULT '' AFTER created_by",
    )
    _add_column(
        "viktor_coding_tasks",
        "pending_gate",
        "pending_gate VARCHAR(64) NOT NULL DEFAULT '' AFTER created_by_mobile",
    )
    _add_column(
        "viktor_coding_tasks",
        "pending_owner_mobile",
        "pending_owner_mobile VARCHAR(32) NOT NULL DEFAULT '' AFTER pending_gate",
    )
    _add_column(
        "viktor_coding_tasks",
        "pending_owner_label",
        "pending_owner_label VARCHAR(128) NOT NULL DEFAULT '' AFTER pending_owner_mobile",
    )
    _add_index("viktor_coding_tasks", "ix_coding_task_pending_owner", "(pending_owner_mobile, status)")


if __name__ == "__main__":
    main()
