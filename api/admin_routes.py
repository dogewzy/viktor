"""面向独立前端的管理 API。

这些接口把原先 Starlette-Admin 隐含的 CRUD 能力显式化为 JSON API。
写入仍复用注册路由中的持久化约定，避免 DB 与内存 Registry 行为分叉。
"""
from __future__ import annotations

import re
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

import httpx
import yaml
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import or_
from sqlalchemy.orm import Session

from api.register_routes import (
    bind_group,
    register_context,
    register_database_connector,
    register_external_connector,
    register_glossary,
    register_knowledge_note,
    register_project,
    register_repository_connector,
    register_log_connector,
    register_runtime_context,
    register_skill,
    unbind_group,
    unregister_context,
    unregister_database_connector,
    unregister_external_connector,
    unregister_glossary,
    unregister_knowledge_note,
    unregister_project,
    unregister_repository_connector,
    unregister_log_connector,
    unregister_runtime_context,
    unregister_skill,
)
from core.database import SessionLocal
from core.audit.service import get_trace_events, list_traces
from core.evaluation.service import (
    list_trace_evaluations,
    queue_trace_evaluation,
)
from core.learning.service import (
    apply_learning_candidate,
    list_learning_candidates,
    run_trace_learning,
    update_learning_candidate,
)
from core.llm_metrics import (
    list_chat_token_usage,
    list_coding_task_token_usage,
    list_llm_calls,
    llm_summary,
    llm_usage_timeseries,
    provider_health,
)
from core.models import (
    ChatMessageModel,
    ContextModel,
    DatabaseConnectorModel,
    ExternalConnectorModel,
    GitLabTaskModel,
    GlossaryModel,
    GroupBindingModel,
    KnowledgeNoteModel,
    LLMCallModel,
    OnboardingArtifactModel,
    OnboardingTaskModel,
    ProjectModel,
    RepositoryConnectorModel,
    ReportModel,
    LogConnectorModel,
    RuntimeContextModel,
    SkillModel,
    TraceEvaluationModel,
)
from core.registry import (
    ContextItem,
    DatabaseConnectorItem,
    ExternalConnectorItem,
    GlossaryItem,
    GroupBinding,
    KnowledgeNoteItem,
    ProjectItem,
    RepositoryConnectorItem,
    LogConnectorItem,
    RuntimeContextItem,
    SkillItem,
    normalize_conversation_id,
    registry,
)
from core.skill_service import KNOWN_TOOLS, draft_skill_from_text

router = APIRouter(prefix="/api/v1/admin", tags=["管理后台"])


class AdminListResponse(BaseModel):
    items: list[dict[str, Any]]
    total: int
    limit: int
    offset: int


class DatabaseConnectorAdminPayload(BaseModel):
    id: str = Field(description="数据库连接器 ID")
    project_id: str
    type: str = "mysql"
    host: str
    port: int = 3306
    username: str
    password: str | None = Field(default=None, description="编辑时留空表示沿用原密码")
    database: str
    readonly: bool = True
    charset: str = "utf8mb4"
    ssh_tunnel: dict[str, Any] | None = None


class LearningCandidatePatch(BaseModel):
    target_id: str | None = None
    title: str | None = None
    content: str | None = None
    payload: dict[str, Any] | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    risk_level: str | None = None
    status: str | None = None


class LogConnectorAdminPayload(BaseModel):
    id: str = Field(description="日志连接器 ID")
    project_id: str
    sls_project: str
    logstore: str
    display_name: str = ""
    description: str = ""
    enabled: bool = True


class ExternalConnectorAdminPayload(BaseModel):
    id: str = Field(description="外部连接器 ID")
    project_id: str
    connector_type: str
    display_name: str = ""
    description: str = ""
    config: dict[str, Any] = Field(default_factory=dict)
    secrets: dict[str, Any] | None = Field(default=None, description="编辑时留空表示沿用原 secrets")
    enabled: bool = True


class OpenAPIImportRequest(BaseModel):
    project_id: str
    docs_url: str = Field(description="FastAPI /docs、/redoc、openapi.json、swagger.json 或常见 api-docs 地址")
    id: str = Field(default="", description="Connector ID，留空则按文档标题或域名生成")
    display_name: str = ""
    description: str = ""
    timeout: float = 10
    allowed_methods: list[str] | None = None
    enabled: bool = True


class RuntimeContextAdminPayload(BaseModel):
    id: str = Field(description="运行时上下文 ID")
    project_id: str
    environment: str = "prod"
    source_type: str = "kubevela"
    source_repo: str = ""
    source_path: str = ""
    app_name: str = ""
    namespace: str = ""
    workload_type: str = "Deployment"
    workload_name: str = ""
    service_name: str = ""
    clusters: list[str] = Field(default_factory=list)
    selector: dict[str, Any] = Field(default_factory=dict)
    labels: dict[str, Any] = Field(default_factory=dict)
    replicas: int | None = None
    image: str = ""
    command: list[str] = Field(default_factory=list)
    ports: list[dict[str, Any]] = Field(default_factory=list)
    resources: dict[str, Any] = Field(default_factory=dict)
    probes: dict[str, Any] = Field(default_factory=dict)
    log_bindings: list[dict[str, Any]] = Field(default_factory=list)
    exposures: list[dict[str, Any]] = Field(default_factory=list)
    scheduling: dict[str, Any] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class SkillDraftRequest(BaseModel):
    project_id: str
    raw_text: str = Field(min_length=1)


def _db() -> Session:
    return SessionLocal()


@contextmanager
def _session():
    db = _db()
    try:
        yield db
    finally:
        db.close()


def _dt(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return None


def _page(query, *, limit: int, offset: int) -> AdminListResponse:
    total = query.count()
    rows = query.offset(offset).limit(limit).all()
    return AdminListResponse(
        items=[_serialize(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


SerializerGetter = str | Callable[[Any], Any]


def _dt_attr(name: str) -> Callable[[Any], str | None]:
    return lambda row: _dt(getattr(row, name))


def _bool_attr(name: str) -> Callable[[Any], bool]:
    return lambda row: bool(getattr(row, name))


def _json_attr(name: str, default: list | dict) -> Callable[[Any], list | dict]:
    return lambda row: getattr(row, name) or default.copy()


def _const(value: Any) -> Callable[[Any], Any]:
    return lambda _: value.copy() if isinstance(value, (dict, list)) else value


SERIALIZER_FIELDS: dict[type, list[tuple[str, SerializerGetter]]] = {
    ProjectModel: [
        ("id", "project_id"),
        ("project_id", "project_id"),
        ("name", "name"),
        ("description", "description"),
        ("git_url", "git_url"),
        ("default_branch", "default_branch"),
        ("k8s_workload", "k8s_workload"),
        ("created_at", _dt_attr("created_at")),
        ("updated_at", _dt_attr("updated_at")),
    ],
    RepositoryConnectorModel: [
        ("id", "connector_id"),
        ("project_id", "project_id"),
        ("display_name", "display_name"),
        ("description", "description"),
        ("git_url", "git_url"),
        ("default_branch", "default_branch"),
        ("k8s_workload", "k8s_workload"),
        ("sort_order", "sort_order"),
        ("build_venv", lambda row: bool(row.build_venv)),
        ("language", "language"),
        ("test_command", "test_command"),
        ("lint_command", "lint_command"),
        ("maintainer_mobile", "maintainer_mobile"),
        ("created_at", _dt_attr("created_at")),
        ("updated_at", _dt_attr("updated_at")),
    ],
    ContextModel: [
        ("id", "context_id"),
        ("project_id", "project_id"),
        ("context_id", "context_id"),
        ("priority", "priority"),
        ("content", "content"),
        ("created_at", _dt_attr("created_at")),
        ("updated_at", _dt_attr("updated_at")),
    ],
    DatabaseConnectorModel: [
        ("id", "connector_id"),
        ("project_id", "project_id"),
        ("type", "type"),
        ("host", "host"),
        ("port", "port"),
        ("username", "username"),
        ("password", _const("")),
        ("database", "database_name"),
        ("readonly", _bool_attr("readonly_flag")),
        ("charset", "charset_name"),
        ("ssh_tunnel", "ssh_tunnel"),
        ("created_at", _dt_attr("created_at")),
        ("updated_at", _dt_attr("updated_at")),
    ],
    LogConnectorModel: [
        ("id", "connector_id"),
        ("project_id", "project_id"),
        ("display_name", "display_name"),
        ("sls_project", "sls_project"),
        ("logstore", "logstore"),
        ("description", "description"),
        ("enabled", _bool_attr("enabled")),
        ("created_at", _dt_attr("created_at")),
        ("updated_at", _dt_attr("updated_at")),
    ],
    ExternalConnectorModel: [
        ("id", "connector_id"),
        ("project_id", "project_id"),
        ("connector_type", "connector_type"),
        ("display_name", "display_name"),
        ("description", "description"),
        ("config", _json_attr("config", {})),
        ("secrets", _const({})),
        ("enabled", _bool_attr("enabled")),
        ("created_at", _dt_attr("created_at")),
        ("updated_at", _dt_attr("updated_at")),
    ],
    RuntimeContextModel: [
        ("id", "runtime_id"),
        ("project_id", "project_id"),
        ("environment", "environment"),
        ("source_type", "source_type"),
        ("source_repo", "source_repo"),
        ("source_path", "source_path"),
        ("app_name", "app_name"),
        ("namespace", "namespace"),
        ("workload_type", "workload_type"),
        ("workload_name", "workload_name"),
        ("service_name", "service_name"),
        ("clusters", _json_attr("clusters", [])),
        ("selector", _json_attr("selector", {})),
        ("labels", _json_attr("labels", {})),
        ("replicas", "replicas"),
        ("image", "image"),
        ("command", _json_attr("command", [])),
        ("ports", _json_attr("ports", [])),
        ("resources", _json_attr("resources", {})),
        ("probes", _json_attr("probes", {})),
        ("log_bindings", _json_attr("log_bindings", [])),
        ("exposures", _json_attr("exposures", [])),
        ("scheduling", _json_attr("scheduling", {})),
        ("config", _json_attr("config", {})),
        ("enabled", _bool_attr("enabled")),
        ("created_at", _dt_attr("created_at")),
        ("updated_at", _dt_attr("updated_at")),
    ],
    SkillModel: [
        ("id", "skill_id"),
        ("project_id", "project_id"),
        ("name", "name"),
        ("kind", "kind"),
        ("description", "description"),
        ("trigger_examples", _json_attr("trigger_examples", [])),
        ("input_contract", _json_attr("input_contract", {})),
        ("required_contexts", _json_attr("required_contexts", [])),
        ("required_tools", _json_attr("required_tools", [])),
        ("related_glossary_terms", _json_attr("related_glossary_terms", [])),
        ("instructions", _json_attr("instructions", [])),
        ("output_contract", _json_attr("output_contract", {})),
        ("safety_policy", _json_attr("safety_policy", {})),
        ("source_type", "source_type"),
        ("source_uri", "source_uri"),
        ("raw_content", "raw_content"),
        ("status", "status"),
        ("version", "version"),
        ("created_at", _dt_attr("created_at")),
        ("updated_at", _dt_attr("updated_at")),
    ],
    GroupBindingModel: [
        ("id", "conversation_id"),
        ("conversation_id", "conversation_id"),
        ("project_id", "project_id"),
        ("group_name", "group_name"),
        ("created_at", _dt_attr("created_at")),
        ("updated_at", _dt_attr("updated_at")),
    ],
    GlossaryModel: [
        ("id", "glossary_id"),
        ("project_id", "project_id"),
        ("glossary_id", "glossary_id"),
        ("term", "term"),
        ("aliases", _json_attr("aliases", [])),
        ("code_keywords", _json_attr("code_keywords", [])),
        ("description", "description"),
        ("enabled", _bool_attr("enabled")),
        ("created_at", _dt_attr("created_at")),
        ("updated_at", _dt_attr("updated_at")),
    ],
    KnowledgeNoteModel: [
        ("id", "note_id"),
        ("project_id", "project_id"),
        ("note_id", "note_id"),
        ("kind", "kind"),
        ("scope", "scope"),
        ("title", "title"),
        ("content", "content"),
        ("tags", _json_attr("tags", [])),
        ("enabled", _bool_attr("enabled")),
        ("source", "source"),
        ("created_at", _dt_attr("created_at")),
        ("updated_at", _dt_attr("updated_at")),
    ],
    GitLabTaskModel: [
        ("id", "task_id"),
        ("task_id", "task_id"),
        ("project_id", "project_id"),
        ("repo_url", "repo_url"),
        ("branch", "branch"),
        ("status", "status"),
        ("message", "message"),
        ("contexts_generated", _json_attr("contexts_generated", [])),
        ("created_at", _dt_attr("created_at")),
        ("updated_at", _dt_attr("updated_at")),
    ],
    OnboardingTaskModel: [
        ("id", "task_id"),
        ("task_id", "task_id"),
        ("project_id", "project_id"),
        ("repo_url", "repo_url"),
        ("branch", "branch"),
        ("status", "status"),
        ("stage", "stage"),
        ("message", "message"),
        ("analysis_level", "analysis_level"),
        ("profile", _json_attr("profile", {})),
        ("stats", _json_attr("stats", {})),
        ("created_at", _dt_attr("created_at")),
        ("updated_at", _dt_attr("updated_at")),
    ],
    OnboardingArtifactModel: [
        ("id", "artifact_id"),
        ("artifact_id", "artifact_id"),
        ("task_id", "task_id"),
        ("project_id", "project_id"),
        ("artifact_type", "artifact_type"),
        ("target_id", "target_id"),
        ("title", "title"),
        ("content", "content"),
        ("payload", _json_attr("payload", {})),
        ("status", "status"),
        ("created_at", _dt_attr("created_at")),
        ("updated_at", _dt_attr("updated_at")),
    ],
    ReportModel: [
        ("id", "id"),
        ("thread_id", "thread_id"),
        ("project_id", "project_id"),
        ("title", "title"),
        ("summary", "summary"),
        ("html_body", "html_body"),
        ("created_at", _dt_attr("created_at")),
        ("expires_at", _dt_attr("expires_at")),
    ],
    ChatMessageModel: [
        ("id", "id"),
        ("thread_id", "thread_id"),
        ("session_id", "thread_id"),
        ("topic_thread_id", "topic_thread_id"),
        ("project_id", "project_id"),
        ("turn_id", "turn_id"),
        ("role", "role"),
        ("content", "content"),
        ("tool_calls", "tool_calls"),
        ("tool_call_id", "tool_call_id"),
        ("tool_name", "tool_name"),
        ("truncated", _bool_attr("truncated")),
        ("created_at", _dt_attr("created_at")),
    ],
    LLMCallModel: [
        ("id", "id"),
        ("request_id", "request_id"),
        ("feature", "feature"),
        ("provider", "provider"),
        ("model", "model"),
        ("attempt_index", "attempt_index"),
        ("fallback_from", "fallback_from"),
        ("status", "status"),
        ("streaming", _bool_attr("streaming")),
        ("started_at", _dt_attr("started_at")),
        ("first_token_ms", "first_token_ms"),
        ("duration_ms", "duration_ms"),
        ("prompt_tokens", "prompt_tokens"),
        ("completion_tokens", "completion_tokens"),
        ("total_tokens", "total_tokens"),
        ("output_chars", "output_chars"),
        ("tokens_per_second", "tokens_per_second"),
        ("error_type", "error_type"),
        ("error_message", "error_message"),
        ("meta", "meta"),
    ],
    TraceEvaluationModel: [
        ("id", "evaluation_id"),
        ("evaluation_id", "evaluation_id"),
        ("trace_id", "trace_id"),
        ("project_id", "project_id"),
        ("status", "status"),
        ("sample_type", "sample_type"),
        ("evaluator_version", "evaluator_version"),
        ("metrics", _json_attr("metrics", [])),
        ("scores", _json_attr("scores", {})),
        ("sample_preview", _json_attr("sample_preview", {})),
        ("diagnostics", _json_attr("diagnostics", {})),
        ("error", "error"),
        ("created_at", _dt_attr("created_at")),
        ("updated_at", _dt_attr("updated_at")),
    ],
}


def _field_value(row: Any, getter: SerializerGetter) -> Any:
    if isinstance(getter, str):
        return getattr(row, getter)
    return getter(row)


def _serialize(row: Any) -> dict[str, Any]:
    for model, fields in SERIALIZER_FIELDS.items():
        if isinstance(row, model):
            return {key: _field_value(row, getter) for key, getter in fields}
    raise TypeError(f"不支持序列化类型: {type(row)!r}")


def _get_or_404(db: Session, model: type, key: Any, label: str) -> Any:
    row = db.get(model, key)
    if row is None:
        raise HTTPException(status_code=404, detail=f"{label} 不存在")
    return row


def _get_serialized(model: type, key: Any, label: str) -> dict:
    with _session() as db:
        return _serialize(_get_or_404(db, model, key, label))


def _list_rows(
    model: type,
    *,
    project_id: str | None = None,
    q: str | None = None,
    limit: int = 100,
    offset: int = 0,
    q_columns: list[Any] | tuple[Any, ...] = (),
    order_by: list[Any] | tuple[Any, ...] = (),
    extra_filters: list[Any] | tuple[Any, ...] = (),
    project_column: Any | None = None,
) -> AdminListResponse:
    project_id, q, limit, offset = _filters(project_id=project_id, q=q, limit=limit, offset=offset)
    with _session() as db:
        query = db.query(model)
        if project_id:
            column = project_column if project_column is not None else getattr(model, "project_id")
            query = query.filter(column == project_id)
        for condition in extra_filters:
            if condition is not None:
                query = query.filter(condition)
        if q and q_columns:
            query = query.filter(or_(*(column.contains(q) for column in q_columns)))
        return _page(query.order_by(*order_by), limit=limit, offset=offset)


def _ensure_project_path(item: Any, project_id: str | None) -> None:
    if project_id and item.id != project_id:
        raise HTTPException(status_code=400, detail="路径 project_id 与请求体 id 不一致")


def _ensure_resource_path(
    item: Any,
    project_id: str | None,
    resource_id: str | None,
    *,
    item_id_attr: str = "id",
) -> None:
    if project_id and (item.project_id != project_id or getattr(item, item_id_attr) != resource_id):
        raise HTTPException(status_code=400, detail="路径参数与请求体不一致")


def _existing_value(model: type, key: Any, attr: str) -> Any | None:
    with _session() as db:
        row = db.get(model, key)
        return None if row is None else getattr(row, attr)


def _filters(
    *,
    project_id: str | None = None,
    q: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[str | None, str | None, int, int]:
    if limit <= 0 or limit > 1000:
        raise HTTPException(status_code=400, detail="limit 需在 1~1000 之间")
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset 不能为负数")
    return project_id or None, (q or "").strip() or None, limit, offset


def _parse_dt_param(value: str | None, name: str) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"{name} 必须为 ISO datetime") from e


def _slugify_connector_id(text: str, fallback: str = "http-service") -> str:
    pieces = re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", (text or "").lower())
    slug = "-".join(pieces).strip("-")
    return (slug or fallback)[:120]


def _origin(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        raise HTTPException(status_code=400, detail="URL 必须是完整 http(s) 地址")
    return f"{parsed.scheme}://{parsed.netloc}"


def _parse_openapi_text(text: str, source_url: str) -> dict[str, Any] | None:
    stripped = (text or "").lstrip()
    if stripped.startswith("<"):
        return None
    try:
        payload = yaml.safe_load(text)
    except yaml.YAMLError as e:
        if stripped[:1] not in {"{", "["} and not re.search(r"^\s*(openapi|swagger|paths)\s*:", text, re.M):
            return None
        raise HTTPException(status_code=400, detail=f"OpenAPI 文档解析失败: {e}") from e
    if not isinstance(payload, dict):
        return None
    if "paths" not in payload or not isinstance(payload.get("paths"), dict):
        return None
    if not (payload.get("openapi") or payload.get("swagger")):
        raise HTTPException(status_code=400, detail=f"{source_url} 看起来不是 OpenAPI/Swagger 文档")
    return payload


def _extract_openapi_refs_from_html(html: str) -> list[str]:
    patterns = [
        r"\bspec-url\s*=\s*['\"]([^'\"]+)['\"]",
        r"\burl\s*:\s*['\"]([^'\"]+)['\"]",
        r"['\"]url['\"]\s*:\s*['\"]([^'\"]+)['\"]",
        r"\burls\s*:\s*\[\s*\{\s*url\s*:\s*['\"]([^'\"]+)['\"]",
        r"['\"]urls['\"]\s*:\s*\[\s*\{\s*['\"]url['\"]\s*:\s*['\"]([^'\"]+)['\"]",
    ]
    refs: list[str] = []
    for pattern in patterns:
        refs.extend(match.group(1) for match in re.finditer(pattern, html))
    return [ref for ref in refs if ref and not ref.startswith("data:")]


def _candidate_openapi_urls(docs_url: str, html: str | None = None) -> list[str]:
    candidates: list[str] = []
    if html:
        candidates.extend(urljoin(docs_url, ref) for ref in _extract_openapi_refs_from_html(html))
    origin = _origin(docs_url)
    parsed = urlparse(docs_url)
    path = parsed.path or "/"
    if path.endswith((".json", ".yaml", ".yml")) or "api-docs" in path:
        candidates.append(docs_url)
    candidates.extend([
        urljoin(docs_url.rstrip("/") + "/", "openapi.json"),
        urljoin(docs_url, "../openapi.json"),
        f"{origin}/openapi.json",
        f"{origin}/swagger.json",
        f"{origin}/v3/api-docs",
        f"{origin}/v2/api-docs",
        f"{origin}/api-docs",
    ])

    unique: list[str] = []
    for candidate in candidates:
        if candidate not in unique:
            unique.append(candidate)
    return unique


def _fetch_openapi_document(docs_url: str, timeout: float) -> tuple[str, dict[str, Any]]:
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        try:
            first = client.get(docs_url)
            first.raise_for_status()
        except httpx.HTTPError as e:
            raise HTTPException(status_code=400, detail=f"读取文档地址失败: {e}") from e

        direct = _parse_openapi_text(first.text, str(first.url))
        if direct:
            return str(first.url), direct

        for candidate in _candidate_openapi_urls(str(first.url), first.text):
            try:
                resp = client.get(candidate)
                resp.raise_for_status()
            except httpx.HTTPError:
                continue
            parsed = _parse_openapi_text(resp.text, str(resp.url))
            if parsed:
                return str(resp.url), parsed

    raise HTTPException(status_code=400, detail="没有从该地址识别到 OpenAPI/Swagger 文档")


def _schema_ref_name(schema: dict[str, Any]) -> str:
    ref = str(schema.get("$ref") or "")
    return ref.rsplit("/", 1)[-1] if ref else ""


def _schema_preview(schema: dict[str, Any]) -> dict[str, Any]:
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    required = schema.get("required") if isinstance(schema.get("required"), list) else []
    return {
        "type": schema.get("type") or ("enum" if "enum" in schema else "object"),
        "field_count": len(props),
        "required_count": len(required),
        "required": required[:20],
    }


def _operation_preview(openapi: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    operations: list[dict[str, Any]] = []
    methods: set[str] = set()
    for path, path_item in sorted((openapi.get("paths") or {}).items()):
        if not isinstance(path_item, dict):
            continue
        for method, operation in sorted(path_item.items()):
            method_upper = method.upper()
            if method_upper not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
                continue
            methods.add(method_upper)
            operation = operation if isinstance(operation, dict) else {}
            request_body = operation.get("requestBody") if isinstance(operation.get("requestBody"), dict) else {}
            response_schemas = []
            for response in (operation.get("responses") or {}).values():
                if not isinstance(response, dict):
                    continue
                for content in (response.get("content") or {}).values():
                    if not isinstance(content, dict):
                        continue
                    schema = content.get("schema") if isinstance(content.get("schema"), dict) else {}
                    name = _schema_ref_name(schema)
                    if name:
                        response_schemas.append(name)
            operations.append({
                "method": method_upper,
                "path": path,
                "summary": operation.get("summary") or "",
                "operation_id": operation.get("operationId") or "",
                "tags": operation.get("tags") or [],
                "deprecated": bool(operation.get("deprecated")),
                "parameter_count": len(operation.get("parameters") or []),
                "has_request_body": bool(request_body),
                "response_schemas": sorted(set(response_schemas))[:8],
            })
    return operations, sorted(methods)


def _resolve_openapi_base_url(openapi_url: str, openapi: dict[str, Any]) -> str:
    servers = openapi.get("servers") if isinstance(openapi.get("servers"), list) else []
    for server in servers:
        if not isinstance(server, dict):
            continue
        raw_url = str(server.get("url") or "").strip()
        if raw_url and "{" not in raw_url:
            return urljoin(openapi_url, raw_url).rstrip("/")
    return _origin(openapi_url)


def _openapi_import_payload(req: OpenAPIImportRequest) -> tuple[ExternalConnectorItem, dict[str, Any]]:
    docs_url = req.docs_url.strip()
    if not docs_url:
        raise HTTPException(status_code=400, detail="docs_url 不能为空")
    openapi_url, openapi = _fetch_openapi_document(docs_url, timeout=float(req.timeout or 10))
    info = openapi.get("info") if isinstance(openapi.get("info"), dict) else {}
    operations, discovered_methods = _operation_preview(openapi)
    schemas = openapi.get("components", {}).get("schemas", {}) if isinstance(openapi.get("components"), dict) else {}
    if not isinstance(schemas, dict):
        schemas = {}
    schema_summary = {name: _schema_preview(schema) for name, schema in sorted(schemas.items()) if isinstance(schema, dict)}
    connector_id = req.id.strip() or _slugify_connector_id(str(info.get("title") or urlparse(openapi_url).hostname or "http-service"))
    display_name = req.display_name.strip() or str(info.get("title") or connector_id)
    allowed_methods = [method.upper() for method in (req.allowed_methods or discovered_methods or ["GET"])]
    config = {
        "base_url": _resolve_openapi_base_url(openapi_url, openapi),
        "openapi_url": openapi_url,
        "allowed_methods": sorted(set(allowed_methods)),
        "timeout": float(req.timeout or 10),
        "openapi_summary": {
            "title": info.get("title") or "",
            "version": info.get("version") or "",
            "path_count": len(openapi.get("paths") or {}),
            "operation_count": len(operations),
            "schema_count": len(schema_summary),
            "tags": sorted({
                str(tag)
                for operation in operations
                for tag in operation.get("tags", [])
                if str(tag).strip()
            }),
            "operations": operations,
            "schemas": schema_summary,
        },
    }
    item = ExternalConnectorItem(
        id=connector_id,
        project_id=req.project_id,
        connector_type="http_service",
        display_name=display_name,
        description=req.description.strip() or f"Imported from {openapi_url}",
        config=config,
        secrets={},
        enabled=req.enabled,
    )
    return item, {
        "connector": item.model_dump(),
        "openapi_url": openapi_url,
        "base_url": config["base_url"],
        "summary": config["openapi_summary"],
    }


@router.get("/meta")
def get_admin_meta() -> dict:
    return {
        "resources": [
            {"id": "projects", "label": "项目", "writable": True},
            {"id": "repository-connectors", "label": "Repository Connector", "writable": True},
            {"id": "contexts", "label": "上下文", "writable": True},
            {"id": "database-connectors", "label": "Database Connector", "writable": True},
            {"id": "log-connectors", "label": "Log Connector", "writable": True},
            {"id": "external-connectors", "label": "External Connector", "writable": True},
            {"id": "runtime-contexts", "label": "Runtime Context", "writable": True},
            {"id": "skills", "label": "技能", "writable": True},
            {"id": "group-bindings", "label": "群绑定", "writable": True},
            {"id": "glossaries", "label": "业务术语", "writable": True},
            {"id": "knowledge-notes", "label": "知识笔记", "writable": True},
            {"id": "gitlab-tasks", "label": "GitLab 任务", "writable": False},
            {"id": "onboarding-tasks", "label": "接入审核", "writable": False},
            {"id": "onboarding-artifacts", "label": "接入候选产物", "writable": False},
            {"id": "reports", "label": "报告", "writable": False},
            {"id": "llm-calls", "label": "LLM 调用", "writable": False},
            {"id": "agent-traces", "label": "Agent Trace", "writable": False},
            {"id": "trace-evaluations", "label": "Trace Evaluation", "writable": False},
        ]
    }


@router.get("/projects", response_model=AdminListResponse)
def list_projects(q: str | None = None, limit: int = 100, offset: int = 0) -> AdminListResponse:
    return _list_rows(
        ProjectModel,
        q=q,
        limit=limit,
        offset=offset,
        q_columns=(ProjectModel.project_id, ProjectModel.name),
        order_by=(ProjectModel.project_id,),
    )


@router.get("/projects/{project_id}")
def get_project(project_id: str) -> dict:
    return _get_serialized(ProjectModel, project_id, "项目")


@router.post("/projects")
@router.put("/projects/{project_id}")
def save_project(item: ProjectItem, project_id: str | None = None) -> dict:
    _ensure_project_path(item, project_id)
    return register_project(item)


@router.delete("/projects/{project_id}")
def delete_project(project_id: str) -> dict:
    return unregister_project(project_id)


@router.get("/repository-connectors", response_model=AdminListResponse)
def list_repository_connectors(
    project_id: str | None = None,
    q: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> AdminListResponse:
    return _list_rows(
        RepositoryConnectorModel,
        project_id=project_id,
        q=q,
        limit=limit,
        offset=offset,
        q_columns=(
            RepositoryConnectorModel.connector_id,
            RepositoryConnectorModel.display_name,
            RepositoryConnectorModel.description,
            RepositoryConnectorModel.git_url,
            RepositoryConnectorModel.maintainer_mobile,
        ),
        order_by=(RepositoryConnectorModel.project_id, RepositoryConnectorModel.sort_order),
    )


@router.get("/repository-connectors/{project_id}/{connector_id}")
def get_repository_connector(project_id: str, connector_id: str) -> dict:
    return _get_serialized(RepositoryConnectorModel, (project_id, connector_id), "Repository Connector")


@router.post("/repository-connectors")
@router.put("/repository-connectors/{project_id}/{connector_id}")
def save_repository_connector(item: RepositoryConnectorItem, project_id: str | None = None, connector_id: str | None = None) -> dict:
    _ensure_resource_path(item, project_id, connector_id)
    return register_repository_connector(item.project_id, item)


@router.delete("/repository-connectors/{project_id}/{connector_id}")
def delete_repository_connector(project_id: str, connector_id: str) -> dict:
    return unregister_repository_connector(project_id, connector_id)


@router.get("/contexts", response_model=AdminListResponse)
def list_contexts(
    project_id: str | None = None,
    q: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> AdminListResponse:
    return _list_rows(
        ContextModel,
        project_id=project_id,
        q=q,
        limit=limit,
        offset=offset,
        q_columns=(ContextModel.context_id, ContextModel.content),
        order_by=(ContextModel.project_id, ContextModel.priority.desc()),
    )


@router.get("/contexts/{project_id}/{context_id}")
def get_context(project_id: str, context_id: str) -> dict:
    return _get_serialized(ContextModel, (project_id, context_id), "上下文")


@router.post("/contexts")
@router.put("/contexts/{project_id}/{context_id}")
def save_context(item: ContextItem, project_id: str | None = None, context_id: str | None = None) -> dict:
    _ensure_resource_path(item, project_id, context_id)
    return register_context(item)


@router.delete("/contexts/{project_id}/{context_id}")
def delete_context(project_id: str, context_id: str) -> dict:
    return unregister_context(project_id, context_id)


@router.get("/database-connectors", response_model=AdminListResponse)
def list_database_connectors(
    project_id: str | None = None,
    q: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> AdminListResponse:
    return _list_rows(
        DatabaseConnectorModel,
        project_id=project_id,
        q=q,
        limit=limit,
        offset=offset,
        q_columns=(DatabaseConnectorModel.connector_id, DatabaseConnectorModel.host),
        order_by=(DatabaseConnectorModel.project_id, DatabaseConnectorModel.connector_id),
    )


@router.get("/database-connectors/{project_id}/{connector_id}")
def get_database_connector(project_id: str, connector_id: str) -> dict:
    return _get_serialized(DatabaseConnectorModel, (project_id, connector_id), "数据库连接器")


@router.post("/database-connectors")
@router.put("/database-connectors/{project_id}/{connector_id}")
def save_database_connector(
    payload: DatabaseConnectorAdminPayload,
    project_id: str | None = None,
    connector_id: str | None = None,
) -> dict:
    _ensure_resource_path(payload, project_id, connector_id)
    password = payload.password or ""
    if not password:
        password = _existing_value(DatabaseConnectorModel, (payload.project_id, payload.id), "password")
        if password is None:
            raise HTTPException(status_code=400, detail="新建数据库连接器必须填写 password")
    item = DatabaseConnectorItem(**payload.model_dump(exclude={"password"}), password=password)
    return register_database_connector(item)


@router.delete("/database-connectors/{project_id}/{connector_id}")
def delete_database_connector(project_id: str, connector_id: str) -> dict:
    return unregister_database_connector(project_id, connector_id)


@router.get("/log-connectors", response_model=AdminListResponse)
def list_log_connectors(
    project_id: str | None = None,
    q: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> AdminListResponse:
    return _list_rows(
        LogConnectorModel,
        project_id=project_id,
        q=q,
        limit=limit,
        offset=offset,
        q_columns=(
            LogConnectorModel.connector_id,
            LogConnectorModel.display_name,
            LogConnectorModel.sls_project,
            LogConnectorModel.logstore,
        ),
        order_by=(LogConnectorModel.project_id, LogConnectorModel.connector_id),
    )


@router.get("/log-connectors/{project_id}/{connector_id}")
def get_log_connector(project_id: str, connector_id: str) -> dict:
    return _get_serialized(LogConnectorModel, (project_id, connector_id), "Log Connector")


@router.post("/log-connectors")
@router.put("/log-connectors/{project_id}/{connector_id}")
def save_log_connector(
    item: LogConnectorAdminPayload,
    project_id: str | None = None,
    connector_id: str | None = None,
) -> dict:
    _ensure_resource_path(item, project_id, connector_id)
    return register_log_connector(LogConnectorItem(**item.model_dump()))


@router.delete("/log-connectors/{project_id}/{connector_id}")
def delete_log_connector(project_id: str, connector_id: str) -> dict:
    return unregister_log_connector(project_id, connector_id)


@router.get("/external-connectors", response_model=AdminListResponse)
def list_external_connectors(
    project_id: str | None = None,
    connector_type: str | None = None,
    q: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> AdminListResponse:
    return _list_rows(
        ExternalConnectorModel,
        project_id=project_id,
        q=q,
        limit=limit,
        offset=offset,
        q_columns=(
            ExternalConnectorModel.connector_id,
            ExternalConnectorModel.connector_type,
            ExternalConnectorModel.display_name,
            ExternalConnectorModel.description,
        ),
        extra_filters=(ExternalConnectorModel.connector_type == connector_type if connector_type else None,),
        order_by=(
            ExternalConnectorModel.project_id,
            ExternalConnectorModel.connector_type,
            ExternalConnectorModel.connector_id,
        ),
    )


@router.get("/external-connectors/{project_id}/{connector_id}")
def get_external_connector(project_id: str, connector_id: str) -> dict:
    return _get_serialized(ExternalConnectorModel, (project_id, connector_id), "External Connector")


@router.post("/external-connectors/import-openapi")
def import_openapi_connector(req: OpenAPIImportRequest) -> dict:
    """从 FastAPI docs / Swagger / OpenAPI 地址一键导入 HTTP Service Connector。"""
    item, preview = _openapi_import_payload(req)
    result = register_external_connector(item)
    return {
        "ok": True,
        "message": result.get("message") or f"HTTP Service Connector '{item.id}' 导入成功",
        **preview,
    }


@router.post("/external-connectors")
@router.put("/external-connectors/{project_id}/{connector_id}")
def save_external_connector(
    payload: ExternalConnectorAdminPayload,
    project_id: str | None = None,
    connector_id: str | None = None,
) -> dict:
    _ensure_resource_path(payload, project_id, connector_id)
    secrets = payload.secrets
    if secrets is None or secrets == {}:
        existing = _existing_value(ExternalConnectorModel, (payload.project_id, payload.id), "secrets")
        secrets = dict(existing or {}) if existing is not None else secrets or {}
    item = ExternalConnectorItem(**payload.model_dump(exclude={"secrets"}), secrets=secrets or {})
    return register_external_connector(item)


@router.delete("/external-connectors/{project_id}/{connector_id}")
def delete_external_connector(project_id: str, connector_id: str) -> dict:
    return unregister_external_connector(project_id, connector_id)


@router.get("/runtime-contexts", response_model=AdminListResponse)
def list_runtime_contexts(
    project_id: str | None = None,
    environment: str | None = None,
    cluster: str | None = None,
    q: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> AdminListResponse:
    return _list_rows(
        RuntimeContextModel,
        project_id=project_id,
        q=q,
        limit=limit,
        offset=offset,
        q_columns=(
            RuntimeContextModel.runtime_id,
            RuntimeContextModel.app_name,
            RuntimeContextModel.workload_name,
            RuntimeContextModel.service_name,
            RuntimeContextModel.source_path,
        ),
        extra_filters=(
            RuntimeContextModel.environment == environment if environment else None,
            RuntimeContextModel.clusters.contains([cluster]) if cluster else None,
        ),
        order_by=(
            RuntimeContextModel.project_id,
            RuntimeContextModel.environment,
            RuntimeContextModel.workload_name,
        ),
    )


@router.get("/runtime-contexts/{project_id}/{runtime_id}")
def get_runtime_context(project_id: str, runtime_id: str) -> dict:
    return _get_serialized(RuntimeContextModel, (project_id, runtime_id), "Runtime Context")


@router.post("/runtime-contexts")
@router.put("/runtime-contexts/{project_id}/{runtime_id}")
def save_runtime_context(
    payload: RuntimeContextAdminPayload,
    project_id: str | None = None,
    runtime_id: str | None = None,
) -> dict:
    _ensure_resource_path(payload, project_id, runtime_id)
    return register_runtime_context(RuntimeContextItem(**payload.model_dump()))


@router.delete("/runtime-contexts/{project_id}/{runtime_id}")
def delete_runtime_context(project_id: str, runtime_id: str) -> dict:
    return unregister_runtime_context(project_id, runtime_id)


@router.get("/skills", response_model=AdminListResponse)
def list_skills(
    project_id: str | None = None,
    kind: str | None = None,
    status: str | None = None,
    q: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> AdminListResponse:
    return _list_rows(
        SkillModel,
        project_id=project_id,
        q=q,
        limit=limit,
        offset=offset,
        q_columns=(SkillModel.skill_id, SkillModel.name, SkillModel.description, SkillModel.raw_content),
        extra_filters=(
            SkillModel.kind == kind if kind else None,
            SkillModel.status == status if status else None,
        ),
        order_by=(SkillModel.project_id, SkillModel.kind, SkillModel.name),
    )


@router.get("/skills/tools")
def list_skill_tools() -> dict:
    return {"tools": KNOWN_TOOLS}


@router.get("/skills/{project_id}/{skill_id}")
def get_skill(project_id: str, skill_id: str) -> dict:
    return _get_serialized(SkillModel, (project_id, skill_id), "Skill")


@router.post("/skills/draft")
async def draft_skill(payload: SkillDraftRequest) -> dict:
    try:
        item = await draft_skill_from_text(payload.project_id, payload.raw_text)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"skill": item.model_dump(), "tools": KNOWN_TOOLS}


@router.post("/skills")
@router.put("/skills/{project_id}/{skill_id}")
def save_skill(item: SkillItem, project_id: str | None = None, skill_id: str | None = None) -> dict:
    _ensure_resource_path(item, project_id, skill_id)
    return register_skill(item)


@router.delete("/skills/{project_id}/{skill_id}")
def delete_skill(project_id: str, skill_id: str) -> dict:
    return unregister_skill(project_id, skill_id)


@router.get("/group-bindings", response_model=AdminListResponse)
def list_group_bindings(project_id: str | None = None, q: str | None = None, limit: int = 100, offset: int = 0) -> AdminListResponse:
    return _list_rows(
        GroupBindingModel,
        project_id=project_id,
        q=q,
        limit=limit,
        offset=offset,
        q_columns=(GroupBindingModel.conversation_id, GroupBindingModel.group_name),
        order_by=(GroupBindingModel.project_id, GroupBindingModel.conversation_id),
    )


@router.post("/group-bindings")
@router.put("/group-bindings/{conversation_id}")
def save_group_binding(item: GroupBinding, conversation_id: str | None = None) -> dict:
    cid = normalize_conversation_id(item.conversation_id)
    if conversation_id and normalize_conversation_id(conversation_id) != cid:
        raise HTTPException(status_code=400, detail="路径 conversation_id 与请求体不一致")
    return bind_group(GroupBinding(conversation_id=cid, project_id=item.project_id, group_name=item.group_name))


@router.delete("/group-bindings/{conversation_id}")
def delete_group_binding(conversation_id: str) -> dict:
    return unbind_group(conversation_id)


@router.get("/glossaries", response_model=AdminListResponse)
def list_glossaries(project_id: str | None = None, q: str | None = None, limit: int = 100, offset: int = 0) -> AdminListResponse:
    return _list_rows(
        GlossaryModel,
        project_id=project_id,
        q=q,
        limit=limit,
        offset=offset,
        q_columns=(GlossaryModel.glossary_id, GlossaryModel.term),
        order_by=(GlossaryModel.project_id, GlossaryModel.glossary_id),
    )


@router.post("/glossaries")
@router.put("/glossaries/{project_id}/{glossary_id}")
def save_glossary(item: GlossaryItem, project_id: str | None = None, glossary_id: str | None = None) -> dict:
    _ensure_resource_path(item, project_id, glossary_id)
    return register_glossary(item)


@router.delete("/glossaries/{project_id}/{glossary_id}")
def delete_glossary(project_id: str, glossary_id: str) -> dict:
    return unregister_glossary(project_id, glossary_id)


@router.get("/knowledge-notes", response_model=AdminListResponse)
def list_knowledge_notes(project_id: str | None = None, q: str | None = None, limit: int = 100, offset: int = 0) -> AdminListResponse:
    return _list_rows(
        KnowledgeNoteModel,
        project_id=project_id,
        q=q,
        limit=limit,
        offset=offset,
        q_columns=(KnowledgeNoteModel.note_id, KnowledgeNoteModel.title, KnowledgeNoteModel.scope),
        order_by=(KnowledgeNoteModel.project_id, KnowledgeNoteModel.kind, KnowledgeNoteModel.note_id),
    )


@router.post("/knowledge-notes")
@router.put("/knowledge-notes/{project_id}/{note_id}")
def save_knowledge_note(item: KnowledgeNoteItem, project_id: str | None = None, note_id: str | None = None) -> dict:
    _ensure_resource_path(item, project_id, note_id)
    return register_knowledge_note(item)


@router.delete("/knowledge-notes/{project_id}/{note_id}")
def delete_knowledge_note(project_id: str, note_id: str) -> dict:
    return unregister_knowledge_note(project_id, note_id)


@router.get("/gitlab-tasks", response_model=AdminListResponse)
def list_gitlab_tasks(project_id: str | None = None, q: str | None = None, limit: int = 100, offset: int = 0) -> AdminListResponse:
    return _list_rows(
        GitLabTaskModel,
        project_id=project_id,
        q=q,
        limit=limit,
        offset=offset,
        q_columns=(GitLabTaskModel.task_id, GitLabTaskModel.repo_url, GitLabTaskModel.status),
        order_by=(GitLabTaskModel.created_at.desc(),),
    )


@router.get("/onboarding-tasks", response_model=AdminListResponse)
def list_onboarding_tasks(project_id: str | None = None, q: str | None = None, limit: int = 100, offset: int = 0) -> AdminListResponse:
    return _list_rows(
        OnboardingTaskModel,
        project_id=project_id,
        q=q,
        limit=limit,
        offset=offset,
        q_columns=(OnboardingTaskModel.task_id, OnboardingTaskModel.repo_url, OnboardingTaskModel.status),
        order_by=(OnboardingTaskModel.created_at.desc(),),
    )


@router.get("/onboarding-artifacts", response_model=AdminListResponse)
def list_onboarding_artifacts(project_id: str | None = None, q: str | None = None, limit: int = 100, offset: int = 0) -> AdminListResponse:
    return _list_rows(
        OnboardingArtifactModel,
        project_id=project_id,
        q=q,
        limit=limit,
        offset=offset,
        q_columns=(OnboardingArtifactModel.artifact_id, OnboardingArtifactModel.target_id, OnboardingArtifactModel.title),
        order_by=(OnboardingArtifactModel.created_at.desc(),),
    )


@router.get("/reports", response_model=AdminListResponse)
def list_reports(project_id: str | None = None, q: str | None = None, limit: int = 100, offset: int = 0) -> AdminListResponse:
    return _list_rows(
        ReportModel,
        project_id=project_id,
        q=q,
        limit=limit,
        offset=offset,
        q_columns=(ReportModel.id, ReportModel.title, ReportModel.summary),
        order_by=(ReportModel.created_at.desc(),),
    )


@router.get("/reports/{report_id}")
def get_report_json(report_id: str) -> dict:
    return _get_serialized(ReportModel, report_id, "报告")


@router.get("/llm/providers")
def get_llm_providers() -> dict:
    return {"items": provider_health()}


@router.get("/llm/summary")
def get_llm_summary(
    window_minutes: int = Query(default=60, ge=1, le=24 * 60),
    scene: str | None = None,
) -> dict:
    return llm_summary(window_minutes=window_minutes, scene=scene)


@router.get("/llm/usage/timeseries")
def get_llm_usage_timeseries(
    window_minutes: int = Query(default=1440, ge=1, le=7 * 24 * 60),
    buckets: int = Query(default=48, ge=1, le=500),
    scene: str | None = None,
) -> dict:
    return llm_usage_timeseries(window_minutes=window_minutes, buckets=buckets, scene=scene)


@router.get("/llm/calls")
def get_llm_calls(
    provider: str | None = None,
    feature: str | None = None,
    scene: str | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> dict:
    return list_llm_calls(limit=limit, offset=offset, provider=provider, feature=feature, scene=scene)


@router.get("/agent-traces")
def get_agent_traces(
    project_id: str | None = None,
    session_id: str | None = None,
    topic_thread_id: str | None = None,
    event_type: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> dict:
    return list_traces(
        project_id=project_id,
        session_id=session_id,
        topic_thread_id=topic_thread_id,
        event_type=event_type,
        start_time=_parse_dt_param(start_time, "start_time"),
        end_time=_parse_dt_param(end_time, "end_time"),
        limit=limit,
        offset=offset,
    )


@router.get("/agent-traces/{trace_id}/events")
def get_agent_trace_events(
    trace_id: str,
    event_type: str | None = None,
    limit: int = Query(default=1000, ge=1, le=2000),
    offset: int = Query(default=0, ge=0),
) -> dict:
    return get_trace_events(trace_id, event_type=event_type, limit=limit, offset=offset)


@router.get("/trace-evaluations")
def get_trace_evaluations(
    project_id: str | None = None,
    trace_id: str | None = None,
    status: str | None = None,
    q: str | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> dict:
    return list_trace_evaluations(
        project_id=project_id,
        trace_id=trace_id,
        status=status,
        q=q,
        limit=limit,
        offset=offset,
    )


@router.get("/agent-traces/{trace_id}/evaluations")
def get_agent_trace_evaluations(
    trace_id: str,
    status: str | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> dict:
    return list_trace_evaluations(trace_id=trace_id, status=status, limit=limit, offset=offset)


@router.post("/agent-traces/{trace_id}/evaluations")
def create_agent_trace_evaluation(trace_id: str) -> dict:
    return queue_trace_evaluation(trace_id, force=True)


@router.get("/learning-candidates")
def get_learning_candidates(
    project_id: str | None = None,
    status: str | None = "pending",
    target_type: str | None = None,
    source_trace_id: str | None = None,
    q: str | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> dict:
    return list_learning_candidates(
        project_id=project_id,
        status=status,
        target_type=target_type,
        source_trace_id=source_trace_id,
        q=q,
        limit=limit,
        offset=offset,
    )


@router.patch("/learning-candidates/{candidate_id}")
def patch_learning_candidate(candidate_id: str, patch: LearningCandidatePatch) -> dict:
    payload = patch.model_dump(exclude_none=True)
    try:
        return update_learning_candidate(candidate_id, payload)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/learning-candidates/{candidate_id}/apply")
def apply_learning_candidate_api(candidate_id: str) -> dict:
    try:
        return apply_learning_candidate(candidate_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/agent-traces/{trace_id}/learning-candidates")
def rerun_agent_trace_learning(trace_id: str) -> dict:
    return run_trace_learning(trace_id)


@router.get("/llm/usage/coding-tasks")
def get_coding_task_token_usage(
    project_id: str | None = None,
    days: int | None = Query(default=None, ge=1, le=366),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict:
    return list_coding_task_token_usage(project_id=project_id, days=days, limit=limit, offset=offset)


@router.get("/llm/usage/chats")
def get_chat_token_usage(
    project_id: str | None = None,
    days: int | None = Query(default=None, ge=1, le=366),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict:
    return list_chat_token_usage(project_id=project_id, days=days, limit=limit, offset=offset)


@router.get("/chat/messages", response_model=AdminListResponse)
def list_chat_messages(
    topic_thread_id: str | None = None,
    thread_id: str | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> AdminListResponse:
    tid = (topic_thread_id or thread_id or "").strip()
    if not tid:
        raise HTTPException(status_code=400, detail="topic_thread_id 不能为空")
    return _list_rows(
        ChatMessageModel,
        limit=limit,
        offset=offset,
        extra_filters=(ChatMessageModel.topic_thread_id == tid,),
        order_by=(ChatMessageModel.created_at, ChatMessageModel.id),
    )
