#!/usr/bin/env python3
"""迁移 Connector 术语相关表：先迁移旧表数据，再删除旧表。

迁移关系：
- viktor_project_repos      -> viktor_repository_connectors
- viktor_datasources        -> viktor_database_connectors
- viktor_sls_log_sources    -> viktor_log_connectors

用法:
    python scripts/migrate_connector_terminology.py

幂等策略：
- 新表不存在则创建。
- 迁移数据使用 INSERT IGNORE，已存在的新主键不会重复写入。
- 旧表仅在数据迁移语句执行后删除；旧表不存在则跳过。
"""
from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import inspect, text

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.database import engine
from core.models import DatabaseConnectorModel, LogConnectorModel, RepositoryConnectorModel


OLD_REPOSITORY_TABLE = "viktor_project_repos"
NEW_REPOSITORY_TABLE = "viktor_repository_connectors"
OLD_DATABASE_TABLE = "viktor_datasources"
NEW_DATABASE_TABLE = "viktor_database_connectors"
OLD_LOG_TABLE = "viktor_sls_log_sources"
NEW_LOG_TABLE = "viktor_log_connectors"


def _has_table(table: str) -> bool:
    return inspect(engine).has_table(table)


def _columns(table: str) -> set[str]:
    if not _has_table(table):
        return set()
    return {col["name"] for col in inspect(engine).get_columns(table)}


def _ensure_tables() -> None:
    print("=== 确保新 Connector 表存在 ===")
    if not _has_table(NEW_REPOSITORY_TABLE):
        RepositoryConnectorModel.__table__.create(engine)
        print(f"创建表: {NEW_REPOSITORY_TABLE}")
    else:
        print(f"表已存在: {NEW_REPOSITORY_TABLE}")

    if not _has_table(NEW_DATABASE_TABLE):
        DatabaseConnectorModel.__table__.create(engine)
        print(f"创建表: {NEW_DATABASE_TABLE}")
    else:
        print(f"表已存在: {NEW_DATABASE_TABLE}")

    if not _has_table(NEW_LOG_TABLE):
        LogConnectorModel.__table__.create(engine)
        print(f"创建表: {NEW_LOG_TABLE}")
    else:
        print(f"表已存在: {NEW_LOG_TABLE}")

    cols = _columns(NEW_DATABASE_TABLE)
    if "ssh_tunnel" not in cols:
        ddl = f"ALTER TABLE {NEW_DATABASE_TABLE} ADD COLUMN ssh_tunnel JSON NULL AFTER charset_name"
        with engine.begin() as conn:
            conn.execute(text(ddl))
        print(f"添加列: {NEW_DATABASE_TABLE}.ssh_tunnel")


def _migrate_repository_connectors() -> None:
    if not _has_table(OLD_REPOSITORY_TABLE):
        print(f"旧表不存在，跳过: {OLD_REPOSITORY_TABLE}")
        return

    print(f"=== 迁移 {OLD_REPOSITORY_TABLE} -> {NEW_REPOSITORY_TABLE} ===")
    sql = f"""
        INSERT IGNORE INTO {NEW_REPOSITORY_TABLE}
            (project_id, connector_id, display_name, git_url, default_branch, k8s_workload, sort_order, created_at, updated_at)
        SELECT
            project_id, repo_id, display_name, git_url, default_branch, k8s_workload, sort_order, created_at, updated_at
        FROM {OLD_REPOSITORY_TABLE}
    """
    with engine.begin() as conn:
        result = conn.execute(text(sql))
        print(f"迁移 Repository Connector 行数: {result.rowcount}")
        conn.execute(text(f"DROP TABLE {OLD_REPOSITORY_TABLE}"))
        print(f"已删除旧表: {OLD_REPOSITORY_TABLE}")


def _migrate_database_connectors() -> None:
    if not _has_table(OLD_DATABASE_TABLE):
        print(f"旧表不存在，跳过: {OLD_DATABASE_TABLE}")
        return

    print(f"=== 迁移 {OLD_DATABASE_TABLE} -> {NEW_DATABASE_TABLE} ===")
    old_cols = _columns(OLD_DATABASE_TABLE)
    ssh_expr = "ssh_tunnel" if "ssh_tunnel" in old_cols else "NULL"
    sql = f"""
        INSERT IGNORE INTO {NEW_DATABASE_TABLE}
            (project_id, connector_id, type, host, port, username, password, database_name,
             readonly_flag, charset_name, ssh_tunnel, created_at, updated_at)
        SELECT
            project_id, datasource_id, type, host, port, username, password, database_name,
            readonly_flag, charset_name, {ssh_expr}, created_at, updated_at
        FROM {OLD_DATABASE_TABLE}
    """
    with engine.begin() as conn:
        result = conn.execute(text(sql))
        print(f"迁移 Database Connector 行数: {result.rowcount}")
        conn.execute(text(f"DROP TABLE {OLD_DATABASE_TABLE}"))
        print(f"已删除旧表: {OLD_DATABASE_TABLE}")


def _migrate_log_connectors() -> None:
    if not _has_table(OLD_LOG_TABLE):
        print(f"旧表不存在，跳过: {OLD_LOG_TABLE}")
        return

    print(f"=== 迁移 {OLD_LOG_TABLE} -> {NEW_LOG_TABLE} ===")
    old_cols = _columns(OLD_LOG_TABLE)
    connector_expr = "source_id" if "source_id" in old_cols else "connector_id"
    display_expr = "display_name" if "display_name" in old_cols else "''"
    description_expr = "description" if "description" in old_cols else "''"
    enabled_expr = "enabled" if "enabled" in old_cols else "1"
    sql = f"""
        INSERT IGNORE INTO {NEW_LOG_TABLE}
            (project_id, connector_id, display_name, sls_project, logstore, description, enabled, created_at, updated_at)
        SELECT
            project_id, {connector_expr}, {display_expr}, sls_project, logstore, {description_expr}, {enabled_expr}, created_at, updated_at
        FROM {OLD_LOG_TABLE}
    """
    with engine.begin() as conn:
        result = conn.execute(text(sql))
        print(f"迁移 Log Connector 行数: {result.rowcount}")
        conn.execute(text(f"DROP TABLE {OLD_LOG_TABLE}"))
        print(f"已删除旧表: {OLD_LOG_TABLE}")


def main() -> None:
    _ensure_tables()
    _migrate_repository_connectors()
    _migrate_database_connectors()
    _migrate_log_connectors()
    print("✅ Connector 术语表迁移完成")


if __name__ == "__main__":
    main()
