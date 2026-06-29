"""External Connector tools.

这些工具只做只读探查，供 Agent 在诊断时获取 Redis / OSS / Queue /
Vector Store / HTTP Service 等非 SQL 证据。
"""
from __future__ import annotations

import json
import os
import time
from typing import Any
from urllib.parse import quote, urljoin, urlparse

import httpx
from loguru import logger

from core.registry import ExternalConnectorItem, registry
from settings import dingtalk_config


_DINGTALK_API_BASE = "https://api.dingtalk.com"
_DINGTALK_TOKEN_CACHE: dict[str, tuple[str, float]] = {}


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def _connector(project_id: str, connector_id: str, expected_type: str | None = None) -> ExternalConnectorItem:
    item = registry.get_external_connector(project_id, connector_id)
    if not item:
        raise ValueError(f"External Connector '{connector_id}' 在项目 '{project_id}' 中未注册")
    if expected_type and item.connector_type != expected_type:
        raise ValueError(f"External Connector '{connector_id}' 类型为 {item.connector_type}，不是 {expected_type}")
    if not item.enabled:
        raise ValueError(f"External Connector '{connector_id}' 未启用")
    return item


def _masked_config(item: ExternalConnectorItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "type": item.connector_type,
        "display_name": item.display_name,
        "description": item.description,
        "config": item.config,
        "secrets": sorted(item.secrets.keys()),
        "enabled": item.enabled,
    }


def _env_or_value(secrets: dict[str, Any], value_key: str, env_key: str, default: str = "") -> str:
    value = str(secrets.get(value_key) or "").strip()
    if value:
        return value
    env_name = str(secrets.get(env_key) or "").strip()
    if env_name:
        return os.environ.get(env_name, "").strip()
    return default.strip()


def _dingtalk_access_token(item: ExternalConnectorItem) -> str:
    sec = item.secrets
    app_key = _env_or_value(sec, "app_key", "app_key_env", dingtalk_config.app_key)
    app_secret = _env_or_value(sec, "app_secret", "app_secret_env", dingtalk_config.app_secret)
    if not app_key or not app_secret:
        raise ValueError("钉钉文档 Connector 缺少 app_key/app_secret，需配置 secrets 或 DINGTALK_APP_KEY/DINGTALK_APP_SECRET")

    cache_key = app_key
    cached = _DINGTALK_TOKEN_CACHE.get(cache_key)
    now = time.time()
    if cached and cached[1] > now:
        return cached[0]

    with httpx.Client(timeout=float(item.config.get("timeout", 10))) as client:
        resp = client.post(
            f"{_DINGTALK_API_BASE}/v1.0/oauth2/accessToken",
            json={"appKey": app_key, "appSecret": app_secret},
        )
        resp.raise_for_status()
        payload = resp.json()
    token = str(payload.get("accessToken") or "").strip()
    if not token:
        raise ValueError(f"获取钉钉 accessToken 失败：{payload}")
    expires_in = int(payload.get("expireIn") or payload.get("expiresIn") or 7200)
    _DINGTALK_TOKEN_CACHE[cache_key] = (token, now + max(60, expires_in - 300))
    return token


def _dingtalk_operator_id(item: ExternalConnectorItem) -> str:
    cfg, sec = item.config, item.secrets
    operator_id = (
        str(sec.get("operator_id") or sec.get("operator_union_id") or sec.get("default_operator_id") or "").strip()
        or str(cfg.get("operator_id") or cfg.get("operator_union_id") or cfg.get("default_operator_id") or "").strip()
    )
    if operator_id:
        return operator_id
    env_name = str(sec.get("operator_id_env") or cfg.get("operator_id_env") or "DINGTALK_DOC_OPERATOR_ID").strip()
    operator_id = os.environ.get(env_name, "").strip()
    if operator_id:
        return operator_id
    raise ValueError(
        "钉钉文档 Connector 缺少 operatorId（unionId）。"
        "当前最小实现需配置服务账号 unionId：secrets.operator_id 或 DINGTALK_DOC_OPERATOR_ID"
    )


def _assert_allowed_dingtalk_doc_url(item: ExternalConnectorItem, doc_url: str) -> None:
    parsed = urlparse(doc_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("doc_url 必须是完整 http(s) 链接")
    allowed = item.config.get("allowed_domains") or ["alidocs.dingtalk.com", "docs.dingtalk.com"]
    hostname = parsed.hostname or ""
    if not any(hostname == domain or hostname.endswith(f".{domain}") for domain in allowed):
        raise ValueError(f"doc_url 域名 {hostname!r} 不在 allowed_domains 中")


def _extract_dingtalk_node(payload: dict[str, Any]) -> dict[str, Any]:
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    node = result.get("node") if isinstance(result.get("node"), dict) else None
    if node is None:
        node = payload.get("node") if isinstance(payload.get("node"), dict) else None
    if node is None:
        node = result if result else payload
    return dict(node)


def _pick_doc_key(node: dict[str, Any]) -> str:
    for key in ("docKey", "documentId", "resourceId", "objId", "nodeId"):
        value = str(node.get(key) or "").strip()
        if value:
            return value
    return ""


def _collect_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_collect_text(item) for item in value)
    if not isinstance(value, dict):
        return ""
    chunks: list[str] = []
    for key in (
        "text",
        "content",
        "plainText",
        "value",
        "title",
        "elements",
        "children",
        "paragraph",
        "heading",
        "list",
        "table",
        "cells",
    ):
        if key in value:
            chunks.append(_collect_text(value[key]))
    return "".join(chunks)


def _blocks_from_payload(payload: dict[str, Any]) -> list[Any]:
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    for container in (result, payload):
        for key in ("blocks", "data", "items", "list"):
            value = container.get(key) if isinstance(container, dict) else None
            if isinstance(value, list):
                return value
    return []


def _render_dingtalk_blocks(blocks: list[Any]) -> str:
    lines: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            text = _collect_text(block).strip()
            if text:
                lines.append(text)
            continue
        block_type = str(block.get("type") or block.get("blockType") or "").lower()
        text = _collect_text(block).strip()
        if not text:
            continue
        if "heading" in block_type or block_type.startswith("h"):
            level = int(block.get("level") or block.get("headingLevel") or 2)
            lines.append(f"{'#' * min(max(level, 1), 6)} {text}")
        elif "ordered" in block_type:
            lines.append(f"1. {text}")
        elif "bullet" in block_type or "list" in block_type:
            lines.append(f"- {text}")
        elif "quote" in block_type:
            lines.append(f"> {text}")
        else:
            lines.append(text)
    return "\n\n".join(lines)


def list_external_connectors(project_id: str, connector_type: str | None = None) -> str:
    items = registry.get_external_connectors(project_id, connector_type=connector_type, only_enabled=True)
    if not items:
        suffix = f" type={connector_type}" if connector_type else ""
        return f"项目 '{project_id}' 尚未注册 External Connector{suffix}"
    return _json([_masked_config(item) for item in items])


def redis_exists(project_id: str, connector_id: str, key: str) -> str:
    try:
        import redis
    except ImportError:
        return "Redis 工具依赖缺失：请安装 redis>=5"
    item = _connector(project_id, connector_id, "redis")
    cfg, sec = item.config, item.secrets
    client = redis.Redis(
        host=cfg.get("host"),
        port=int(cfg.get("port", 6379)),
        db=int(cfg.get("db", 0)),
        username=sec.get("username") or cfg.get("username"),
        password=sec.get("password"),
        socket_timeout=float(cfg.get("socket_timeout", 3)),
        decode_responses=True,
    )
    return _json({"connector_id": connector_id, "key": key, "exists": bool(client.exists(key)), "ttl": client.ttl(key)})


def redis_get(project_id: str, connector_id: str, key: str, max_chars: int = 4000) -> str:
    try:
        import redis
    except ImportError:
        return "Redis 工具依赖缺失：请安装 redis>=5"
    item = _connector(project_id, connector_id, "redis")
    cfg, sec = item.config, item.secrets
    client = redis.Redis(
        host=cfg.get("host"),
        port=int(cfg.get("port", 6379)),
        db=int(cfg.get("db", 0)),
        username=sec.get("username") or cfg.get("username"),
        password=sec.get("password"),
        socket_timeout=float(cfg.get("socket_timeout", 3)),
        decode_responses=True,
    )
    value = client.get(key)
    if value is not None and len(value) > max_chars:
        value = value[:max_chars] + "..."
    return _json({"connector_id": connector_id, "key": key, "value": value, "ttl": client.ttl(key)})


def object_storage_head(project_id: str, connector_id: str, object_key: str) -> str:
    try:
        import oss2
    except ImportError:
        return "OSS 工具依赖缺失：请安装 oss2"
    item = _connector(project_id, connector_id, "object_storage")
    cfg, sec = item.config, item.secrets
    auth = oss2.Auth(sec.get("access_key_id", ""), sec.get("access_key_secret", ""))
    bucket = oss2.Bucket(auth, cfg.get("endpoint"), cfg.get("bucket"))
    try:
        headers = bucket.head_object(object_key).headers
        return _json({
            "connector_id": connector_id,
            "bucket": cfg.get("bucket"),
            "object_key": object_key,
            "exists": True,
            "content_length": headers.get("Content-Length"),
            "content_type": headers.get("Content-Type"),
            "etag": headers.get("ETag"),
            "last_modified": headers.get("Last-Modified"),
        })
    except Exception as e:  # noqa: BLE001
        return _json({"connector_id": connector_id, "object_key": object_key, "exists": False, "error": str(e)})


def object_storage_list(project_id: str, connector_id: str, prefix: str, max_keys: int = 50) -> str:
    try:
        import oss2
    except ImportError:
        return "OSS 工具依赖缺失：请安装 oss2"
    item = _connector(project_id, connector_id, "object_storage")
    cfg, sec = item.config, item.secrets
    auth = oss2.Auth(sec.get("access_key_id", ""), sec.get("access_key_secret", ""))
    bucket = oss2.Bucket(auth, cfg.get("endpoint"), cfg.get("bucket"))
    rows = []
    for index, obj in enumerate(oss2.ObjectIterator(bucket, prefix=prefix)):
        if index >= max_keys:
            break
        rows.append({
            "key": obj.key,
            "size": obj.size,
            "last_modified": obj.last_modified,
            "etag": obj.etag,
        })
    return _json({"connector_id": connector_id, "bucket": cfg.get("bucket"), "prefix": prefix, "objects": rows})


def queue_overview(project_id: str, connector_id: str, name_filter: str = "") -> str:
    item = _connector(project_id, connector_id, "queue")
    cfg, sec = item.config, item.secrets
    host = cfg.get("management_url") or f"http://{cfg.get('host')}:{cfg.get('management_port', 15672)}"
    username = sec.get("username") or cfg.get("username")
    password = sec.get("password")
    vhost = cfg.get("vhost", "/")
    try:
        with httpx.Client(timeout=float(cfg.get("timeout", 5))) as client:
            resp = client.get(
                urljoin(host.rstrip("/") + "/", f"api/queues/{quote(vhost, safe='')}"),
                auth=(username, password),
            )
            resp.raise_for_status()
            queues = resp.json()
    except Exception as e:  # noqa: BLE001
        return _json({"connector_id": connector_id, "error": str(e)})
    rows = []
    for q in queues:
        name = q.get("name", "")
        if name_filter and name_filter not in name:
            continue
        rows.append({
            "name": name,
            "messages": q.get("messages"),
            "messages_ready": q.get("messages_ready"),
            "messages_unacknowledged": q.get("messages_unacknowledged"),
            "consumers": q.get("consumers"),
            "state": q.get("state"),
        })
    return _json({"connector_id": connector_id, "vhost": vhost, "queues": rows[:100]})


def vector_collection_info(project_id: str, connector_id: str) -> str:
    try:
        from pymilvus import MilvusClient
    except ImportError:
        return "Vector Store 工具依赖缺失：请安装 pymilvus"
    item = _connector(project_id, connector_id, "vector_store")
    cfg, sec = item.config, item.secrets
    collection_name = cfg.get("collection_name") or cfg.get("embedding_collection_name")
    try:
        client = MilvusClient(uri=cfg.get("uri"), token=sec.get("token"), db_name=cfg.get("db_name"))
        desc = client.describe_collection(collection_name)
        stats = client.get_collection_stats(collection_name)
        return _json({"connector_id": connector_id, "collection": desc, "stats": stats})
    except Exception as e:  # noqa: BLE001
        logger.warning("vector_collection_info failed: {}", e)
        return _json({"connector_id": connector_id, "collection_name": collection_name, "error": str(e)})


def http_health_check(project_id: str, connector_id: str, path: str = "") -> str:
    item = _connector(project_id, connector_id, "http_service")
    cfg, sec = item.config, item.secrets
    base_url = str(cfg.get("base_url") or "").rstrip("/") + "/"
    target = urljoin(base_url, path.lstrip("/"))
    headers = dict(cfg.get("headers") or {})
    if sec.get("bearer_token"):
        headers["Authorization"] = f"Bearer {sec['bearer_token']}"
    try:
        with httpx.Client(timeout=float(cfg.get("timeout", 5)), follow_redirects=False) as client:
            resp = client.get(target, headers=headers)
        return _json({
            "connector_id": connector_id,
            "url": target,
            "status_code": resp.status_code,
            "content_type": resp.headers.get("content-type"),
            "text_preview": resp.text[:500],
        })
    except Exception as e:  # noqa: BLE001
        return _json({"connector_id": connector_id, "url": target, "error": str(e)})


def _safe_http_target(item: ExternalConnectorItem, path: str) -> str:
    cfg = item.config
    base_url = str(cfg.get("base_url") or "").strip()
    if not base_url:
        raise ValueError("HTTP Service Connector 缺少 config.base_url")
    parsed_base = urlparse(base_url)
    if parsed_base.scheme not in {"http", "https"} or not parsed_base.netloc:
        raise ValueError("config.base_url 必须是完整 http(s) URL")

    relative_path = str(path or "").strip()
    parsed_path = urlparse(relative_path)
    if parsed_path.scheme or parsed_path.netloc:
        raise ValueError("path 必须是相对路径，不能传入完整 URL")

    target = urljoin(base_url.rstrip("/") + "/", relative_path.lstrip("/"))
    parsed_target = urlparse(target)
    if (parsed_target.scheme, parsed_target.netloc) != (parsed_base.scheme, parsed_base.netloc):
        raise ValueError("HTTP 调用目标必须与 connector base_url 同源")
    return target


def _http_auth_headers(item: ExternalConnectorItem, extra_headers: dict[str, Any] | None = None) -> dict[str, str]:
    cfg, sec = item.config, item.secrets
    headers: dict[str, str] = {str(k): str(v) for k, v in dict(cfg.get("headers") or {}).items()}

    bearer_token = _env_or_value(sec, "bearer_token", "bearer_token_env")
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"

    api_key = _env_or_value(sec, "api_key", "api_key_env")
    if api_key:
        header_name = str(sec.get("api_key_header") or cfg.get("api_key_header") or "X-API-Key").strip()
        headers[header_name] = api_key

    for key, value in dict(extra_headers or {}).items():
        if key and value is not None:
            headers[str(key)] = str(value)
    return headers


def _allowed_http_methods(item: ExternalConnectorItem) -> set[str]:
    raw = item.config.get("allowed_methods") or ["GET"]
    if isinstance(raw, str):
        raw = [part.strip() for part in raw.split(",")]
    methods = {str(method).upper() for method in raw if str(method).strip()}
    return methods or {"GET"}


def http_call(
    project_id: str,
    connector_id: str,
    method: str,
    path: str,
    query: dict[str, Any] | None = None,
    body: Any = None,
    headers: dict[str, Any] | None = None,
    max_chars: int = 4000,
) -> str:
    """Call a registered HTTP service and return a bounded response preview."""
    item = _connector(project_id, connector_id, "http_service")
    method_upper = str(method or "GET").upper()
    allowed_methods = _allowed_http_methods(item)
    if method_upper not in allowed_methods:
        return _json({
            "connector_id": connector_id,
            "method": method_upper,
            "allowed_methods": sorted(allowed_methods),
            "error": "HTTP method 未在 connector config.allowed_methods 中启用",
        })

    target = _safe_http_target(item, path)
    request_headers = _http_auth_headers(item, headers)
    timeout = float(item.config.get("timeout", 10))
    max_chars = max(200, min(int(max_chars or 4000), 20000))
    request_kwargs: dict[str, Any] = {
        "method": method_upper,
        "url": target,
        "headers": request_headers,
        "params": query or {},
    }
    if body is not None:
        request_kwargs["json"] = body

    try:
        with httpx.Client(timeout=timeout, follow_redirects=bool(item.config.get("follow_redirects", False))) as client:
            resp = client.request(**request_kwargs)
        text = resp.text
        result: dict[str, Any] = {
            "connector_id": connector_id,
            "method": method_upper,
            "url": str(resp.url),
            "status_code": resp.status_code,
            "content_type": resp.headers.get("content-type"),
            "elapsed_ms": round(resp.elapsed.total_seconds() * 1000, 2),
            "truncated": len(text) > max_chars,
        }
        try:
            result["json"] = resp.json()
        except ValueError:
            result["text"] = text[:max_chars]
        else:
            rendered = _json(result["json"])
            if len(rendered) > max_chars:
                result.pop("json", None)
                result["text"] = rendered[:max_chars]
                result["truncated"] = True
        return _json(result)
    except Exception as e:  # noqa: BLE001
        return _json({"connector_id": connector_id, "method": method_upper, "url": target, "error": str(e)})


def dingtalk_doc_read(project_id: str, connector_id: str, doc_url: str, max_chars: int = 20000) -> str:
    """Read a DingTalk document by URL using a fixed operator unionId.

    DingTalk document APIs are app-token authenticated but still permission-check
    against operatorId. For the fast path, configure a service account unionId
    with document access instead of looking up the message sender.
    """
    item = _connector(project_id, connector_id, "dingtalk_doc")
    _assert_allowed_dingtalk_doc_url(item, doc_url)
    operator_id = _dingtalk_operator_id(item)
    token = _dingtalk_access_token(item)
    headers = {"x-acs-dingtalk-access-token": token}
    timeout = float(item.config.get("timeout", 10))

    try:
        with httpx.Client(timeout=timeout) as client:
            node_resp = client.post(
                f"{_DINGTALK_API_BASE}/v2.0/wiki/nodes/queryByUrl",
                params={"operatorId": operator_id},
                headers=headers,
                json={"url": doc_url},
            )
            node_resp.raise_for_status()
            node_payload = node_resp.json()
            node = _extract_dingtalk_node(node_payload)
            doc_key = _pick_doc_key(node)
            if not doc_key:
                return _json({
                    "connector_id": connector_id,
                    "url": doc_url,
                    "node": node,
                    "error": "已解析链接，但响应中没有可用于读取正文的 docKey/documentId/resourceId/nodeId",
                })

            blocks_resp = client.get(
                f"{_DINGTALK_API_BASE}/v1.0/doc/suites/documents/{quote(doc_key, safe='')}/blocks",
                params={"operatorId": operator_id},
                headers=headers,
            )
            blocks_resp.raise_for_status()
            blocks_payload = blocks_resp.json()
    except httpx.HTTPStatusError as e:
        text = e.response.text[:1000] if e.response is not None else ""
        return _json({"connector_id": connector_id, "url": doc_url, "status_code": e.response.status_code, "error": text})
    except Exception as e:  # noqa: BLE001
        return _json({"connector_id": connector_id, "url": doc_url, "error": str(e)})

    blocks = _blocks_from_payload(blocks_payload)
    content = _render_dingtalk_blocks(blocks)
    limit = min(max(1000, int(max_chars or item.config.get("max_chars", 20000))), 100000)
    truncated = len(content) > limit
    if truncated:
        content = content[:limit] + "\n\n..."
    return _json({
        "connector_id": connector_id,
        "url": doc_url,
        "node": {
            "nodeId": node.get("nodeId"),
            "name": node.get("name") or node.get("title"),
            "type": node.get("type"),
            "docKey": doc_key,
        },
        "block_count": len(blocks),
        "truncated": truncated,
        "content": content,
    })
