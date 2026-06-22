"""Watchdog 调度引擎。

职责：
- APScheduler 定时探针调度
- 探针执行（HTTP / SQL Metric）
- 编排流程：探针 → AI 分析 → 可选 CodingTask → 原子通知
- 冷却/去重保护
"""
from __future__ import annotations

import asyncio
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger

from core.database import SessionLocal
from core.models import WatchdogEventModel
from core.registry import WatchdogItem, registry
from core.watchdog_agent import WatchdogConclusion, run_watchdog_agent
from core.watchdog_notifier import send_dingtalk_notification
from settings import watchdog_config


# ────────────────────────────────────────────────────────────
# 探针执行
# ────────────────────────────────────────────────────────────

async def _execute_http_probe(spec: dict[str, Any]) -> dict[str, Any]:
    """执行 HTTP 探针。

    spec 字段：url, method(默认GET), headers, body, timeout_sec, expected_status/expect_status
    """
    url = spec["url"]
    method = spec.get("method", "GET").upper()
    headers = spec.get("headers") or {}
    body = spec.get("body")
    timeout = spec.get("timeout_sec", 10)
    expect_status = spec.get("expected_status", spec.get("expect_status", 200))

    start = time.time()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.request(method, url, headers=headers, content=body)
        elapsed_ms = (time.time() - start) * 1000
        is_healthy = resp.status_code == expect_status
        return {
            "type": "http",
            "url": url,
            "status_code": resp.status_code,
            "elapsed_ms": round(elapsed_ms, 2),
            "is_healthy": is_healthy,
            "body_preview": resp.text[:500] if not is_healthy else "",
        }
    except Exception as e:
        elapsed_ms = (time.time() - start) * 1000
        return {
            "type": "http",
            "url": url,
            "status_code": 0,
            "elapsed_ms": round(elapsed_ms, 2),
            "is_healthy": False,
            "error": str(e),
        }


async def _execute_sql_metric_probe(spec: dict[str, Any]) -> dict[str, Any]:
    """执行 SQL Metric 探针。

    spec 字段：project_id, connector_id, sql, threshold, operator
    通过现有数据库连接器执行只读 SQL 获取指标。
    """
    from tools.sql_executor import execute_free_sql

    project_id = spec["project_id"]
    connector_id = spec["connector_id"]
    sql = spec["sql"]
    threshold = spec["threshold"]
    operator = spec["operator"]

    start = time.time()
    try:
        result = execute_free_sql(project_id=project_id, connector_id=connector_id, sql=sql)
        elapsed_ms = (time.time() - start) * 1000
        metric_value = _extract_first_number(result)
        threshold_matched = (
            _compare_metric(metric_value, operator, threshold)
            if metric_value is not None
            else False
        )
        success = metric_value is not None
        return {
            "type": "sql_metric",
            "connector_id": connector_id,
            "sql": sql,
            "metric_value": metric_value,
            "threshold": threshold,
            "operator": operator,
            "threshold_matched": threshold_matched,
            "is_healthy": success and not threshold_matched,
            "is_anomaly": (not success) or threshold_matched,
            "elapsed_ms": round(elapsed_ms, 2),
            "result": result,
            "success": success,
            "evaluation_error": "" if success else "SQL 结果中未提取到可计算的数字指标",
        }
    except Exception as e:
        elapsed_ms = (time.time() - start) * 1000
        return {
            "type": "sql_metric",
            "connector_id": connector_id,
            "sql": sql,
            "elapsed_ms": round(elapsed_ms, 2),
            "error": str(e),
            "success": False,
            "is_healthy": False,
            "is_anomaly": True,
        }


async def _execute_http_json_metric_probe(spec: dict[str, Any]) -> dict[str, Any]:
    """执行 HTTP JSON 指标探针，提取 parser 摘要和异常 parser 列表。"""
    url = spec["url"]
    urls = [url, *(spec.get("fallback_urls") or [])]
    method = spec.get("method", "GET").upper()
    headers = spec.get("headers") or {}
    timeout = spec.get("timeout_sec", 10)
    expect_status = spec.get("expected_status", spec.get("expect_status", 200))

    start = time.time()
    attempts: list[dict[str, Any]] = []
    try:
        resp: httpx.Response | None = None
        payload: Any = None
        async with httpx.AsyncClient(timeout=timeout) as client:
            for candidate_url in urls:
                attempt_start = time.time()
                try:
                    candidate_resp = await client.request(method, candidate_url, headers=headers)
                    attempt_elapsed_ms = (time.time() - attempt_start) * 1000
                    attempt = {
                        "url": candidate_url,
                        "status_code": candidate_resp.status_code,
                        "elapsed_ms": round(attempt_elapsed_ms, 2),
                    }
                    if candidate_resp.status_code != expect_status:
                        attempt["error"] = f"HTTP status {candidate_resp.status_code} != {expect_status}"
                        attempts.append(attempt)
                        continue
                    try:
                        payload = candidate_resp.json()
                    except ValueError as e:
                        attempt["error"] = f"响应不是合法 JSON: {e}"
                        attempts.append(attempt)
                        continue
                    attempts.append(attempt)
                    resp = candidate_resp
                    url = candidate_url
                    break
                except Exception as e:
                    attempts.append({
                        "url": candidate_url,
                        "status_code": 0,
                        "elapsed_ms": round((time.time() - attempt_start) * 1000, 2),
                        "error": str(e),
                    })
        elapsed_ms = (time.time() - start) * 1000
        if resp is None:
            return {
                "type": "http_json_metric",
                "url": spec["url"],
                "attempts": attempts,
                "status_code": attempts[-1]["status_code"] if attempts else 0,
                "elapsed_ms": round(elapsed_ms, 2),
                "success": False,
                "is_healthy": False,
                "is_anomaly": True,
                "error": "所有 HTTP JSON 指标 URL 均请求失败或响应不可用",
            }
        status_ok = resp.status_code == expect_status

        summary = _extract_json_summary(payload, spec.get("summary_path") or "")
        anomalous_parsers = _extract_anomalous_parsers(payload, spec.get("anomalous_parsers_path") or "")
        parser_statistics = _extract_parser_statistics(payload, spec.get("parser_statistics_path") or "")
        max_anomalous_parsers = int(spec.get("max_anomalous_parsers") or 100)
        parser_stat_anomalies = _detect_parser_stat_anomalies(
            parser_statistics,
            failure_rate_threshold=spec.get("failure_rate_threshold"),
            failed_attempts_threshold=spec.get("failed_attempts_threshold"),
            min_total_attempts=int(spec.get("min_total_attempts") or 0),
            max_items=max_anomalous_parsers,
        )
        if parser_stat_anomalies:
            anomalous_parsers = [*anomalous_parsers, *parser_stat_anomalies]
        metric_value = _extract_json_metric(payload, spec.get("metric_path") or "")
        threshold = spec.get("threshold")
        operator = spec.get("operator")
        threshold_matched = (
            _compare_metric(metric_value, operator, threshold)
            if metric_value is not None and operator and threshold is not None
            else False
        )
        is_anomaly = (not status_ok) or bool(anomalous_parsers) or threshold_matched
        return {
            "type": "http_json_metric",
            "url": url,
            "attempts": attempts,
            "status_code": resp.status_code,
            "elapsed_ms": round(elapsed_ms, 2),
            "success": status_ok,
            "is_healthy": not is_anomaly,
            "is_anomaly": is_anomaly,
            "summary": summary,
            "anomalous_parsers": anomalous_parsers[:max_anomalous_parsers],
            "anomalous_parser_count": len(anomalous_parsers),
            "parser_statistics_count": len(parser_statistics),
            "metric_value": metric_value,
            "threshold": threshold,
            "operator": operator,
            "threshold_matched": threshold_matched,
            "json_preview": _compact_json_preview(payload),
        }
    except Exception as e:
        elapsed_ms = (time.time() - start) * 1000
        return {
            "type": "http_json_metric",
            "url": url,
            "status_code": 0,
            "elapsed_ms": round(elapsed_ms, 2),
            "success": False,
            "is_healthy": False,
            "is_anomaly": True,
            "error": str(e),
        }


def _compare_metric(value: float | int, operator: str, threshold: float | int) -> bool:
    """返回 metric 是否命中异常阈值。"""
    ops = {
        "gt": value > threshold,
        "lt": value < threshold,
        "gte": value >= threshold,
        "lte": value <= threshold,
        "eq": value == threshold,
        "neq": value != threshold,
    }
    return bool(ops.get(operator, False))


def _extract_first_number(value: Any) -> float | None:
    """从 SQL 文本结果或 JSON 值中提取第一个数字指标。"""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value)
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    return float(match.group(0)) if match else None


def _get_json_path(payload: Any, path: str) -> Any:
    if not path:
        return None
    current = payload
    for part in path.replace("[", ".").replace("]", "").split("."):
        if part == "":
            continue
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            current = current[int(part)] if int(part) < len(current) else None
        else:
            return None
        if current is None:
            return None
    return current


def _first_json_path(payload: Any, paths: list[str]) -> Any:
    for path in paths:
        value = _get_json_path(payload, path)
        if value not in (None, "", []):
            return value
    return None


def _extract_json_summary(payload: Any, configured_path: str) -> Any:
    paths = [
        configured_path,
        "summary",
        "working_summary",
        "parser_working_summary",
        "data.summary",
        "data.working_summary",
        "data.parser_working_summary",
        "result.summary",
        "cache.summary",
    ]
    return _first_json_path(payload, [p for p in paths if p]) or {}


def _extract_anomalous_parsers(payload: Any, configured_path: str) -> list[Any]:
    paths = [
        configured_path,
        "anomalous_parsers",
        "anomaly_parsers",
        "abnormal_parsers",
        "failed_parsers",
        "data.anomalous_parsers",
        "data.anomaly_parsers",
        "data.abnormal_parsers",
        "data.failed_parsers",
        "result.anomalous_parsers",
        "summary.anomalous_parsers",
        "cache.anomalous_parsers",
    ]
    value = _first_json_path(payload, [p for p in paths if p])
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return list(value.values())
    return []


def _extract_parser_statistics(payload: Any, configured_path: str) -> list[dict[str, Any]]:
    paths = [
        configured_path,
        "parser_statistics",
        "data.parser_statistics",
        "result.parser_statistics",
        "cache.parser_statistics",
    ]
    value = _first_json_path(payload, [p for p in paths if p])
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _detect_parser_stat_anomalies(
    parser_statistics: list[dict[str, Any]],
    *,
    failure_rate_threshold: Any,
    failed_attempts_threshold: Any,
    min_total_attempts: int,
    max_items: int,
) -> list[dict[str, Any]]:
    """从 crawler-console ParserStatistic 列表中筛出异常 parser。"""
    if failure_rate_threshold is None and failed_attempts_threshold is None:
        return []
    try:
        failure_threshold = float(failure_rate_threshold) if failure_rate_threshold is not None else None
    except (TypeError, ValueError):
        failure_threshold = None
    try:
        failed_threshold = int(failed_attempts_threshold) if failed_attempts_threshold is not None else None
    except (TypeError, ValueError):
        failed_threshold = None

    anomalies: list[dict[str, Any]] = []
    for item in parser_statistics:
        parser_name = str(item.get("parser_name") or "").strip()
        total_attempts = _int_or_zero(item.get("total_attempts"))
        failed_attempts = _int_or_zero(item.get("failed_attempts"))
        failure_rate = _float_or_none(item.get("failure_rate"))
        if total_attempts < min_total_attempts:
            continue
        reasons: list[str] = []
        if failure_threshold is not None and failure_rate is not None and failure_rate >= failure_threshold:
            reasons.append(f"failure_rate>={failure_threshold}")
        if failed_threshold is not None and failed_attempts >= failed_threshold:
            reasons.append(f"failed_attempts>={failed_threshold}")
        if not reasons:
            continue
        anomalies.append({
            "parser_name": parser_name,
            "total_attempts": total_attempts,
            "successful_attempts": _int_or_zero(item.get("successful_attempts")),
            "failed_attempts": failed_attempts,
            "failure_rate": failure_rate,
            "average_parse_time": item.get("average_parse_time"),
            "per99_avg_parse_time": item.get("per99_avg_parse_time"),
            "reasons": reasons,
        })
    anomalies.sort(key=lambda row: (row.get("failure_rate") or 0, row.get("failed_attempts") or 0), reverse=True)
    return anomalies[:max_items]


def _int_or_zero(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _extract_json_metric(payload: Any, configured_path: str) -> float | None:
    if not configured_path:
        return None
    return _extract_first_number(_get_json_path(payload, configured_path))


def _compact_json_preview(payload: Any) -> Any:
    if isinstance(payload, dict):
        return {k: payload[k] for k in list(payload)[:20]}
    if isinstance(payload, list):
        return payload[:20]
    return payload


async def execute_probe(watchdog: WatchdogItem) -> dict[str, Any]:
    """根据探针类型分发执行。"""
    probe = watchdog.probe
    if probe.http:
        return await _execute_http_probe(probe.http.model_dump())
    elif probe.sql_metric:
        spec = probe.sql_metric.model_dump()
        spec["project_id"] = watchdog.project_id
        return await _execute_sql_metric_probe(spec)
    elif probe.http_json_metric:
        return await _execute_http_json_metric_probe(probe.http_json_metric.model_dump())
    else:
        return {"type": "unknown", "error": "无法识别的探针类型", "success": False}


# ────────────────────────────────────────────────────────────
# 冷却检查
# ────────────────────────────────────────────────────────────

def _is_in_cooldown(watchdog: WatchdogItem) -> bool:
    """检查 watchdog 是否在冷却期内（距上次异常通知 < cooldown_minutes）。"""
    db = SessionLocal()
    try:
        last_event = (
            db.query(WatchdogEventModel)
            .filter(
                WatchdogEventModel.watchdog_id == watchdog.id,
                WatchdogEventModel.project_id == watchdog.project_id,
                WatchdogEventModel.notification_sent == 1,
            )
            .order_by(WatchdogEventModel.started_at.desc())
            .first()
        )
        if not last_event or not last_event.completed_at:
            return False
        elapsed_min = (datetime.now(timezone.utc) - last_event.completed_at.replace(tzinfo=timezone.utc)).total_seconds() / 60
        return elapsed_min < watchdog.cooldown_minutes
    finally:
        db.close()


# ────────────────────────────────────────────────────────────
# CodingTask 集成
# ────────────────────────────────────────────────────────────

def _create_coding_task(watchdog: WatchdogItem, conclusion: WatchdogConclusion) -> dict[str, Any]:
    """创建 CodingTask 并等待 plan 生成完成。返回 task 状态摘要。"""
    from core.coding_service import get_task, start_coding_task

    requirement = (
        f"[Watchdog 自动生成] {watchdog.name}\n\n"
        f"## 异常分析结论\n{conclusion.conclusion}\n\n"
        f"## 佐证\n" + "\n".join(f"- {e}" for e in conclusion.evidence)
    )

    task_id = start_coding_task(
        project_id=watchdog.project_id,
        requirement=requirement,
        repo_connector_id=watchdog.coding_repo_connector_id,
        created_by="watchdog",
    )
    logger.info("Watchdog {} 已创建 CodingTask: {}", watchdog.id, task_id)

    # 等待 plan 生成完成
    deadline = time.time() + watchdog_config.plan_wait_timeout_sec
    while time.time() < deadline:
        task_data = get_task(task_id)
        if task_data:
            status = task_data.get("status", "")
            stage = task_data.get("stage", "")
            message = task_data.get("message", "")
            # plan 就绪的状态：当前实现会进入 waiting_plan_review 等待人工审核。
            if status in ("waiting_plan_review", "waiting_clarification", "running", "waiting_code_review", "completed"):
                return {"task_id": task_id, "status": status, "stage": stage, "message": message}
            if stage in ("plan_ready", "plan_review", "waiting_plan_review"):
                return {"task_id": task_id, "status": status or stage, "stage": stage, "message": message}
            if status in ("failed", "cancelled"):
                logger.warning("Watchdog {} CodingTask {} 失败: status={}", watchdog.id, task_id, status)
                return {"task_id": task_id, "status": status, "stage": stage, "message": message}
        time.sleep(5)

    logger.warning("Watchdog {} CodingTask {} plan 生成超时", watchdog.id, task_id)
    task_data = get_task(task_id) or {}
    return {
        "task_id": task_id,
        "status": "timeout",
        "stage": str(task_data.get("stage") or ""),
        "message": str(task_data.get("message") or "等待 Plan 生成超时"),
    }


# ────────────────────────────────────────────────────────────
# 单次 Watchdog 执行编排
# ────────────────────────────────────────────────────────────

async def _run_single_watchdog(watchdog: WatchdogItem) -> None:
    """单个 Watchdog 的完整执行流程。"""
    # 冷却检查
    if _is_in_cooldown(watchdog):
        logger.debug("Watchdog {} 在冷却期内，跳过", watchdog.id)
        return

    started_at = datetime.now(timezone.utc)

    # 1. 记录事件开始
    db = SessionLocal()
    try:
        event = WatchdogEventModel(
            watchdog_id=watchdog.id,
            project_id=watchdog.project_id,
            status="started",
            started_at=started_at,
        )
        db.add(event)
        db.commit()
        db.refresh(event)
        event_id = event.id
    finally:
        db.close()

    probe_result: dict[str, Any] = {}
    conclusion = WatchdogConclusion()
    coding_task_id = ""
    coding_task_info: dict[str, Any] = {}
    notification_sent = False
    notification_error = ""

    try:
        # 2. 执行探针
        probe_result = await execute_probe(watchdog)
        logger.info("Watchdog {} 探针完成: {}", watchdog.id, probe_result.get("type"))

        # 3. AI 分析
        conclusion = await run_watchdog_agent(watchdog, probe_result)
        logger.info(
            "Watchdog {} AI 分析完成: is_anomaly={}, severity={}, action={}",
            watchdog.id, conclusion.is_anomaly, conclusion.severity, conclusion.action_type,
        )

        # 4. 严重程度过滤
        if not conclusion.is_anomaly:
            logger.debug("Watchdog {} 无异常，结束", watchdog.id)
            _update_event(event_id, "completed", probe_result, conclusion)
            return

        # 5. 可选: 生成 CodingTask
        if watchdog.auto_coding_plan and conclusion.action_type == "coding_plan":
            coding_task_info = _create_coding_task(watchdog, conclusion)
            coding_task_id = str(coding_task_info.get("task_id") or "")

        # 6. 通知（原子性：分析+plan都就绪后才发）
        if conclusion.severity in watchdog.severity_filter:
            try:
                await send_dingtalk_notification(
                    watchdog.notification,
                    watchdog_name=watchdog.name,
                    project_id=watchdog.project_id,
                    severity=conclusion.severity,
                    conclusion=conclusion.conclusion,
                    coding_task_id=coding_task_id,
                    coding_task_status=str(coding_task_info.get("status") or ""),
                    coding_task_stage=str(coding_task_info.get("stage") or ""),
                    coding_task_message=str(coding_task_info.get("message") or ""),
                )
                notification_sent = True
            except Exception as e:
                notification_error = str(e)
                logger.error("Watchdog {} 通知发送失败: {}", watchdog.id, e)

    except Exception as e:
        logger.exception("Watchdog {} 执行异常: {}", watchdog.id, e)
        conclusion = WatchdogConclusion(
            is_anomaly=True,
            severity="warning",
            conclusion=f"Watchdog 执行过程异常: {e}",
            action_type="notify",
        )

    # 7. 更新事件记录
    completed_at = datetime.now(timezone.utc)
    duration_ms = (completed_at - started_at).total_seconds() * 1000
    db = SessionLocal()
    try:
        row = db.get(WatchdogEventModel, event_id)
        if row:
            row.status = "completed"
            row.probe_result = probe_result
            row.is_anomaly = int(conclusion.is_anomaly)
            row.severity = conclusion.severity
            row.conclusion = conclusion.conclusion
            row.evidence = conclusion.evidence
            row.action_type = conclusion.action_type
            row.coding_task_id = coding_task_id
            if coding_task_info:
                row.probe_result = {**probe_result, "coding_task": coding_task_info}
            row.notification_sent = int(notification_sent)
            row.notification_error = notification_error
            row.duration_ms = duration_ms
            row.completed_at = completed_at
            db.commit()
    finally:
        db.close()


def _update_event(
    event_id: int,
    status: str,
    probe_result: dict,
    conclusion: WatchdogConclusion,
) -> None:
    """快速更新无异常场景的事件记录。"""
    db = SessionLocal()
    try:
        row = db.get(WatchdogEventModel, event_id)
        if row:
            row.status = status
            row.probe_result = probe_result
            row.is_anomaly = int(conclusion.is_anomaly)
            row.severity = conclusion.severity
            row.conclusion = conclusion.conclusion
            row.completed_at = datetime.now(timezone.utc)
            row.duration_ms = (row.completed_at - row.started_at).total_seconds() * 1000 if row.started_at else 0
            db.commit()
    finally:
        db.close()


# ────────────────────────────────────────────────────────────
# 调度触发入口（在 daemon thread 中运行 asyncio）
# ────────────────────────────────────────────────────────────

def _trigger_watchdog(watchdog_id: str, project_id: str) -> None:
    """APScheduler job 入口：在新线程中启动 asyncio 执行。"""
    watchdog = registry.get_watchdog(project_id, watchdog_id)
    if not watchdog or watchdog.status != "enabled":
        return
    try:
        asyncio.run(_run_single_watchdog(watchdog))
    except Exception:
        logger.exception("Watchdog {}/{} 触发执行失败", project_id, watchdog_id)


# ────────────────────────────────────────────────────────────
# WatchdogScheduler: 对外暴露的调度器管理类
# ────────────────────────────────────────────────────────────

class WatchdogScheduler:
    """Watchdog 定时调度器。管理所有已注册 Watchdog 的 cron job。"""

    def __init__(self) -> None:
        self._scheduler: BackgroundScheduler | None = None
        self._lock = threading.Lock()
        self._started = False

    def start(self) -> None:
        """启动调度器并加载所有已注册的 Watchdog。

        coding 自维护逻辑已迁出至 CodingMaintenanceScheduler；watchdog 禁用即直接不启动。
        """
        if not watchdog_config.enabled:
            logger.info("Watchdog 调度器已禁用 (watchdog.enabled=false)")
            return

        with self._lock:
            if self._started:
                return
            self._scheduler = BackgroundScheduler(
                job_defaults={
                    "max_instances": max(watchdog_config.max_concurrent_runs, 1),
                    "coalesce": True,
                    "misfire_grace_time": 60,
                },
            )
            self._scheduler.start()
            self._started = True

        # 注册所有已有的 Watchdog
        all_watchdogs = registry.get_all_enabled_watchdogs()
        for wd in all_watchdogs:
            self.add_job(wd)
        logger.info("Watchdog 调度器已启动，加载 {} 个 job", len(all_watchdogs))

    def shutdown(self) -> None:
        """停止调度器。"""
        with self._lock:
            if self._scheduler and self._started:
                self._scheduler.shutdown(wait=False)
                self._started = False
                logger.info("Watchdog 调度器已停止")

    def add_job(self, watchdog: WatchdogItem) -> None:
        """注册或更新一个 Watchdog 的 cron job。"""
        if not self._scheduler or not self._started:
            return
        job_id = f"watchdog_{watchdog.project_id}_{watchdog.id}"
        # 先移除已有 job（幂等）
        try:
            self._scheduler.remove_job(job_id)
        except Exception:
            pass
        trigger = CronTrigger.from_crontab(watchdog.schedule)
        self._scheduler.add_job(
            _trigger_watchdog,
            trigger=trigger,
            id=job_id,
            args=[watchdog.id, watchdog.project_id],
            replace_existing=True,
        )
        logger.debug("Watchdog job 已注册: {} (schedule={})", job_id, watchdog.schedule)

    def remove_job(self, project_id: str, watchdog_id: str) -> None:
        """移除一个 Watchdog job。"""
        if not self._scheduler or not self._started:
            return
        job_id = f"watchdog_{project_id}_{watchdog_id}"
        try:
            self._scheduler.remove_job(job_id)
            logger.debug("Watchdog job 已移除: {}", job_id)
        except Exception:
            pass

    @property
    def is_running(self) -> bool:
        return self._started


# 全局单例
watchdog_scheduler = WatchdogScheduler()
