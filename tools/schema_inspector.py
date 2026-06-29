"""
数据库 Schema 自省工具。

给 LLM 提供运行时探索 schema 的能力：
- list_tables(project_id, connector_id)            列出库中全部表与表注释
- describe_table(project_id, connector_id, table)  返回指定表的字段/主键/索引
- sample_rows(project_id, connector_id, table, limit) 抽样若干行

所有查询走 information_schema（MySQL），共用 sql_executor.engine_context 的
直连 / SSH 隧道分派逻辑；结果进程内缓存，TTL 由 agent_config.schema_cache_ttl 控制。
"""
import re
import time
from typing import Any

from loguru import logger
from sqlalchemy import text

from core.registry import registry
from settings import agent_config
from tools.sql_executor import engine_context

# 缓存结构：
#   _cache[(project_id, connector_id)] = {
#       "tables":  [ {table, comment}, ... ]            或 None（未加载）
#       "columns": { table_name: {...} }                按需加载
#       "tables_ts":  timestamp（"tables" 的加载时间）
#       "columns_ts": { table_name: timestamp }
#   }
_cache: dict[tuple[str, str], dict[str, Any]] = {}

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _safe_ident(name: str) -> str:
    """校验标识符合法性，防止表名/列名注入。"""
    if not _IDENT_RE.match(name or ""):
        raise ValueError(f"非法标识符：{name!r}")
    return name


def _bucket(project_id: str, connector_id: str) -> dict[str, Any]:
    key = (project_id, connector_id)
    b = _cache.get(key)
    if b is None:
        b = {"tables": None, "columns": {}, "tables_ts": 0.0, "columns_ts": {}}
        _cache[key] = b
    return b


def invalidate(project_id: str, connector_id: str) -> None:
    """手动失效某数据库连接器的 schema 缓存。"""
    _cache.pop((project_id, connector_id), None)


def _ttl_valid(ts: float) -> bool:
    return (time.time() - ts) < agent_config.schema_cache_ttl


# ============================================================
# list_tables
# ============================================================

def list_tables(project_id: str, connector_id: str) -> str:
    """列出指定数据库连接器当前库下全部表（带表注释）。"""
    database_connector = registry._database_connectors.get(project_id, {}).get(connector_id)
    if not database_connector:
        return f"错误：数据库连接器 '{connector_id}' 在项目 '{project_id}' 中未注册"

    b = _bucket(project_id, connector_id)
    tables = b["tables"]
    if tables is None or not _ttl_valid(b["tables_ts"]):
        try:
            with engine_context(project_id, connector_id) as (engine, ds):
                with engine.connect() as conn:
                    result = conn.execute(
                        text(
                            "SELECT TABLE_NAME, TABLE_COMMENT "
                            "FROM information_schema.TABLES "
                            "WHERE TABLE_SCHEMA = :db "
                            "ORDER BY TABLE_NAME"
                        ),
                        {"db": ds.database},
                    )
                    tables = [
                        {"table": row[0], "comment": row[1] or ""}
                        for row in result.fetchall()
                    ]
            b["tables"] = tables
            b["tables_ts"] = time.time()
            logger.info(
                "schema 缓存已刷新(tables): project={} ds={} count={}",
                project_id, connector_id, len(tables),
            )
        except Exception as e:  # noqa: BLE001
            logger.error("list_tables 失败 project={} ds={} error={}", project_id, connector_id, e)
            return f"获取表列表失败：{e}"

    if not tables:
        return f"数据库 `{database_connector.database}` 下无表"

    lines = [f"数据库 `{database_connector.database}` 共 {len(tables)} 张表："]
    for t in tables:
        if t["comment"]:
            lines.append(f"- {t['table']}  —  {t['comment']}")
        else:
            lines.append(f"- {t['table']}")
    return "\n".join(lines)


# ============================================================
# describe_table
# ============================================================

def describe_table(project_id: str, connector_id: str, table: str) -> str:
    """返回指定表的字段明细、主键和索引。"""
    database_connector = registry._database_connectors.get(project_id, {}).get(connector_id)
    if not database_connector:
        return f"错误：数据库连接器 '{connector_id}' 在项目 '{project_id}' 中未注册"

    try:
        _safe_ident(table)
    except ValueError as e:
        return f"错误：{e}"

    b = _bucket(project_id, connector_id)
    cached = b["columns"].get(table)
    cached_ts = b["columns_ts"].get(table, 0.0)
    if cached is None or not _ttl_valid(cached_ts):
        try:
            with engine_context(project_id, connector_id) as (engine, ds):
                with engine.connect() as conn:
                    cols_result = conn.execute(
                        text(
                            "SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, "
                            "COLUMN_DEFAULT, COLUMN_KEY, EXTRA, COLUMN_COMMENT "
                            "FROM information_schema.COLUMNS "
                            "WHERE TABLE_SCHEMA = :db AND TABLE_NAME = :tbl "
                            "ORDER BY ORDINAL_POSITION"
                        ),
                        {"db": ds.database, "tbl": table},
                    )
                    columns = [
                        {
                            "name": row[0],
                            "type": row[1],
                            "nullable": row[2] == "YES",
                            "default": row[3],
                            "key": row[4] or "",
                            "extra": row[5] or "",
                            "comment": row[6] or "",
                        }
                        for row in cols_result.fetchall()
                    ]
                    idx_result = conn.execute(
                        text(
                            "SELECT INDEX_NAME, NON_UNIQUE, COLUMN_NAME, SEQ_IN_INDEX "
                            "FROM information_schema.STATISTICS "
                            "WHERE TABLE_SCHEMA = :db AND TABLE_NAME = :tbl "
                            "ORDER BY INDEX_NAME, SEQ_IN_INDEX"
                        ),
                        {"db": ds.database, "tbl": table},
                    )
                    idx_map: dict[str, dict[str, Any]] = {}
                    for iname, non_unique, cname, _seq in idx_result.fetchall():
                        idx = idx_map.setdefault(
                            iname, {"name": iname, "unique": (non_unique == 0), "columns": []}
                        )
                        idx["columns"].append(cname)
                    indexes = list(idx_map.values())

            if not columns:
                return f"表 `{database_connector.database}.{table}` 不存在或无字段"

            cached = {"columns": columns, "indexes": indexes}
            b["columns"][table] = cached
            b["columns_ts"][table] = time.time()
            logger.info(
                "schema 缓存已刷新(columns): project={} ds={} table={} columns={} indexes={}",
                project_id, connector_id, table, len(columns), len(indexes),
            )
        except Exception as e:  # noqa: BLE001
            logger.error(
                "describe_table 失败 project={} ds={} table={} error={}",
                project_id, connector_id, table, e,
            )
            return f"获取表结构失败：{e}"

    columns = cached["columns"]
    indexes = cached["indexes"]

    lines = [f"表 `{database_connector.database}.{table}` 字段（共 {len(columns)} 列）："]
    for col in columns:
        parts = [
            col["name"],
            col["type"],
            "NULL" if col["nullable"] else "NOT NULL",
        ]
        if col["default"] is not None:
            parts.append(f"DEFAULT {col['default']}")
        if col["key"] == "PRI":
            parts.append("PK")
        elif col["key"] == "UNI":
            parts.append("UNIQUE")
        elif col["key"] == "MUL":
            parts.append("INDEX")
        if col["extra"]:
            parts.append(col["extra"])
        line = " | ".join(parts)
        if col["comment"]:
            line += f"  —  {col['comment']}"
        lines.append(f"- {line}")

    if indexes:
        lines.append("")
        lines.append("索引：")
        for idx in indexes:
            uniq = "UNIQUE " if idx["unique"] else ""
            lines.append(f"- {uniq}{idx['name']} ({', '.join(idx['columns'])})")

    return "\n".join(lines)


# ============================================================
# sample_rows
# ============================================================

def sample_rows(project_id: str, connector_id: str, table: str, limit: int = 5) -> str:
    """抽样若干行（LIMIT 受 agent_config.sample_row_limit 限制）。"""
    try:
        safe_table = _safe_ident(table)
    except ValueError as e:
        return f"错误：{e}"

    max_limit = agent_config.sample_row_limit
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 5
    if limit <= 0:
        limit = 5
    if limit > max_limit:
        limit = max_limit

    # 直接走 run_select，复用安全校验 / 审计日志 / 结果截断
    from tools.sql_executor import run_select

    sql = f"SELECT * FROM `{safe_table}` LIMIT :__sample_limit"
    return run_select(
        project_id,
        connector_id,
        sql,
        params={"__sample_limit": limit},
        source=f"sample_rows:{safe_table}",
        auto_limit=False,
    )
