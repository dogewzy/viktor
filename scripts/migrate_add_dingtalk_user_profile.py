#!/usr/bin/env python3
"""迁移：补充钉钉用户身份与部门画像字段。

字段用于把手机号作为唯一身份凭证，并保留钉钉 userid、部门路径和派生用户画像。
幂等：列或索引已存在时跳过；若历史数据存在重复手机号，唯一索引会由数据库拒绝。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect, text

from core.database import engine

TABLE = "viktor_users"


def _columns() -> set[str]:
    return {c["name"] for c in inspect(engine).get_columns(TABLE)}


def _indexes() -> list[dict]:
    return inspect(engine).get_indexes(TABLE)


def _index_names() -> set[str]:
    return {str(i["name"]) for i in _indexes()}


def _has_unique_index(columns: list[str]) -> bool:
    wanted = tuple(columns)
    for index in _indexes():
        if tuple(index.get("column_names") or []) == wanted and bool(index.get("unique")):
            return True
    return False


def _exec(sql: str) -> None:
    print(f"执行: {sql}")
    with engine.begin() as conn:
        conn.execute(text(sql))


def _add_column(column: str, ddl: str) -> None:
    if column in _columns():
        print(f"{TABLE}.{column} 已存在，跳过")
        return
    _exec(f"ALTER TABLE {TABLE} ADD COLUMN {ddl}")
    print(f"{TABLE}.{column} 添加完成")


def _add_index(index_name: str, ddl: str) -> None:
    if index_name in _index_names():
        print(f"{TABLE}.{index_name} 已存在，跳过")
        return
    _exec(f"ALTER TABLE {TABLE} ADD INDEX {index_name} {ddl}")
    print(f"{TABLE}.{index_name} 添加完成")


def _add_unique_mobile_index() -> None:
    if _has_unique_index(["mobile"]):
        print(f"{TABLE}.mobile 已有唯一索引，跳过")
        return
    _exec(f"ALTER TABLE {TABLE} ADD UNIQUE INDEX ux_viktor_users_mobile (mobile)")
    print(f"{TABLE}.ux_viktor_users_mobile 添加完成")


def main() -> None:
    _add_column("password_set", "password_set TINYINT NOT NULL DEFAULT 1 AFTER password_hash")
    _add_column("dingtalk_userid", "dingtalk_userid VARCHAR(128) NOT NULL DEFAULT '' AFTER mobile")
    _add_column("department_paths", "department_paths JSON NULL AFTER dingtalk_userid")
    _add_column("primary_department", "primary_department VARCHAR(512) NOT NULL DEFAULT '' AFTER department_paths")
    _add_column("profile_key", "profile_key VARCHAR(32) NOT NULL DEFAULT '' AFTER primary_department")
    _add_column("auth_source", "auth_source VARCHAR(32) NOT NULL DEFAULT 'local' AFTER profile_key")
    _add_index("ix_viktor_users_dingtalk_userid", "(dingtalk_userid)")
    _add_unique_mobile_index()


if __name__ == "__main__":
    main()
