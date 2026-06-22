"""Shared persistence helpers for registry-backed items."""
from __future__ import annotations

from typing import Any

from core.models import (
    DatabaseConnectorModel,
    ExternalConnectorModel,
    GlossaryModel,
    KnowledgeNoteModel,
    LogConnectorModel,
    RepositoryConnectorModel,
    RuntimeContextModel,
    SkillModel,
)
from core.registry import (
    DatabaseConnectorItem,
    ExternalConnectorItem,
    GlossaryItem,
    KnowledgeNoteItem,
    LogConnectorItem,
    RepositoryConnectorItem,
    RuntimeContextItem,
    SSHTunnelSpec,
    SkillItem,
)


def _apply(row: Any, values: dict[str, Any]) -> None:
    for key, value in values.items():
        setattr(row, key, value)


def _upsert(db: Any, model_cls: type, key: Any, values: dict[str, Any]) -> None:
    row = db.get(model_cls, key)
    if row:
        _apply(row, values)
    else:
        db.add(model_cls(**values))


def _ssh_tunnel_to_json(item: DatabaseConnectorItem) -> dict[str, Any] | None:
    if item.ssh_tunnel is None:
        return None
    return item.ssh_tunnel.model_dump(exclude_none=True)


def database_connector_values(item: DatabaseConnectorItem) -> dict[str, Any]:
    return {
        "project_id": item.project_id,
        "connector_id": item.id,
        "type": item.type,
        "host": item.host,
        "port": item.port,
        "username": item.username,
        "password": item.password,
        "database_name": item.database,
        "readonly_flag": 1 if item.readonly else 0,
        "charset_name": item.charset,
        "ssh_tunnel": _ssh_tunnel_to_json(item),
    }


def upsert_database_connector_model(db: Any, item: DatabaseConnectorItem) -> None:
    _upsert(db, DatabaseConnectorModel, (item.project_id, item.id), database_connector_values(item))


def database_connector_item_from_model(row: DatabaseConnectorModel) -> DatabaseConnectorItem:
    tunnel_raw = getattr(row, "ssh_tunnel", None)
    tunnel_spec = SSHTunnelSpec(**tunnel_raw) if isinstance(tunnel_raw, dict) else None
    return DatabaseConnectorItem(
        id=row.connector_id,
        project_id=row.project_id,
        type=row.type or "mysql",
        host=row.host,
        port=row.port,
        username=row.username,
        password=row.password,
        database=row.database_name,
        readonly=bool(row.readonly_flag),
        charset=row.charset_name or "utf8mb4",
        ssh_tunnel=tunnel_spec,
    )


def _workload_to_json(item: RepositoryConnectorItem) -> dict[str, Any] | None:
    if item.k8s_workload is None:
        return None
    return item.k8s_workload.model_dump(exclude_none=True)


def repository_connector_values(item: RepositoryConnectorItem) -> dict[str, Any]:
    return {
        "project_id": item.project_id,
        "connector_id": item.id,
        "display_name": item.display_name,
        "description": getattr(item, "description", "") or "",
        "git_url": item.git_url,
        "default_branch": item.default_branch,
        "k8s_workload": _workload_to_json(item),
        "sort_order": item.sort_order,
        "build_venv": 1 if item.build_venv else 0,
        "language": getattr(item, "language", "") or "",
        "test_command": getattr(item, "test_command", "") or "",
        "lint_command": getattr(item, "lint_command", "") or "",
        "maintainer_mobile": getattr(item, "maintainer_mobile", "") or "",
    }


def upsert_repository_connector_model(db: Any, item: RepositoryConnectorItem) -> None:
    _upsert(db, RepositoryConnectorModel, (item.project_id, item.id), repository_connector_values(item))


def log_connector_values(item: LogConnectorItem) -> dict[str, Any]:
    return {
        "project_id": item.project_id,
        "connector_id": item.id,
        "display_name": item.display_name,
        "sls_project": item.sls_project,
        "logstore": item.logstore,
        "description": item.description,
        "enabled": 1 if item.enabled else 0,
    }


def upsert_log_connector_model(db: Any, item: LogConnectorItem) -> None:
    _upsert(db, LogConnectorModel, (item.project_id, item.id), log_connector_values(item))


def log_connector_item_from_model(row: LogConnectorModel) -> LogConnectorItem:
    return LogConnectorItem(
        id=row.connector_id,
        project_id=row.project_id,
        display_name=row.display_name or "",
        sls_project=row.sls_project,
        logstore=row.logstore,
        description=row.description or "",
        enabled=bool(row.enabled),
    )


def external_connector_values(item: ExternalConnectorItem) -> dict[str, Any]:
    return {
        "project_id": item.project_id,
        "connector_id": item.id,
        "connector_type": item.connector_type,
        "display_name": item.display_name,
        "description": item.description,
        "config": dict(item.config),
        "secrets": dict(item.secrets),
        "enabled": 1 if item.enabled else 0,
    }


def upsert_external_connector_model(db: Any, item: ExternalConnectorItem) -> None:
    _upsert(db, ExternalConnectorModel, (item.project_id, item.id), external_connector_values(item))


def external_connector_item_from_model(row: ExternalConnectorModel) -> ExternalConnectorItem:
    return ExternalConnectorItem(
        id=row.connector_id,
        project_id=row.project_id,
        connector_type=row.connector_type,
        display_name=row.display_name or "",
        description=row.description or "",
        config=dict(row.config or {}),
        secrets=dict(row.secrets or {}),
        enabled=bool(row.enabled),
    )


def runtime_context_values(item: RuntimeContextItem) -> dict[str, Any]:
    payload = item.model_dump()
    return {
        "project_id": item.project_id,
        "runtime_id": item.id,
        "environment": item.environment,
        "source_type": item.source_type,
        "source_repo": item.source_repo,
        "source_path": item.source_path,
        "app_name": item.app_name,
        "namespace": item.namespace,
        "workload_type": item.workload_type,
        "workload_name": item.workload_name,
        "service_name": item.service_name,
        "clusters": payload["clusters"],
        "selector": payload["selector"],
        "labels": payload["labels"],
        "replicas": item.replicas,
        "image": item.image,
        "command": payload["command"],
        "ports": payload["ports"],
        "resources": payload["resources"],
        "probes": payload["probes"],
        "log_bindings": payload["log_bindings"],
        "exposures": payload["exposures"],
        "scheduling": payload["scheduling"],
        "config": payload["config"],
        "enabled": 1 if item.enabled else 0,
    }


def upsert_runtime_context_model(db: Any, item: RuntimeContextItem) -> None:
    _upsert(db, RuntimeContextModel, (item.project_id, item.id), runtime_context_values(item))


def runtime_context_item_from_model(row: RuntimeContextModel) -> RuntimeContextItem:
    return RuntimeContextItem(
        id=row.runtime_id,
        project_id=row.project_id,
        environment=row.environment or "prod",
        source_type=row.source_type or "kubevela",
        source_repo=row.source_repo or "",
        source_path=row.source_path or "",
        app_name=row.app_name or "",
        namespace=row.namespace or "",
        workload_type=row.workload_type or "Deployment",
        workload_name=row.workload_name or "",
        service_name=row.service_name or "",
        clusters=list(row.clusters or []),
        selector=dict(row.selector or {}),
        labels=dict(row.labels or {}),
        replicas=row.replicas,
        image=row.image or "",
        command=list(row.command or []),
        ports=list(row.ports or []),
        resources=dict(row.resources or {}),
        probes=dict(row.probes or {}),
        log_bindings=list(row.log_bindings or []),
        exposures=list(row.exposures or []),
        scheduling=dict(row.scheduling or {}),
        config=dict(row.config or {}),
        enabled=bool(row.enabled),
    )


def skill_values(item: SkillItem) -> dict[str, Any]:
    payload = item.model_dump()
    return {
        "project_id": item.project_id,
        "skill_id": item.id,
        "name": item.name,
        "kind": item.kind,
        "description": item.description,
        "trigger_examples": payload["trigger_examples"],
        "input_contract": payload["input_contract"],
        "required_contexts": payload["required_contexts"],
        "required_tools": payload["required_tools"],
        "related_glossary_terms": payload["related_glossary_terms"],
        "instructions": payload["instructions"],
        "output_contract": payload["output_contract"],
        "safety_policy": payload["safety_policy"],
        "source_type": item.source_type,
        "source_uri": item.source_uri,
        "raw_content": item.raw_content,
        "status": item.status,
        "version": item.version,
        "scope": item.scope,
    }


def upsert_skill_model(db: Any, item: SkillItem) -> None:
    _upsert(db, SkillModel, (item.project_id, item.id), skill_values(item))


def skill_item_from_model(row: SkillModel) -> SkillItem:
    return SkillItem(
        id=row.skill_id,
        project_id=row.project_id,
        name=row.name or "",
        kind=row.kind or "business",
        scope=row.scope or "project",
        description=row.description or "",
        trigger_examples=list(row.trigger_examples or []),
        input_contract=dict(row.input_contract or {}),
        required_contexts=list(row.required_contexts or []),
        required_tools=list(row.required_tools or []),
        related_glossary_terms=list(row.related_glossary_terms or []),
        instructions=list(row.instructions or []),
        output_contract=dict(row.output_contract or {}),
        safety_policy=dict(row.safety_policy or {}),
        source_type=row.source_type or "manual",
        source_uri=row.source_uri or "",
        raw_content=row.raw_content or "",
        status=row.status or "enabled",
        version=row.version or 1,
    )


def glossary_values(item: GlossaryItem) -> dict[str, Any]:
    return {
        "project_id": item.project_id,
        "glossary_id": item.id,
        "term": item.term,
        "aliases": list(item.aliases),
        "code_keywords": list(item.code_keywords),
        "description": item.description,
        "enabled": 1 if item.enabled else 0,
    }


def upsert_glossary_model(db: Any, item: GlossaryItem) -> None:
    _upsert(db, GlossaryModel, (item.project_id, item.id), glossary_values(item))


def glossary_item_from_model(row: GlossaryModel) -> GlossaryItem:
    return GlossaryItem(
        id=row.glossary_id,
        project_id=row.project_id,
        term=row.term,
        aliases=list(row.aliases or []),
        code_keywords=list(row.code_keywords or []),
        description=row.description or "",
        enabled=bool(row.enabled),
    )


def knowledge_note_values(item: KnowledgeNoteItem) -> dict[str, Any]:
    return {
        "project_id": item.project_id,
        "note_id": item.id,
        "kind": item.kind,
        "scope": item.scope,
        "title": item.title,
        "content": item.content,
        "tags": list(item.tags),
        "enabled": 1 if item.enabled else 0,
        "source": item.source,
    }


def upsert_knowledge_note_model(db: Any, item: KnowledgeNoteItem) -> None:
    _upsert(db, KnowledgeNoteModel, (item.project_id, item.id), knowledge_note_values(item))


def knowledge_note_item_from_model(row: KnowledgeNoteModel) -> KnowledgeNoteItem:
    return KnowledgeNoteItem(
        id=row.note_id,
        project_id=row.project_id,
        kind=row.kind,
        scope=row.scope or "",
        title=row.title or "",
        content=row.content or "",
        tags=list(row.tags or []),
        enabled=bool(row.enabled),
        source=row.source or "api",
    )
