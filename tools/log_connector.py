"""Log Connector tools for Aliyun SLS."""
from __future__ import annotations

import json
import time
from typing import Any

from core.registry import registry
from settings import aliyun_sls_config


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def list_log_connectors(project_id: str) -> str:
    items = registry.get_log_connectors(project_id, only_enabled=True)
    if not items:
        return f"项目 '{project_id}' 尚未注册 Log Connector"
    return _json([
        {
            "id": item.id,
            "display_name": item.display_name,
            "sls_project": item.sls_project,
            "logstore": item.logstore,
            "description": item.description,
            "enabled": item.enabled,
        }
        for item in items
    ])


def query_logs(project_id: str, connector_id: str, query: str, minutes: int = 30, limit: int = 20) -> str:
    try:
        from aliyun.log import GetLogsRequest, LogClient
    except ImportError:
        return "SLS 工具依赖缺失：请安装 aliyun-log-python-sdk"

    item = registry.get_log_connector(project_id, connector_id)
    if not item:
        return f"Log Connector '{connector_id}' 在项目 '{project_id}' 中未注册"
    if not item.enabled:
        return f"Log Connector '{connector_id}' 未启用"
    if not aliyun_sls_config.endpoint or not aliyun_sls_config.access_key_id or not aliyun_sls_config.access_key_secret:
        return "SLS 全局 endpoint/access_key 未配置"

    now = int(time.time())
    req = GetLogsRequest(
        project=item.sls_project,
        logstore=item.logstore,
        fromTime=now - max(1, minutes) * 60,
        toTime=now,
        query=query or "*",
        line=min(max(limit, 1), 100),
        offset=0,
        reverse=True,
    )
    try:
        client = LogClient(
            aliyun_sls_config.endpoint,
            accessKeyId=aliyun_sls_config.access_key_id,
            accessKey=aliyun_sls_config.access_key_secret,
            securityToken=aliyun_sls_config.security_token or None,
        )
        resp = client.get_logs(req)
        logs = []
        for log in resp.get_logs():
            row = {"__time__": log.get_time(), "__source__": log.get_source()}
            row.update(log.get_contents() or {})
            logs.append(row)
        return _json({
            "connector_id": connector_id,
            "sls_project": item.sls_project,
            "logstore": item.logstore,
            "query": query,
            "count": resp.get_count(),
            "is_completed": resp.is_completed(),
            "logs": logs,
        })
    except Exception as e:  # noqa: BLE001
        return _json({"connector_id": connector_id, "query": query, "error": str(e)})
