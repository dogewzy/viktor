"""
通用 SQL 执行器。

安全设计：
- 只允许 SELECT 语句（强制 SELECT 开头 + 危险关键字黑名单）
- 单语句限制（禁止 ; 拼接多条 SQL）
- 长度限制（防止超长 SQL 爆炸）
- 所有查询通过 SQLAlchemy text() + 参数绑定，防注入
- Free-SQL 路径自动注入 LIMIT（若用户未写）
- 结果行数硬截断（防止返回过多数据）
- 执行审计日志

连接方式（优先级：数据库连接器自带配置）：
- database_connector.ssh_tunnel 为 None -> 直连（默认）
- database_connector.ssh_tunnel 有值 -> 通过 SSH 隧道，缺省字段从全局 ssh_tunnel_config 回退

对外主要入口：
- engine_context(project_id, connector_id)   底层连接上下文，可被 schema_inspector 复用
- run_select(project_id, connector_id, sql, params, *, source, auto_limit)   统一执行入口
- execute_free_sql(project_id, connector_id, sql)                             LLM 自由 SELECT 入口
"""
import contextlib
import re
import time
from typing import Any, Iterator, Optional

from loguru import logger
from sqlalchemy import text, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.pool import NullPool

from core.registry import DatabaseConnectorItem, registry
from settings import agent_config, ssh_tunnel_config

_DANGEROUS_KEYWORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|GRANT|REVOKE|REPLACE|MERGE|CALL|LOAD|HANDLER|LOCK|UNLOCK|RENAME)\b",
    re.IGNORECASE,
)

_SELECT_LIKE_FULL_SCAN_RE = re.compile(r"\blike\s+'%[^']*%'", re.IGNORECASE)
_COUNT_ONLY_RE = re.compile(r"^\s*select\s+count\s*\(\s*\*\s*\)\s+from\s+", re.IGNORECASE)
_EXPLAIN_PREFIX_RE = re.compile(r"^\s*explain(?:\s+format\s*=\s*json)?\s+", re.IGNORECASE)
_LIMIT_RE = re.compile(r"\blimit\b", re.IGNORECASE)


# ============================================================
# SQL 安全校验
# ============================================================

def _is_single_statement(sql: str) -> bool:
    """判断是否为单条 SQL（忽略字符串字面量中的分号）。"""
    in_single = in_double = False
    prev = ""
    for ch in sql:
        if ch == "'" and not in_double and prev != "\\":
            in_single = not in_single
        elif ch == '"' and not in_single and prev != "\\":
            in_double = not in_double
        elif ch == ";" and not in_single and not in_double:
            return False
        prev = ch
    return True


def validate_sql_safety(sql: str) -> None:
    """
    校验 SQL 安全性。

    Raises:
        ValueError: 不符合安全要求（SELECT-only / 关键字 / 长度 / 多语句）。
    """
    stripped = sql.strip().rstrip(";").strip()
    if not stripped:
        raise ValueError("SQL 为空")
    if len(stripped) > agent_config.max_sql_length:
        raise ValueError(
            f"SQL 长度超限（{len(stripped)} > {agent_config.max_sql_length}）"
        )
    if not stripped.upper().startswith(("SELECT", "WITH")):
        raise ValueError("只允许 SELECT 查询（允许 WITH ... SELECT）")
    if _DANGEROUS_KEYWORDS.search(stripped):
        raise ValueError("SQL 包含禁止的关键字")
    if not _is_single_statement(stripped):
        raise ValueError("只允许单条 SQL，禁止使用 ; 拼接多条语句")


# ============================================================
# 隧道参数解析
# ============================================================

def _needs_ssh_tunnel(database_connector: DatabaseConnectorItem) -> bool:
    """根据数据库连接器自带配置判断是否走 SSH 隧道。默认直连。"""
    return database_connector.ssh_tunnel is not None


def _resolve_tunnel_params(database_connector: DatabaseConnectorItem) -> dict:
    """合并数据库连接器级 SSH 设置与全局默认值，给 tunnel_context 用。"""
    spec = database_connector.ssh_tunnel
    return {
        "jump_host": (spec.jump_host if spec and spec.jump_host else ssh_tunnel_config.jump_host),
        "jump_port": (spec.jump_port if spec and spec.jump_port else ssh_tunnel_config.jump_port),
        "username": (spec.username if spec and spec.username else ssh_tunnel_config.username),
        "private_key": (spec.private_key if spec and spec.private_key else ssh_tunnel_config.private_key),
    }


# ============================================================
# 引擎上下文：统一直连 / 隧道两种连接
# ============================================================

@contextlib.contextmanager
def engine_context(project_id: str, connector_id: str) -> Iterator[tuple[Engine, DatabaseConnectorItem]]:
    """
    统一的引擎上下文管理器。

    - 直连数据库连接器：复用 registry 已缓存的 engine
    - 需要隧道的数据库连接器：临时打开隧道 + 临时 engine（NullPool），退出时自动关闭

    Yields:
        (engine, database_connector)

    Raises:
        ValueError: 数据库连接器未注册或对应 engine 未初始化。
    """
    database_connector = registry._database_connectors.get(project_id, {}).get(connector_id)
    if not database_connector:
        raise ValueError(f"数据库连接器 '{connector_id}' 在项目 '{project_id}' 中未注册")

    if not _needs_ssh_tunnel(database_connector):
        engine = registry.get_engine(project_id, connector_id)
        if not engine:
            raise ValueError(f"数据库连接器 '{connector_id}' 的引擎未初始化")
        yield engine, database_connector
        return

    # 走 SSH 隧道
    from core.ssh_tunnel import tunnel_context

    tunnel_params = _resolve_tunnel_params(database_connector)
    logger.info(
        "使用 SSH 隧道连接数据库: {}:{} 经由 {}:{} (项目: {}, 数据库连接器: {})",
        database_connector.host, database_connector.port,
        tunnel_params["jump_host"], tunnel_params["jump_port"],
        project_id, connector_id,
    )
    with tunnel_context(
        database_connector.host, database_connector.port,
        jump_host=tunnel_params["jump_host"],
        jump_port=tunnel_params["jump_port"],
        username=tunnel_params["username"],
        private_key=tunnel_params["private_key"],
    ) as local_port:
        url = (
            f"mysql+pymysql://{database_connector.username}:{database_connector.password}"
            f"@127.0.0.1:{local_port}/{database_connector.database}"
            f"?charset={database_connector.charset}"
        )
        engine = create_engine(url, poolclass=NullPool)
        try:
            yield engine, database_connector
        finally:
            engine.dispose()


# ============================================================
# 统一执行入口
# ============================================================

def _has_limit(sql: str) -> bool:
    """粗粒度判断 SQL 是否含 LIMIT。子查询内的 LIMIT 也会命中，偏保守不注入更安全。"""
    return bool(_LIMIT_RE.search(sql))


def _summarize_sql(sql: str, max_len: int = 200) -> str:
    """压缩 SQL 用于审计日志。"""
    single_line = re.sub(r"\s+", " ", sql).strip()
    if len(single_line) <= max_len:
        return single_line
    return single_line[:max_len] + "..."


def _strip_explain_prefix(sql: str) -> str:
    return _EXPLAIN_PREFIX_RE.sub("", sql, count=1).strip()


def _set_mysql_timeout(conn: Any, timeout_sec: int) -> None:
    """给 MySQL 连接设置 SELECT 级执行上限。"""
    timeout_ms = max(int(timeout_sec * 1000), 1)
    try:
        conn.execute(text(f"SET SESSION MAX_EXECUTION_TIME={timeout_ms}"))
    except Exception as e:  # noqa: BLE001
        logger.debug("设置 MySQL MAX_EXECUTION_TIME 失败，继续执行: {}", e)


def _is_timeout_error(exc: Exception) -> bool:
    orig = getattr(exc, "orig", exc)
    args = getattr(orig, "args", ())
    if not args:
        return False
    code = args[0]
    return code in {1317, 3024, 1969}


def _format_explain_rows(columns: list[str], rows: list[tuple[Any, ...]]) -> str:
    if not rows:
        return "EXPLAIN 无结果"
    lines: list[str] = []
    for row in rows:
        row_dict = dict(zip(columns, row))
        lines.append(" | ".join(f"{col}: {_format_value(val)}" for col, val in row_dict.items()))
    return "\n".join(lines)


def _estimate_rows_from_explain(columns: list[str], rows: list[tuple[Any, ...]]) -> tuple[int, bool, str]:
    """从 EXPLAIN 粗估扫描规模，返回 (rows_estimate, full_scan, access_summary)。"""
    if not rows:
        return 0, False, "no_rows"

    idx_rows = None
    idx_type = None
    idx_extra = None
    for i, col in enumerate(columns):
        if col == "rows":
            idx_rows = i
        elif col == "type":
            idx_type = i
        elif col == "Extra":
            idx_extra = i

    rows_estimate = 0
    full_scan = False
    access_items: list[str] = []
    for row in rows:
        row_est = 0
        if idx_rows is not None:
            try:
                row_est = int(float(row[idx_rows] or 0))
            except (TypeError, ValueError):
                row_est = 0
        rows_estimate += max(row_est, 0)

        row_type = str(row[idx_type]) if idx_type is not None and row[idx_type] is not None else ""
        extra = str(row[idx_extra]) if idx_extra is not None and row[idx_extra] is not None else ""
        access_items.append("/".join([item for item in [row_type, extra] if item]))
        if row_type.upper() == "ALL":
            full_scan = True

    return rows_estimate, full_scan, "; ".join(access_items[:3])


def _preflight_sql_risk(engine: Engine, sql: str, params: dict[str, Any]) -> str | None:
    """
    执行前风险预估。

    只针对自由 SQL 做保守拦截：如果 EXPLAIN 显示大范围全表扫描，则直接拒绝。
    """
    sql_lower = sql.lower()
    if _SELECT_LIKE_FULL_SCAN_RE.search(sql) and " where " not in sql_lower:
        return (
            "当前 SQL 使用了前置通配符 LIKE，但没有足够过滤条件。请先用 describe_table / EXPLAIN 收敛索引，"
            "再用更窄的时间范围、枚举值或前缀匹配。"
        )
    try:
        with engine.connect() as conn:
            result = conn.execute(text(f"EXPLAIN {sql}"), params)
            columns = list(result.keys())
            rows = result.fetchmany(64)
    except Exception as e:  # noqa: BLE001
        logger.debug("EXPLAIN 预检失败，跳过阻断: {}", e)
        return None

    rows_estimate, full_scan, access_summary = _estimate_rows_from_explain(columns, rows)
    if rows_estimate >= agent_config.sql_explain_max_estimated_rows and full_scan:
        return (
            f"SQL 预估风险过高：EXPLAIN 估计扫描约 {rows_estimate} 行，"
            f"访问方式 {access_summary or '未知'}。请先用 describe_table / sample_rows / list_tables 收敛字段、"
            f"改成更窄的 WHERE 条件或先建立项目级知识，再执行该查询。"
        )
    return None


def run_select(
    project_id: str,
    connector_id: str,
    sql: str,
    params: Optional[dict[str, Any]] = None,
    *,
    source: str = "free_sql",
    auto_limit: bool = False,
    preflight: bool = False,
) -> str:
    """
    执行一条 SELECT 查询，返回格式化文本。

    Args:
        project_id: 项目 ID。
        connector_id: 数据库连接器 ID（必须已注册）。
        sql: 完整 SQL（SELECT 或 WITH ... SELECT）。
        params: 命名参数字典（对应 :name 占位符）。
        source: 审计日志来源标识（如 "free_sql"）。
        auto_limit: 为 True 时若 SQL 未包含 LIMIT，自动追加 LIMIT :__auto_limit。

    Returns:
        供 LLM 阅读的结果字符串；错误时返回错误说明（不抛异常）。
    """
    sql_clean = sql.strip().rstrip(";").strip()
    try:
        validate_sql_safety(sql_clean)
    except ValueError as e:
        return f"SQL 安全校验失败：{e}"

    params = dict(params or {})
    effective_sql = sql_clean
    if auto_limit and not _has_limit(sql_clean):
        effective_sql = f"{sql_clean}\nLIMIT :__auto_limit"
        params["__auto_limit"] = agent_config.query_result_limit

    t0 = time.perf_counter()
    try:
        with engine_context(project_id, connector_id) as (engine, _):
            if preflight:
                risk_msg = _preflight_sql_risk(engine, effective_sql, params)
                if risk_msg:
                    return f"SQL 预估拦截：{risk_msg}"
            output, row_count, truncated = _execute_and_format(
                engine,
                effective_sql,
                params,
                timeout_sec=agent_config.sql_timeout_sec,
            )
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        logger.info(
            "SQL审计 project={} ds={} source={} elapsed={}ms rows={} truncated={} sql={}",
            project_id, connector_id, source, elapsed_ms,
            row_count, truncated, _summarize_sql(sql_clean),
        )
        return output
    except ValueError as e:
        return f"错误：{e}"
    except Exception as e:  # noqa: BLE001
        if _is_timeout_error(e):
            logger.warning(
                "SQL执行超时 project={} ds={} source={} timeout_sec={} sql={}",
                project_id, connector_id, source, agent_config.sql_timeout_sec, _summarize_sql(sql_clean),
            )
            return (
                f"查询执行超时：已超过 {agent_config.sql_timeout_sec} 秒，数据库已中止该查询。"
                "请先收窄时间范围、减少 LIKE '%...%' 这种大范围匹配，"
                "或先用 describe_table / sample_rows / EXPLAIN 收敛索引与条件。"
            )
        logger.error(
            "SQL执行失败 project={} ds={} source={} error={} sql={}",
            project_id, connector_id, source, e, _summarize_sql(sql_clean),
        )
        return f"查询执行失败：{e}"


def explain_sql(project_id: str, connector_id: str, sql: str, params: Optional[dict[str, Any]] = None) -> str:
    """执行 EXPLAIN，不访问真实结果集。"""
    sql_clean = _strip_explain_prefix(sql.strip().rstrip(";").strip())
    try:
        validate_sql_safety(sql_clean)
    except ValueError as e:
        return f"SQL 安全校验失败：{e}"

    params = dict(params or {})
    try:
        with engine_context(project_id, connector_id) as (engine, _):
            with engine.connect() as conn:
                result = conn.execute(text(f"EXPLAIN {sql_clean}"), params)
                columns = list(result.keys())
                rows = result.fetchmany(64)
        return _format_explain_rows(columns, rows)
    except Exception as e:  # noqa: BLE001
        logger.error(
            "EXPLAIN 执行失败 project={} ds={} error={} sql={}",
            project_id, connector_id, e, _summarize_sql(sql_clean),
        )
        return f"EXPLAIN 执行失败：{e}"


def execute_free_sql(project_id: str, connector_id: str, sql: str) -> str:
    """
    Free-SQL 入口：LLM 自由书写 SELECT，走更严格的保护（自动注入 LIMIT）。
    """
    return run_select(
        project_id,
        connector_id,
        sql,
        params=None,
        source="free_sql",
        auto_limit=True,
        preflight=True,
    )


def execute_probe_sql(project_id: str, connector_id: str, sql: str) -> str:
    """
    SQL 探针入口：用于摸索表规模或数据形态，比回答业务问题更保守。
    """
    sql_clean = sql.strip().rstrip(";").strip()
    if _COUNT_ONLY_RE.search(sql_clean) and " where " not in sql_clean.lower():
        return (
            "SQL 探针被拦截：不要用无过滤条件的 COUNT(*) 试探表大小。"
            "请先用 describe_table / sample_rows / explain_sql 收敛索引、时间范围或业务过滤条件。"
            "如果用户明确要求精确总数，请使用 execute_sql 而不是 probe_sql。"
        )
    return run_select(
        project_id,
        connector_id,
        sql,
        params=None,
        source="probe_sql",
        auto_limit=True,
        preflight=True,
    )


# ============================================================
# SQL 执行与结果格式化
# ============================================================

def _execute_and_format(
    engine: Engine,
    sql: str,
    params: dict[str, Any],
    *,
    timeout_sec: int,
) -> tuple[str, int, bool]:
    """
    执行 SQL 并格式化为人类可读文本。

    Returns:
        (formatted_text, row_count, truncated)
    """
    with engine.connect() as conn:
        _set_mysql_timeout(conn, timeout_sec)
        result = conn.execute(text(sql), params)
        columns = list(result.keys())
        rows = result.fetchmany(agent_config.query_result_limit + 1)

        if not rows:
            return "查询无结果", 0, False

        truncated = len(rows) > agent_config.query_result_limit
        if truncated:
            rows = rows[: agent_config.query_result_limit]

        lines = []
        for row in rows:
            row_dict = dict(zip(columns, row))
            row_str = " | ".join(
                f"{col}: {_format_value(val)}" for col, val in row_dict.items()
            )
            lines.append(row_str)

        output = "\n".join(lines)
        if truncated:
            output += (
                f"\n\n（结果已截断，仅显示前 {agent_config.query_result_limit} 条，"
                f"实际可能更多）"
            )
        return output, len(rows), truncated


def _format_value(val: Any) -> str:
    """将查询结果值格式化为可读字符串。"""
    if val is None:
        return "NULL"
    return str(val)
