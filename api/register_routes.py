"""
注册 API 路由。

业务方通过这些接口动态注册项目、上下文、数据库连接器和钉钉群绑定。
所有注册项按项目隔离，Viktor 本身不硬编码任何业务知识。
注册操作同时写入内存 Registry 和 MySQL 持久化层。
"""
from fastapi import APIRouter, HTTPException
from loguru import logger

from typing import Any, Optional

from pydantic import Field

from core.database import SessionLocal
from core.repo_warmup import warmup_project_async, warmup_repo_async
from core.models import (
    ProjectModel,
    ContextModel,
    DatabaseConnectorModel,
    LogConnectorModel,
    ExternalConnectorModel,
    RuntimeContextModel,
    SkillModel,
    GroupBindingModel,
    GlossaryModel,
    KnowledgeNoteModel,
    OnboardingArtifactModel,
    OnboardingTaskModel,
)
from core.registry import (
    ProjectItem,
    RepositoryConnectorItem,
    ContextItem,
    DatabaseConnectorItem,
    LogConnectorItem,
    ExternalConnectorItem,
    RuntimeContextItem,
    SkillItem,
    GroupBinding,
    GlossaryItem,
    KnowledgeNoteItem,
    normalize_conversation_id,
    registry,
)
from core.registry_persistence import (
    upsert_database_connector_model,
    upsert_external_connector_model,
    upsert_glossary_model,
    upsert_knowledge_note_model,
    upsert_log_connector_model,
    upsert_repository_connector_model,
    upsert_runtime_context_model,
    upsert_skill_model,
)
from core.onboarding_service import (
    _normalize_repository_specs,
    _repository_connector_item_from_spec,
)

router = APIRouter(prefix="/api/v1/register", tags=["注册"])


def _db():
    return SessionLocal()


class ProjectRegistrationItem(ProjectItem):
    """项目注册请求体。

    ProjectItem 只保存项目自身字段；这里额外兼容前端一次提交多个仓库。
    """

    repo_url: Any = ""
    repo_urls: list[Any] = Field(default_factory=list)
    repositories: list[Any] = Field(default_factory=list)
    repos: list[Any] = Field(default_factory=list)


def _repository_specs_from_project_registration(item: ProjectRegistrationItem) -> list[dict[str, Any]]:
    repo_url = (item.git_url or "").strip()
    raw_repo_url = getattr(item, "repo_url", "")
    repo_urls = list(getattr(item, "repo_urls", []) or [])
    repositories = list(getattr(item, "repositories", []) or [])
    repos = list(getattr(item, "repos", []) or [])
    if isinstance(raw_repo_url, str) and raw_repo_url.strip() and not repo_url:
        repo_url = raw_repo_url.strip()
    elif isinstance(raw_repo_url, list):
        repo_urls.extend(raw_repo_url)
    elif isinstance(raw_repo_url, dict):
        repositories.insert(0, raw_repo_url)

    has_repo_inputs = bool(repo_url or repo_urls or repositories or repos)
    if not has_repo_inputs:
        return []
    return _normalize_repository_specs(
        project_id=item.id,
        repo_url=repo_url,
        branch=item.default_branch,
        repo_urls=repo_urls,
        repositories=repositories,
        repos=repos,
    )


# ============================================================
# 项目注册
# ============================================================

@router.post("/project", summary="注册项目")
def register_project(item: ProjectRegistrationItem) -> dict:
    """注册或更新一个项目。所有其他注册项都必须归属于某个已注册的项目。"""
    try:
        repository_specs = _repository_specs_from_project_registration(item)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if repository_specs and not item.git_url:
        primary_repo = repository_specs[0]
        item = item.model_copy(update={
            "git_url": primary_repo["repo_url"],
            "default_branch": primary_repo["branch"],
        })
    registry.register_project(item)
    repository_items = [
        _repository_connector_item_from_spec(item.id, spec)
        for spec in repository_specs
    ]
    db = _db()
    try:
        workload_dict = item.k8s_workload.model_dump(exclude_none=True) if item.k8s_workload else None
        row = db.get(ProjectModel, item.id)
        if row:
            row.name = item.name
            row.description = item.description
            row.git_url = item.git_url
            row.default_branch = item.default_branch
            row.k8s_workload = workload_dict
        else:
            db.add(ProjectModel(
                project_id=item.id, name=item.name, description=item.description,
                git_url=item.git_url,
                default_branch=item.default_branch,
                k8s_workload=workload_dict,
            ))
        for repo in repository_items:
            registry.register_repository_connector(repo)
            upsert_repository_connector_model(db, repo)
        db.commit()
    finally:
        db.close()
    # 后台增量预热仓库（clone + venv），新项目免重启即可用脚本能力
    if repository_items:
        for repo in repository_items:
            warmup_repo_async(item.id, repo.id)
    else:
        warmup_project_async(item.id)
    return {
        "ok": True,
        "message": f"项目 '{item.id}' 注册成功",
        "repository_connectors": [repo.model_dump() for repo in repository_items],
    }


@router.delete("/project/{project_id}", summary="注销项目")
def unregister_project(project_id: str) -> dict:
    """注销项目及其所有关联的上下文、数据库连接器、群绑定等。"""
    if not registry.unregister_project(project_id):
        raise HTTPException(status_code=404, detail=f"项目 '{project_id}' 不存在")
    db = _db()
    try:
        from core.models import RepositoryConnectorModel
        db.query(RepositoryConnectorModel).filter(RepositoryConnectorModel.project_id == project_id).delete()
        db.query(GroupBindingModel).filter(GroupBindingModel.project_id == project_id).delete()
        db.query(ContextModel).filter(ContextModel.project_id == project_id).delete()
        db.query(DatabaseConnectorModel).filter(DatabaseConnectorModel.project_id == project_id).delete()
        db.query(LogConnectorModel).filter(LogConnectorModel.project_id == project_id).delete()
        db.query(ExternalConnectorModel).filter(ExternalConnectorModel.project_id == project_id).delete()
        db.query(RuntimeContextModel).filter(RuntimeContextModel.project_id == project_id).delete()
        db.query(SkillModel).filter(SkillModel.project_id == project_id).delete()
        db.query(GlossaryModel).filter(GlossaryModel.project_id == project_id).delete()
        db.query(KnowledgeNoteModel).filter(KnowledgeNoteModel.project_id == project_id).delete()
        db.query(OnboardingArtifactModel).filter(OnboardingArtifactModel.project_id == project_id).delete()
        db.query(OnboardingTaskModel).filter(OnboardingTaskModel.project_id == project_id).delete()
        db.query(ProjectModel).filter(ProjectModel.project_id == project_id).delete()
        db.commit()
    finally:
        db.close()
    return {"ok": True, "message": f"项目 '{project_id}' 及其所有注册项已注销"}


# ============================================================
# Repository Connector 注册
# ============================================================

@router.post("/project/{project_id}/repository-connector", summary="注册 Repository Connector")
def register_repository_connector(project_id: str, item: RepositoryConnectorItem) -> dict:
    """注册或更新项目关联的 Git 仓库连接器。"""
    if item.project_id != project_id:
        raise HTTPException(status_code=400, detail="路径 project_id 与请求体不一致")
    try:
        registry.register_repository_connector(item)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db = _db()
    try:
        upsert_repository_connector_model(db, item)
        db.commit()
    finally:
        db.close()
    # 后台增量预热该仓库（clone + venv）
    warmup_repo_async(project_id, item.id)
    return {"ok": True, "message": f"Repository Connector '{item.id}' 注册成功（项目: {project_id}）"}


@router.delete("/project/{project_id}/repository-connector/{connector_id}", summary="注销 Repository Connector")
def unregister_repository_connector(project_id: str, connector_id: str) -> dict:
    if not registry.unregister_repository_connector(project_id, connector_id):
        raise HTTPException(status_code=404, detail=f"Repository Connector '{connector_id}' 不存在")
    db = _db()
    try:
        from core.models import RepositoryConnectorModel
        db.query(RepositoryConnectorModel).filter(
            RepositoryConnectorModel.project_id == project_id,
            RepositoryConnectorModel.connector_id == connector_id,
        ).delete()
        db.commit()
    finally:
        db.close()
    return {"ok": True, "message": f"Repository Connector '{connector_id}' 已注销"}


@router.get("/project/{project_id}/repository-connectors", summary="查询 Repository Connector 列表")
def list_repository_connectors(project_id: str) -> dict:
    repository_connectors = registry.get_repository_connectors(project_id)
    return {
        "project_id": project_id,
        "count": len(repository_connectors),
        "repository_connectors": [r.model_dump() for r in repository_connectors],
    }


# ============================================================
# 群绑定
# ============================================================

@router.post("/bindgroup", summary="绑定钉钉群到项目")
def bind_group(item: GroupBinding) -> dict:
    """将钉钉群 conversation_id 绑定到指定项目，消息路由依赖此映射。"""
    cid = normalize_conversation_id(item.conversation_id)
    try:
        registry.bind_group(cid, item.project_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db = _db()
    try:
        row = db.get(GroupBindingModel, cid)
        if row:
            row.project_id = item.project_id
            row.group_name = item.group_name or ""
        else:
            db.add(GroupBindingModel(
                conversation_id=cid,
                project_id=item.project_id,
                group_name=item.group_name or "",
            ))
        db.commit()
    finally:
        db.close()
    return {"ok": True, "message": f"群 '{cid}' 已绑定到项目 '{item.project_id}'"}


@router.delete("/bindgroup/{conversation_id}", summary="解绑钉钉群")
def unbind_group(conversation_id: str) -> dict:
    cid = normalize_conversation_id(conversation_id)
    if not registry.unbind_group(cid):
        raise HTTPException(status_code=404, detail=f"群 '{cid}' 未绑定")
    db = _db()
    try:
        db.query(GroupBindingModel).filter(
            GroupBindingModel.conversation_id == cid
        ).delete()
        db.commit()
    finally:
        db.close()
    return {"ok": True, "message": f"群 '{cid}' 已解绑"}


# ============================================================
# 上下文注册
# ============================================================

@router.post("/context", summary="注册业务上下文")
def register_context(item: ContextItem) -> dict:
    """注册或更新一个上下文片段，将在 Agent 对话时注入 system prompt。"""
    try:
        registry.register_context(item)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db = _db()
    try:
        row = db.get(ContextModel, (item.project_id, item.id))
        if row:
            row.priority = item.priority
            row.content = item.content
        else:
            db.add(ContextModel(
                project_id=item.project_id, context_id=item.id,
                priority=item.priority, content=item.content,
            ))
        db.commit()
    finally:
        db.close()
    return {"ok": True, "message": f"上下文 '{item.id}' 注册成功（项目: {item.project_id}）"}


@router.delete("/context/{project_id}/{context_id}", summary="注销业务上下文")
def unregister_context(project_id: str, context_id: str) -> dict:
    if not registry.unregister_context(project_id, context_id):
        raise HTTPException(status_code=404, detail=f"上下文 '{context_id}' 不存在")
    db = _db()
    try:
        db.query(ContextModel).filter(
            ContextModel.project_id == project_id,
            ContextModel.context_id == context_id,
        ).delete()
        db.commit()
    finally:
        db.close()
    return {"ok": True, "message": f"上下文 '{context_id}' 已注销"}


# ============================================================
# 数据库连接器注册
# ============================================================

@router.post("/database-connector", summary="注册 Database Connector")
def register_database_connector(item: DatabaseConnectorItem) -> dict:
    """注册或更新一个数据库连接。"""
    try:
        registry.register_database_connector(item)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("注册数据库连接器失败, error: {}", e)
        raise HTTPException(status_code=400, detail=str(e))
    db = _db()
    try:
        upsert_database_connector_model(db, item)
        db.commit()
    finally:
        db.close()
    return {"ok": True, "message": f"数据库连接器 '{item.id}' 注册成功（项目: {item.project_id}）"}


@router.delete("/database-connector/{project_id}/{connector_id}", summary="注销 Database Connector")
def unregister_database_connector(project_id: str, connector_id: str) -> dict:
    if not registry.unregister_database_connector(project_id, connector_id):
        raise HTTPException(
            status_code=404, detail=f"数据库连接器 '{connector_id}' 不存在"
        )
    db = _db()
    try:
        db.query(DatabaseConnectorModel).filter(
            DatabaseConnectorModel.project_id == project_id,
            DatabaseConnectorModel.connector_id == connector_id,
        ).delete()
        db.commit()
    finally:
        db.close()
    return {"ok": True, "message": f"数据库连接器 '{connector_id}' 已注销"}


# ============================================================
# Log Connector 注册
# ============================================================

@router.post("/log-connector", summary="注册 Log Connector")
def register_log_connector(item: LogConnectorItem) -> dict:
    """注册或更新项目级 Log Connector，只存业务侧 project/logstore，不存 AK/SK。"""
    try:
        registry.register_log_connector(item)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db = _db()
    try:
        upsert_log_connector_model(db, item)
        db.commit()
    finally:
        db.close()
    return {"ok": True, "message": f"Log Connector '{item.id}' 注册成功（项目: {item.project_id}）"}


@router.get("/log-connector/{project_id}", summary="导出项目 Log Connector")
def list_log_connectors(project_id: str, only_enabled: bool = False) -> dict:
    items = registry.get_log_connectors(project_id, only_enabled=only_enabled)
    return {
        "project_id": project_id,
        "count": len(items),
        "log_connectors": [item.model_dump() for item in items],
    }


@router.delete("/log-connector/{project_id}/{connector_id}", summary="注销 Log Connector")
def unregister_log_connector(project_id: str, connector_id: str) -> dict:
    if not registry.unregister_log_connector(project_id, connector_id):
        raise HTTPException(status_code=404, detail=f"Log Connector '{connector_id}' 不存在")
    db = _db()
    try:
        db.query(LogConnectorModel).filter(
            LogConnectorModel.project_id == project_id,
            LogConnectorModel.connector_id == connector_id,
        ).delete()
        db.commit()
    finally:
        db.close()
    return {"ok": True, "message": f"Log Connector '{connector_id}' 已注销"}


# ============================================================
# External Connector 注册
# ============================================================

@router.post("/external-connector", summary="注册 External Connector")
def register_external_connector(item: ExternalConnectorItem) -> dict:
    """注册或更新项目级外部证据连接器。"""
    try:
        registry.register_external_connector(item)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db = _db()
    try:
        upsert_external_connector_model(db, item)
        db.commit()
    finally:
        db.close()
    return {"ok": True, "message": f"External Connector '{item.id}' 注册成功（项目: {item.project_id}）"}


@router.get("/external-connector/{project_id}", summary="导出项目 External Connector")
def list_external_connectors(project_id: str, connector_type: Optional[str] = None, only_enabled: bool = False) -> dict:
    items = registry.get_external_connectors(project_id, connector_type=connector_type, only_enabled=only_enabled)
    return {
        "project_id": project_id,
        "count": len(items),
        "external_connectors": [item.model_dump() for item in items],
    }


@router.delete("/external-connector/{project_id}/{connector_id}", summary="注销 External Connector")
def unregister_external_connector(project_id: str, connector_id: str) -> dict:
    if not registry.unregister_external_connector(project_id, connector_id):
        raise HTTPException(status_code=404, detail=f"External Connector '{connector_id}' 不存在")
    db = _db()
    try:
        db.query(ExternalConnectorModel).filter(
            ExternalConnectorModel.project_id == project_id,
            ExternalConnectorModel.connector_id == connector_id,
        ).delete()
        db.commit()
    finally:
        db.close()
    return {"ok": True, "message": f"External Connector '{connector_id}' 已注销"}


# ============================================================
# Runtime Context 注册
# ============================================================

@router.post("/runtime-context", summary="注册 Runtime Context")
def register_runtime_context(item: RuntimeContextItem) -> dict:
    """注册或更新项目级运行时上下文。"""
    try:
        registry.register_runtime_context(item)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db = _db()
    try:
        upsert_runtime_context_model(db, item)
        db.commit()
    finally:
        db.close()
    return {"ok": True, "message": f"Runtime Context '{item.id}' 注册成功（项目: {item.project_id}）"}


@router.get("/runtime-context/{project_id}", summary="导出项目 Runtime Context")
def list_runtime_contexts(
    project_id: str,
    environment: Optional[str] = None,
    cluster: Optional[str] = None,
    only_enabled: bool = False,
) -> dict:
    items = registry.get_runtime_contexts(
        project_id,
        environment=environment,
        cluster=cluster,
        only_enabled=only_enabled,
    )
    return {
        "project_id": project_id,
        "environment": environment,
        "cluster": cluster,
        "count": len(items),
        "runtime_contexts": [item.model_dump() for item in items],
    }


@router.delete("/runtime-context/{project_id}/{runtime_id}", summary="注销 Runtime Context")
def unregister_runtime_context(project_id: str, runtime_id: str) -> dict:
    if not registry.unregister_runtime_context(project_id, runtime_id):
        raise HTTPException(status_code=404, detail=f"Runtime Context '{runtime_id}' 不存在")
    db = _db()
    try:
        db.query(RuntimeContextModel).filter(
            RuntimeContextModel.project_id == project_id,
            RuntimeContextModel.runtime_id == runtime_id,
        ).delete()
        db.commit()
    finally:
        db.close()
    return {"ok": True, "message": f"Runtime Context '{runtime_id}' 已注销"}


# ============================================================
# Skill 注册
# ============================================================

@router.post("/skill", summary="注册 Skill")
def register_skill(item: SkillItem) -> dict:
    """注册或更新一条项目级 Skill。"""
    try:
        registry.register_skill(item)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db = _db()
    try:
        upsert_skill_model(db, item)
        db.commit()
    finally:
        db.close()
    return {"ok": True, "message": f"Skill '{item.id}' 注册成功（项目: {item.project_id}）"}


@router.get("/skill/{project_id}", summary="导出项目 Skill")
def list_skills(
    project_id: str,
    kind: Optional[str] = None,
    only_enabled: bool = False,
) -> dict:
    items = registry.get_skills(project_id, kind=kind, only_enabled=only_enabled)
    return {
        "project_id": project_id,
        "kind": kind,
        "count": len(items),
        "skills": [item.model_dump() for item in items],
    }


@router.delete("/skill/{project_id}/{skill_id}", summary="注销 Skill")
def unregister_skill(project_id: str, skill_id: str) -> dict:
    if not registry.unregister_skill(project_id, skill_id):
        raise HTTPException(status_code=404, detail=f"Skill '{skill_id}' 不存在")
    db = _db()
    try:
        db.query(SkillModel).filter(
            SkillModel.project_id == project_id,
            SkillModel.skill_id == skill_id,
        ).delete()
        db.commit()
    finally:
        db.close()
    return {"ok": True, "message": f"Skill '{skill_id}' 已注销"}


# ============================================================
# 状态查询
# ============================================================

@router.get("/status", summary="注册状态")
def get_status() -> dict:
    """返回当前注册状态：各项目的注册项概览。"""
    return registry.get_status()


# ============================================================
# 业务术语表
# ============================================================

@router.post("/glossary", summary="注册业务术语")
def register_glossary(item: GlossaryItem) -> dict:
    """注册或更新一条业务术语条目。"""
    try:
        registry.register_glossary(item)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db = _db()
    try:
        upsert_glossary_model(db, item)
        db.commit()
    finally:
        db.close()
    return {"ok": True, "message": f"术语 '{item.id}' 注册成功（项目: {item.project_id}）"}


@router.post("/glossary/bulk", summary="批量导入业务术语")
def bulk_register_glossary(payload: dict) -> dict:
    """批量导入业务术语，请求体格式：

    {
      "project_id": "xxx",
      "glossary": [
        {"id": "order-create", "term": "下单", "aliases": ["生单"], "code_keywords": ["createOrder"], "description": "..."},
        ...
      ]
    }
    """
    project_id = payload.get("project_id")
    items_raw = payload.get("glossary") or []
    if not project_id:
        raise HTTPException(status_code=400, detail="project_id 必填")
    if not isinstance(items_raw, list) or not items_raw:
        raise HTTPException(status_code=400, detail="glossary 必须为非空数组")

    ok, fail = 0, []
    db = _db()
    try:
        for raw in items_raw:
            try:
                item = GlossaryItem(project_id=project_id, **raw)
                registry.register_glossary(item)
                upsert_glossary_model(db, item)
                ok += 1
            except Exception as e:  # noqa: BLE001
                fail.append({"id": raw.get("id"), "error": str(e)})
        db.commit()
    finally:
        db.close()
    return {"ok": True, "inserted": ok, "failed": fail}


@router.get("/glossary/{project_id}", summary="导出业务术语")
def export_glossary(project_id: str, only_enabled: bool = False) -> dict:
    items = registry.get_glossaries(project_id, only_enabled=only_enabled)
    return {
        "project_id": project_id,
        "count": len(items),
        "glossary": [g.model_dump() for g in items],
    }


@router.delete("/glossary/{project_id}/{glossary_id}", summary="注销业务术语")
def unregister_glossary(project_id: str, glossary_id: str) -> dict:
    if not registry.unregister_glossary(project_id, glossary_id):
        raise HTTPException(status_code=404, detail=f"术语 '{glossary_id}' 不存在")
    db = _db()
    try:
        db.query(GlossaryModel).filter(
            GlossaryModel.project_id == project_id,
            GlossaryModel.glossary_id == glossary_id,
        ).delete()
        db.commit()
    finally:
        db.close()
    return {"ok": True, "message": f"术语 '{glossary_id}' 已注销"}


# ============================================================
# 业务知识笔记
# ============================================================

_VALID_KNOWLEDGE_KINDS = {
    "schema_convention", "field_semantics", "pitfall", "metric_definition",
}


@router.post("/knowledge", summary="注册知识笔记")
def register_knowledge_note(item: KnowledgeNoteItem) -> dict:
    """注册或更新一条知识笔记。"""
    try:
        registry.register_knowledge_note(item)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db = _db()
    try:
        upsert_knowledge_note_model(db, item)
        db.commit()
    finally:
        db.close()
    return {"ok": True, "message": f"知识笔记 '{item.id}' 注册成功（项目: {item.project_id}）"}


@router.post("/knowledge/bulk", summary="批量导入知识笔记")
def bulk_register_knowledge_notes(payload: dict) -> dict:
    """批量导入知识笔记，请求体格式：

    {
      "project_id": "xxx",
      "notes": [
        {"id": "tz-utc", "kind": "schema_convention", "scope": "vt-db",
         "title": "...", "content": "...", "tags": ["timezone"]},
        ...
      ]
    }

    kind 必须在：schema_convention / field_semantics / pitfall / metric_definition 之内。
    导入时 source="import"。
    """
    project_id = payload.get("project_id")
    items_raw = payload.get("notes") or []
    if not project_id:
        raise HTTPException(status_code=400, detail="project_id 必填")
    if not isinstance(items_raw, list) or not items_raw:
        raise HTTPException(status_code=400, detail="notes 必须为非空数组")

    ok, fail = 0, []
    db = _db()
    try:
        for raw in items_raw:
            try:
                raw_copy = dict(raw)
                raw_copy.setdefault("source", "import")
                item = KnowledgeNoteItem(project_id=project_id, **raw_copy)
                registry.register_knowledge_note(item)
                upsert_knowledge_note_model(db, item)
                ok += 1
            except Exception as e:  # noqa: BLE001
                fail.append({"id": raw.get("id"), "error": str(e)})
        db.commit()
    finally:
        db.close()
    return {"ok": True, "inserted": ok, "failed": fail}


@router.get("/knowledge/{project_id}", summary="导出知识笔记")
def export_knowledge_notes(
    project_id: str,
    kind: Optional[str] = None,
    only_enabled: bool = False,
) -> dict:
    if kind and kind not in _VALID_KNOWLEDGE_KINDS:
        raise HTTPException(
            status_code=400,
            detail=f"无效 kind '{kind}'，合法值：{sorted(_VALID_KNOWLEDGE_KINDS)}",
        )
    items = registry.get_knowledge_notes(project_id, kind=kind, only_enabled=only_enabled)
    return {
        "project_id": project_id,
        "kind": kind,
        "count": len(items),
        "notes": [n.model_dump() for n in items],
    }


@router.delete("/knowledge/{project_id}/{note_id}", summary="注销知识笔记")
def unregister_knowledge_note(project_id: str, note_id: str) -> dict:
    if not registry.unregister_knowledge_note(project_id, note_id):
        raise HTTPException(status_code=404, detail=f"知识笔记 '{note_id}' 不存在")
    db = _db()
    try:
        db.query(KnowledgeNoteModel).filter(
            KnowledgeNoteModel.project_id == project_id,
            KnowledgeNoteModel.note_id == note_id,
        ).delete()
        db.commit()
    finally:
        db.close()
    return {"ok": True, "message": f"知识笔记 '{note_id}' 已注销"}
