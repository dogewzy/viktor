"""项目接入审核服务。

接入流程把「仓库分析 → 候选知识审核 → 采纳落地」串成可轮询流程。
项目只有在用户采纳候选产物时才写入正式注册表。
"""
import threading
import uuid
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from fnmatch import fnmatch
from typing import Any, Optional

from loguru import logger

from core.database import SessionLocal
from core.language_defaults import resolve_language, resolve_test_command
from core.models import (
    ContextModel,
    OnboardingArtifactModel,
    OnboardingTaskModel,
    ProjectModel,
)
from core.registry import (
    ContextItem,
    DatabaseConnectorItem,
    ExternalConnectorItem,
    GlossaryItem,
    KnowledgeNoteItem,
    LogConnectorItem,
    ProjectItem,
    RepositoryConnectorItem,
    RuntimeContextItem,
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
)
from gitlab.service import (
    CodeAnalyzer,
    GitLabClient,
    _build_file_tree_text,
    _truncate_for_llm,
    filter_relevant_files,
)
from settings import gitlab_config, llm_config
from tools.runtime_context import discover_runtime_context_items


DOC_FILENAMES = {
    "readme.md",
    "readme",
    "agents.md",
    "业务知识.md",
}
DOC_EXTENSIONS = {".md", ".rst", ".txt"}
ROOT_DIR = "__root__"
CONNECTOR_CONFIG_PROFILE_KEYS = ("connector_config_files", "connector_config_paths")
SECRET_FIELD_HINTS = (
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
    "access_key",
    "accesskey",
    "private_key",
)
EXTERNAL_CONNECTOR_TYPE_ALIASES = {
    "oss": "object_storage",
    "s3": "object_storage",
    "object-store": "object_storage",
    "object_store": "object_storage",
    "rabbitmq": "queue",
    "amqp": "queue",
    "mq": "queue",
    "milvus": "vector_store",
    "zilliz": "vector_store",
    "vector": "vector_store",
    "http": "http_service",
    "https": "http_service",
    "service": "http_service",
}
EXTERNAL_CONNECTOR_TYPES = {"redis", "object_storage", "queue", "vector_store", "http_service"}

# 候选产物会被人审核，也会在采纳后进入正式上下文。这里限制的是“可读正文”，
# 原始覆盖范围与证据放 payload/stats，避免把中间分析全文塞进 prompt。
ARTIFACT_CONTENT_MAX_CHARS = 12_000
ARTIFACT_CONTENT_MAX_BYTES = 48_000
DIRECTORY_ARTIFACT_MAX_CHARS = 10_000
DIRECTORY_SYNTHESIS_INPUT_MAX_CHARS = 40_000
DIRECTORY_ARTIFACT_SNIPPET_CHARS = 650
DIRECTORY_SYNTHESIS_SNIPPET_CHARS = 1_500
PAYLOAD_SUMMARY_SNIPPET_CHARS = 900


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _repo_slug(repo_url: str) -> str:
    path = GitLabClient.extract_project_path(repo_url)
    return path.replace("/", "-").replace(".", "-")[:96] or "repo"


def _safe_connector_id(raw: str, fallback: str) -> str:
    base = (raw or fallback or "connector").strip().lower()
    base = re.sub(r"[^a-z0-9_.-]+", "-", base).strip("-._")
    return (base or fallback or "connector")[:120]


def _as_int(value: Any, default: int) -> int:
    try:
        if value in (None, ""):
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "y", "on"}:
            return True
        if lowered in {"false", "0", "no", "n", "off"}:
            return False
    return default


def _repo_display_name(repo_url: str) -> str:
    path = GitLabClient.extract_project_path(repo_url)
    name = path.rsplit("/", 1)[-1]
    return name or path or repo_url


def _make_unique_connector_id(raw: str, fallback: str, used_ids: set[str]) -> str:
    base = _safe_connector_id(raw, fallback)
    candidate = base
    index = 2
    while candidate in used_ids:
        suffix = f"-{index}"
        candidate = f"{base[: max(1, 120 - len(suffix))]}{suffix}"
        index += 1
    used_ids.add(candidate)
    return candidate


def _iter_repository_inputs(*values: Any) -> list[Any]:
    items: list[Any] = []
    for value in values:
        if value is None:
            continue
        if isinstance(value, str):
            text = value.strip()
            if not text:
                continue
            parts = [part.strip() for part in re.split(r"[\n,]+", text) if part.strip()]
            if len(parts) > 1:
                items.extend(parts)
            else:
                items.append(text)
            continue
        if isinstance(value, list):
            items.extend(_iter_repository_inputs(*value))
            continue
        items.append(value)
    return items


def _repo_url_from_raw(raw: Any) -> str:
    if isinstance(raw, str):
        return raw.strip()
    if not isinstance(raw, dict):
        return ""
    return str(
        raw.get("repo_url")
        or raw.get("git_url")
        or raw.get("url")
        or raw.get("clone_url")
        or ""
    ).strip()


def _normalize_repository_specs(
    *,
    project_id: str,
    repo_url: str = "",
    branch: str = "master",
    profile: Optional[dict[str, Any]] = None,
    repo_urls: Optional[list[Any]] = None,
    repositories: Optional[list[Any]] = None,
    repos: Optional[list[Any]] = None,
) -> list[dict[str, Any]]:
    """Normalize onboarding repository input while preserving the old single-repo API."""
    profile = dict(profile or {})
    raw_items = _iter_repository_inputs(
        {"repo_url": repo_url, "branch": branch} if str(repo_url or "").strip() else None,
        repo_urls,
        repositories,
        repos,
        profile.get("repo_urls"),
        profile.get("repositories"),
        profile.get("repos"),
    )

    specs: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    used_ids: set[str] = set()
    default_branch = (branch or "master").strip() or "master"
    for raw in raw_items:
        url = _repo_url_from_raw(raw)
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)

        data = raw if isinstance(raw, dict) else {}
        repo_branch = str(data.get("branch") or data.get("default_branch") or default_branch).strip() or "master"
        raw_id = str(data.get("id") or data.get("connector_id") or data.get("repo_id") or "").strip()
        fallback_id = "default" if not specs else _repo_display_name(url)
        connector_id = _make_unique_connector_id(raw_id, fallback_id, used_ids)
        display_name = str(
            data.get("display_name")
            or data.get("name")
            or data.get("title")
            or _repo_display_name(url)
        ).strip()
        spec_profile = data.get("profile") if isinstance(data.get("profile"), dict) else {}
        config_files = _iter_repository_inputs(data.get("connector_config_files"), data.get("connector_config_paths"))
        specs.append({
            "id": connector_id,
            "project_id": project_id,
            "display_name": display_name or connector_id,
            "repo_url": url,
            "git_url": url,
            "branch": repo_branch,
            "default_branch": repo_branch,
            "sort_order": _as_int(data.get("sort_order"), len(specs) * 10),
            "build_venv": _as_bool(data.get("build_venv"), True),
            "language": str(data.get("language") or "").strip(),
            "test_command": str(data.get("test_command") or "").strip(),
            "lint_command": str(data.get("lint_command") or "").strip(),
            "connector_config_files": [str(item).strip() for item in config_files if str(item).strip()],
            "profile": dict(spec_profile),
        })

    if not specs:
        raise ValueError("repo_url 或 repositories/repo_urls 至少需要提供一个仓库")
    return specs


def _repository_connector_item_from_spec(project_id: str, spec: dict[str, Any]) -> RepositoryConnectorItem:
    return RepositoryConnectorItem(
        id=str(spec.get("id") or "default"),
        project_id=project_id,
        display_name=str(spec.get("display_name") or spec.get("id") or ""),
        git_url=str(spec.get("repo_url") or spec.get("git_url") or ""),
        default_branch=str(spec.get("branch") or spec.get("default_branch") or "master"),
        sort_order=_as_int(spec.get("sort_order"), 0),
        build_venv=_as_bool(spec.get("build_venv"), True),
        language=str(spec.get("language") or "").strip(),
        test_command=str(spec.get("test_command") or "").strip(),
        lint_command=str(spec.get("lint_command") or "").strip(),
    )


def _repository_connector_markdown(item: RepositoryConnectorItem) -> str:
    return "\n".join([
        f"## {item.display_name or item.id}",
        "",
        f"- Connector ID：{item.id}",
        f"- Git URL：{item.git_url}",
        f"- 默认分支：{item.default_branch or 'master'}",
        f"- 预热依赖环境：{'是' if item.build_venv else '否'}",
        f"- 语言：{resolve_language(item) or '自动探测'}",
        f"- 测试命令：{resolve_test_command(item) or '（无）'}",
    ])


def _project_item_for_ensure(
    *,
    project_id: str,
    name: str,
    description: str,
    repo_url: str,
    branch: str,
    existing: Any | None = None,
) -> ProjectItem:
    if existing is None:
        return ProjectItem(
            id=project_id,
            name=name or project_id,
            description=description,
            git_url=repo_url or None,
            default_branch=branch or "master",
        )
    return ProjectItem(
        id=project_id,
        name=str(getattr(existing, "name", "") or name or project_id),
        description=str(getattr(existing, "description", "") or description or ""),
        git_url=str(getattr(existing, "git_url", "") or repo_url or "") or None,
        default_branch=str(getattr(existing, "default_branch", "") or branch or "master"),
    )


def _repository_profile(base_profile: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base_profile or {})
    merged.update(spec.get("profile") or {})
    if spec.get("connector_config_files"):
        merged["connector_config_files"] = spec["connector_config_files"]
    return merged


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _load_llm_json(raw: str) -> dict[str, Any]:
    cleaned = (raw or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()
        if cleaned.startswith("json"):
            cleaned = cleaned[4:].strip()
    return json.loads(cleaned)


def _truncate_text(
    text: str,
    *,
    max_chars: int = ARTIFACT_CONTENT_MAX_CHARS,
    max_bytes: int = ARTIFACT_CONTENT_MAX_BYTES,
    suffix: str = "\n\n...（内容已压缩，完整依据见 payload.evidence）",
) -> tuple[str, bool]:
    """按字符和 UTF-8 字节双上限压缩文本，避免 MySQL TEXT 与 prompt 失控。"""
    original = str(text or "")
    raw = original
    truncated = False
    if len(raw) > max_chars:
        raw = raw[:max_chars]
        truncated = True

    if truncated and suffix and not raw.endswith(suffix):
        raw = raw.rstrip()

    encoded = raw.encode("utf-8")
    suffix_bytes = suffix.encode("utf-8") if suffix else b""
    if len(encoded) + (len(suffix_bytes) if truncated else 0) > max_bytes:
        keep = max(0, max_bytes - (len(suffix_bytes) if suffix else 0))
        raw = encoded[:keep].decode("utf-8", errors="ignore").rstrip()
        truncated = True
    elif len(encoded) > max_bytes:
        keep = max(0, max_bytes - (len(suffix_bytes) if suffix else 0))
        raw = encoded[:keep].decode("utf-8", errors="ignore").rstrip()
        truncated = True

    if truncated and suffix and not raw.endswith(suffix):
        raw += suffix
    return raw, truncated


def _compact_lines(text: str, *, max_chars: int) -> str:
    """保留 Markdown 中信息密度较高的前几行，用于目录地图。"""
    lines = []
    total = 0
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line or line in {"---", "```"}:
            continue
        if line.startswith("好的，") or line.startswith("以下是"):
            continue
        lines.append(line)
        total += len(line) + 1
        if total >= max_chars:
            break
    compact = "\n".join(lines) if lines else str(text or "").strip()
    compact, _ = _truncate_text(compact, max_chars=max_chars, max_bytes=max_chars * 4, suffix="...")
    return compact


def _directory_files_preview(files: list[str], limit: int = 8) -> str:
    shown = files[:limit]
    suffix = "" if len(files) <= limit else f" 等 {len(files)} 个文件"
    return ", ".join(shown) + suffix if shown else "未记录文件"


def _build_directory_digest(
    directory_summaries: list[dict[str, Any]],
    *,
    per_dir_chars: int,
    max_chars: int,
    include_files: bool = True,
) -> tuple[str, bool]:
    """把目录级长分析压成目录地图，作为候选正文或综合分析输入。"""
    parts: list[str] = []
    for item in directory_summaries:
        directory = item.get("directory") or ROOT_DIR
        files = item.get("files") or []
        summary = _compact_lines(str(item.get("summary") or ""), max_chars=per_dir_chars)
        if include_files:
            parts.append(f"## {directory}\n\n覆盖文件：{_directory_files_preview(files)}\n\n{summary}")
        else:
            parts.append(f"## {directory}\n\n{summary}")
    return _truncate_text(
        "\n\n".join(parts),
        max_chars=max_chars,
        max_bytes=ARTIFACT_CONTENT_MAX_BYTES,
        suffix="\n\n...（目录较多，已保留高密度摘要；完整覆盖范围见 payload.evidence.directories）",
    )


def _directory_evidence(directory_summaries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """给 payload 保留可检索证据，但不塞入目录分析全文。"""
    evidence = []
    for item in directory_summaries:
        snippet, truncated = _truncate_text(
            str(item.get("summary") or ""),
            max_chars=PAYLOAD_SUMMARY_SNIPPET_CHARS,
            max_bytes=PAYLOAD_SUMMARY_SNIPPET_CHARS * 4,
            suffix="...",
        )
        evidence.append({
            "directory": item.get("directory") or ROOT_DIR,
            "files": item.get("files") or [],
            "summary_preview": snippet,
            "summary_preview_truncated": truncated,
        })
    return evidence


def _prepare_artifact_content(
    artifact_type: str,
    title: str,
    content: str,
    payload: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """统一保护候选产物正文大小，并记录压缩元数据。"""
    max_chars = DIRECTORY_ARTIFACT_MAX_CHARS if title == "目录调研摘要" else ARTIFACT_CONTENT_MAX_CHARS
    normalized, truncated = _truncate_text(str(content or ""), max_chars=max_chars)
    if not truncated:
        return normalized, payload

    original = str(content or "")
    meta = dict(payload)
    if meta.get("content") == original:
        meta["content"] = normalized
    if meta.get("description") == original:
        meta["description"] = normalized
    existing_compaction = meta.get("content_compaction") if isinstance(meta.get("content_compaction"), dict) else {}
    meta["content_compaction"] = {
        **existing_compaction,
        "storage_guard": {
            "artifact_type": artifact_type,
            "title": title,
            "original_chars": len(original),
            "stored_chars": len(normalized),
            "max_chars": max_chars,
            "reason": "candidate_artifact_readability_limit",
        },
    }
    return normalized, meta


def _mask_sensitive(data: Any) -> Any:
    if isinstance(data, dict):
        out = {}
        for key, value in data.items():
            lowered = str(key).lower()
            if lowered == "secrets" and isinstance(value, dict):
                out[key] = _mask_sensitive(value)
            elif any(hint in lowered for hint in SECRET_FIELD_HINTS):
                out[key] = "***"
            else:
                out[key] = _mask_sensitive(value)
        return out
    if isinstance(data, list):
        return [_mask_sensitive(item) for item in data]
    return data


def _analysis_limits(level: str) -> dict[str, int]:
    if level == "quick":
        return {"docs": 12, "dirs": 6, "files_per_dir": 10, "max_chars": 60_000}
    if level == "deep":
        return {"docs": 80, "dirs": 40, "files_per_dir": 40, "max_chars": 180_000}
    return {"docs": 40, "dirs": 18, "files_per_dir": 24, "max_chars": 120_000}


def _top_dir(path: str) -> str:
    parts = path.split("/", 1)
    return parts[0] if len(parts) > 1 else ROOT_DIR


def _select_document_files(paths: list[str], limit: int) -> list[str]:
    def score(path: str) -> tuple[int, str]:
        lower = path.lower()
        name = lower.rsplit("/", 1)[-1]
        if name in DOC_FILENAMES:
            return (0, path)
        if lower.startswith("docs/") or "/docs/" in lower:
            return (1, path)
        if lower.endswith(".md"):
            return (2, path)
        return (9, path)

    docs = []
    for path in paths:
        lower = path.lower()
        suffix = "." + lower.rsplit(".", 1)[-1] if "." in lower.rsplit("/", 1)[-1] else ""
        name = lower.rsplit("/", 1)[-1]
        if name in DOC_FILENAMES or suffix in DOC_EXTENSIONS or lower.startswith("docs/") or "/docs/" in lower:
            docs.append(path)
    docs.sort(key=score)
    return docs[:limit]


def _group_files_by_directory(paths: list[str], max_dirs: int, files_per_dir: int) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for path in paths:
        grouped.setdefault(_top_dir(path), []).append(path)
    ordered = sorted(grouped.items(), key=lambda item: (0 if item[0] == ROOT_DIR else 1, item[0]))
    return {directory: files[:files_per_dir] for directory, files in ordered[:max_dirs]}


def _analyze_directory_worker(
    *,
    base_url: str,
    token: str,
    project_path: str,
    ref: str,
    directory: str,
    file_paths: list[str],
    docs_summary: str,
    max_file_size_kb: int,
    max_chars: int,
) -> dict[str, Any]:
    """目录级调研任务：独立读取目录文件并调用目录 Prompt。"""
    client = GitLabClient(base_url=base_url, private_token=token)
    dir_code, dir_read, dir_failed = _collect_code_content_with_evidence(
        client,
        project_path,
        file_paths,
        ref=ref,
        max_file_size_kb=max_file_size_kb,
    )
    summary = CodeAnalyzer().analyze_directory(
        directory,
        _truncate_for_llm(docs_summary, 20_000),
        _truncate_for_llm(dir_code, max_chars),
    )
    return {
        "directory": directory,
        "files": file_paths,
        "summary": summary,
        "read_files": dir_read,
        "failed_files": dir_failed,
    }


def ensure_project(
    project_id: str,
    *,
    name: str = "",
    description: str = "",
    repo_url: str = "",
    branch: str = "master",
    repository_specs: Optional[list[dict[str, Any]]] = None,
) -> None:
    """确保项目存在；只在审核采纳落地时创建正式项目。"""
    db = SessionLocal()
    try:
        row = db.get(ProjectModel, project_id)
        item = _project_item_for_ensure(
            project_id=project_id,
            name=name,
            description=description,
            repo_url=repo_url,
            branch=branch,
            existing=row,
        )
        if row:
            row.name = item.name
            row.description = item.description
            row.git_url = item.git_url
            row.default_branch = item.default_branch
        else:
            db.add(ProjectModel(
                project_id=item.id,
                name=item.name,
                description=item.description,
                git_url=item.git_url,
                default_branch=item.default_branch,
                k8s_workload=None,
            ))
        registry.register_project(item)
        connector_specs = repository_specs or (
            [{
                "id": "default",
                "display_name": f"{item.name} (default)",
                "repo_url": repo_url,
                "branch": item.default_branch,
                "sort_order": 0,
                "build_venv": True,
            }] if repo_url else []
        )
        for spec in connector_specs:
            repo = _repository_connector_item_from_spec(project_id, spec)
            if not repo.git_url:
                continue
            registry.register_repository_connector(repo)
            upsert_repository_connector_model(db, repo)
        db.commit()
    finally:
        db.close()


def _update_task(task_id: str, **kwargs: Any) -> None:
    db = SessionLocal()
    try:
        row = db.get(OnboardingTaskModel, task_id)
        if not row:
            return
        for key, value in kwargs.items():
            setattr(row, key, value)
        db.commit()
    finally:
        db.close()


def _get_task_stats(task_id: str) -> dict[str, Any]:
    db = SessionLocal()
    try:
        row = db.get(OnboardingTaskModel, task_id)
        if not row:
            return {}
        return dict(row.stats or {})
    finally:
        db.close()


def _append_task_event(task_id: str, stage: str, message: str, extra: dict[str, Any] | None = None) -> None:
    """把后台接入过程写入 stats.events，便于前端审计分析过程。"""
    db = SessionLocal()
    try:
        row = db.get(OnboardingTaskModel, task_id)
        if not row:
            return
        stats = dict(row.stats or {})
        events = list(stats.get("events") or [])
        events.append({
            "stage": stage,
            "message": message,
            "extra": extra or {},
        })
        stats["events"] = events[-80:]
        row.stats = stats
        row.stage = stage
        row.message = message
        db.commit()
    finally:
        db.close()


def _collect_code_content_with_evidence(
    client: GitLabClient,
    project_path: str,
    file_paths: list[str],
    ref: str,
    max_file_size_kb: int,
) -> tuple[str, list[dict[str, Any]], list[dict[str, str]]]:
    """读取文件并记录证据；分析质量不应只看结果文本。"""
    parts: list[str] = []
    read_files: list[dict[str, Any]] = []
    failed_files: list[dict[str, str]] = []
    max_chars = max_file_size_kb * 1024

    for fp in file_paths:
        try:
            content = client.get_file_content(project_path, fp, ref=ref)
            original_chars = len(content)
            truncated = original_chars > max_chars
            if truncated:
                content = content[:max_chars] + "\n... (文件过大，已截断)"
            parts.append(f"### {fp}\n```\n{content}\n```\n")
            read_files.append({
                "path": fp,
                "chars": original_chars,
                "truncated": truncated,
            })
        except Exception as e:  # noqa: BLE001
            logger.warning("读取文件 {} 失败: {}", fp, e)
            failed_files.append({"path": fp, "error": str(e)})
    return "\n".join(parts), read_files, failed_files


def _create_artifact(
    *,
    task_id: str,
    project_id: str,
    artifact_type: str,
    target_id: str,
    title: str,
    content: str,
    payload: dict[str, Any],
) -> None:
    db = SessionLocal()
    try:
        stored_content, stored_payload = _prepare_artifact_content(
            artifact_type,
            title,
            content,
            dict(payload or {}),
        )
        row = OnboardingArtifactModel(
            artifact_id=_new_id("artifact"),
            task_id=task_id,
            project_id=project_id,
            artifact_type=artifact_type,
            target_id=target_id,
            title=title,
            content=stored_content,
            payload=stored_payload,
            status="pending",
        )
        db.add(row)
        db.commit()
    finally:
        db.close()


def _create_repository_connector_artifact(
    *,
    task_id: str,
    project_id: str,
    spec: dict[str, Any],
) -> None:
    item = _repository_connector_item_from_spec(project_id, spec)
    _create_artifact(
        task_id=task_id,
        project_id=project_id,
        artifact_type="repository_connector",
        target_id=item.id,
        title=f"Repository Connector / {item.display_name or item.id}",
        content=_repository_connector_markdown(item),
        payload={"repository_connector": item.model_dump(), "source": "onboarding"},
    )


def _connector_config_requests(profile: dict[str, Any]) -> list[str]:
    raw_items: list[Any] = []
    for key in CONNECTOR_CONFIG_PROFILE_KEYS:
        value = profile.get(key)
        if isinstance(value, str):
            raw_items.extend(part.strip() for part in value.split(","))
        elif isinstance(value, list):
            raw_items.extend(value)
    out: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        text = str(item or "").strip().strip("/")
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out[:20]


def _resolve_connector_config_files(tree_files: list[str], requested: list[str]) -> tuple[list[str], list[str]]:
    matches: list[str] = []
    missing: list[str] = []
    for req in requested:
        found = [
            path for path in tree_files
            if path == req or path.endswith(f"/{req}") or path.rsplit("/", 1)[-1] == req or fnmatch(path, req)
        ]
        if not found:
            missing.append(req)
            continue
        for path in sorted(found):
            if path not in matches:
                matches.append(path)
    return matches[:20], missing


def _connector_candidate_markdown(kind: str, payload: dict[str, Any], meta: dict[str, Any]) -> str:
    masked = _mask_sensitive(payload)
    lines = [
        f"## {kind} / {payload.get('id') or meta.get('id') or 'candidate'}",
        "",
        f"- 环境：{meta.get('environment') or '待确认'}",
        f"- 来源文件：{meta.get('source_file') or '待确认'}",
        f"- 置信度：{meta.get('confidence') if meta.get('confidence') is not None else '待确认'}",
    ]
    if meta.get("notes"):
        lines.append(f"- 备注：{meta['notes']}")
    lines.extend(["", "```json", json.dumps(masked, ensure_ascii=False, indent=2, default=str), "```"])
    return "\n".join(lines)


def _build_database_connector_payload(project_id: str, raw: dict[str, Any], index: int) -> tuple[str, dict[str, Any], dict[str, Any]]:
    env = str(raw.get("environment") or "").strip()
    raw_id = raw.get("id") or "-".join(
        part for part in [env, raw.get("database") or raw.get("database_name") or raw.get("display_name")] if part
    )
    connector_id = _safe_connector_id(str(raw_id or ""), f"db-{index}")
    payload = {
        "id": connector_id,
        "project_id": project_id,
        "type": str(raw.get("type") or "mysql"),
        "host": str(raw.get("host") or ""),
        "port": _as_int(raw.get("port"), 3306),
        "username": str(raw.get("username") or raw.get("user") or ""),
        "password": str(raw.get("password") or ""),
        "database": str(raw.get("database") or raw.get("database_name") or ""),
        "readonly": _as_bool(raw.get("readonly"), True),
        "charset": str(raw.get("charset") or raw.get("charset_name") or "utf8mb4"),
    }
    meta = {
        "display_name": raw.get("display_name") or connector_id,
        "environment": env,
        "source_file": raw.get("source_file") or "",
        "confidence": raw.get("confidence"),
        "notes": raw.get("notes") or "",
    }
    return connector_id, payload, meta


def _build_log_connector_payload(project_id: str, raw: dict[str, Any], index: int) -> tuple[str, dict[str, Any], dict[str, Any]]:
    env = str(raw.get("environment") or "").strip()
    raw_id = raw.get("id") or "-".join(part for part in [env, raw.get("logstore")] if part)
    connector_id = _safe_connector_id(str(raw_id or ""), f"log-{index}")
    payload = {
        "id": connector_id,
        "project_id": project_id,
        "sls_project": str(raw.get("sls_project") or raw.get("project") or ""),
        "logstore": str(raw.get("logstore") or ""),
        "display_name": str(raw.get("display_name") or connector_id),
        "description": str(raw.get("description") or raw.get("notes") or ""),
        "enabled": _as_bool(raw.get("enabled"), True),
    }
    meta = {
        "environment": env,
        "source_file": raw.get("source_file") or "",
        "confidence": raw.get("confidence"),
        "notes": raw.get("notes") or "",
    }
    return connector_id, payload, meta


def _build_external_connector_payload(project_id: str, raw: dict[str, Any], index: int) -> tuple[str, dict[str, Any], dict[str, Any]]:
    env = str(raw.get("environment") or "").strip()
    raw_type = str(raw.get("connector_type") or raw.get("type") or "http_service").strip().lower()
    connector_type = EXTERNAL_CONNECTOR_TYPE_ALIASES.get(raw_type, raw_type)
    if connector_type not in EXTERNAL_CONNECTOR_TYPES:
        connector_type = "http_service"
    raw_id = raw.get("id") or "-".join(part for part in [env, connector_type, raw.get("display_name")] if part)
    connector_id = _safe_connector_id(str(raw_id or ""), f"external-{index}")
    payload = {
        "id": connector_id,
        "project_id": project_id,
        "connector_type": connector_type,
        "display_name": str(raw.get("display_name") or connector_id),
        "description": str(raw.get("description") or raw.get("notes") or ""),
        "config": _as_dict(raw.get("config")),
        "secrets": _as_dict(raw.get("secrets")),
        "enabled": _as_bool(raw.get("enabled"), True),
    }
    meta = {
        "environment": env,
        "source_file": raw.get("source_file") or "",
        "confidence": raw.get("confidence"),
        "notes": raw.get("notes") or "",
    }
    return connector_id, payload, meta


def _discover_connector_artifacts(
    *,
    task_id: str,
    project_id: str,
    profile: dict[str, Any],
    client: GitLabClient,
    project_path: str,
    tree_files: list[str],
    ref: str,
    analyzer: CodeAnalyzer,
    project_description: str = "",
) -> tuple[int, dict[str, Any]]:
    requested = _connector_config_requests(profile)
    if not requested:
        return 0, {"connector_config_files_requested": 0, "connector_candidates": 0}

    matched_files, missing_files = _resolve_connector_config_files(tree_files, requested)
    _append_task_event(
        task_id,
        "analyzing_connector_configs",
        f"正在分析 {len(matched_files)} 个用户指定配置文件以提取连接器候选",
        {"requested": requested, "matched_files": matched_files, "missing_files": missing_files},
    )
    if not matched_files:
        return 0, {
            "connector_config_files_requested": len(requested),
            "connector_config_files_matched": 0,
            "connector_config_files_missing": missing_files,
            "connector_candidates": 0,
        }

    config_content, read_files, failed_files = _collect_code_content_with_evidence(
        client,
        project_path,
        matched_files,
        ref=ref,
        max_file_size_kb=gitlab_config.max_file_size_kb,
    )
    try:
        raw_json = analyzer.analyze_connector_configs(
            _truncate_for_llm(config_content, 80_000),
            project_description=project_description,
        )
        extracted = _load_llm_json(raw_json)
    except Exception as e:  # noqa: BLE001
        logger.warning("[Onboarding] 连接器配置提取失败: {}", e)
        _append_task_event(task_id, "connector_config_failed", f"连接器配置提取失败: {e}")
        return 0, {
            "connector_config_files_requested": len(requested),
            "connector_config_files_matched": len(matched_files),
            "connector_config_files_missing": missing_files,
            "connector_config_read_files": read_files,
            "connector_config_failed_files": failed_files,
            "connector_config_error": str(e),
            "connector_candidates": 0,
        }

    artifact_count = 0
    for index, raw in enumerate(extracted.get("database_connectors") or [], start=1):
        if not isinstance(raw, dict):
            continue
        connector_id, payload, meta = _build_database_connector_payload(project_id, raw, index)
        _create_artifact(
            task_id=task_id,
            project_id=project_id,
            artifact_type="database_connector",
            target_id=connector_id,
            title=f"Database Connector / {meta.get('display_name') or connector_id}",
            content=_connector_candidate_markdown("Database Connector", payload, meta),
            payload={"database_connector": payload, "meta": meta},
        )
        artifact_count += 1

    for index, raw in enumerate(extracted.get("log_connectors") or [], start=1):
        if not isinstance(raw, dict):
            continue
        connector_id, payload, meta = _build_log_connector_payload(project_id, raw, index)
        _create_artifact(
            task_id=task_id,
            project_id=project_id,
            artifact_type="log_connector",
            target_id=connector_id,
            title=f"Log Connector / {payload.get('display_name') or connector_id}",
            content=_connector_candidate_markdown("Log Connector", payload, meta),
            payload={"log_connector": payload, "meta": meta},
        )
        artifact_count += 1

    for index, raw in enumerate(extracted.get("external_connectors") or [], start=1):
        if not isinstance(raw, dict):
            continue
        connector_id, payload, meta = _build_external_connector_payload(project_id, raw, index)
        _create_artifact(
            task_id=task_id,
            project_id=project_id,
            artifact_type="external_connector",
            target_id=connector_id,
            title=f"External Connector / {payload.get('display_name') or connector_id}",
            content=_connector_candidate_markdown("External Connector", payload, meta),
            payload={"external_connector": payload, "meta": meta},
        )
        artifact_count += 1

    _append_task_event(task_id, "connector_configs_analyzed", f"生成 {artifact_count} 个连接器候选")
    return artifact_count, {
        "connector_config_files_requested": len(requested),
        "connector_config_files_matched": len(matched_files),
        "connector_config_files_missing": missing_files,
        "connector_config_read_files": read_files,
        "connector_config_failed_files": failed_files,
        "connector_candidates": artifact_count,
    }


def _runtime_context_markdown(item: RuntimeContextItem) -> str:
    clusters = ", ".join(item.clusters) if item.clusters else "待确认"
    command = " ".join(item.command) if item.command else "待确认"
    logstores = [
        str(binding.get("logstore") or binding.get("id") or "")
        for binding in item.log_bindings
        if binding.get("logstore") or binding.get("id")
    ]
    return "\n".join([
        f"## {item.workload_name or item.id}",
        "",
        f"- 环境：{item.environment}",
        f"- 集群：{clusters}",
        f"- Namespace：{item.namespace or '待确认'}",
        f"- Workload：{item.workload_type} / {item.workload_name or '待确认'}",
        f"- Service：{item.service_name or '待确认'}",
        f"- 副本数：{item.replicas if item.replicas is not None else '待确认'}",
        f"- 镜像：{item.image or '待确认'}",
        f"- 启动命令：{command}",
        f"- 日志入口：{', '.join(logstores) if logstores else '待确认'}",
        f"- 配置来源：{item.source_repo or 'deploy-config'} / {item.source_path or '待确认'}",
    ])


def _discover_runtime_context_artifacts(
    *,
    task_id: str,
    project_id: str,
    repo_url: str,
    profile: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    if profile.get("discover_runtime_contexts") is False:
        _append_task_event(task_id, "runtime_context_skipped", "已跳过运行时上下文发现")
        return 0, {"runtime_contexts_discovered": 0, "runtime_context_skipped": True}

    environment = str(profile.get("environment") or "prod")
    app_name = str(profile.get("app_name") or "").strip() or None
    k8s_config_root = str(profile.get("k8s_config_root") or "").strip() or None
    _append_task_event(
        task_id,
        "discovering_runtime_context",
        "正在从 deploy-config 发现 KubeVela/Flux 运行时上下文",
        {
            "environment": environment,
            "app_name": app_name or "",
            "k8s_config_root": k8s_config_root or "workspace-default",
        },
    )

    try:
        items = discover_runtime_context_items(
            project_id,
            repo_url,
            k8s_config_root=k8s_config_root,
            app_name=app_name,
            environment=environment,
            source_repo="deploy-config",
        )
    except Exception as e:  # noqa: BLE001
        message = f"未发现运行时上下文，后续可在项目上下文页手动补充：{e}"
        logger.warning("[Onboarding] Runtime Context 发现失败: {}", e)
        _append_task_event(task_id, "runtime_context_not_found", message)
        return 0, {"runtime_contexts_discovered": 0, "runtime_context_error": str(e)}

    for item in items:
        _create_artifact(
            task_id=task_id,
            project_id=project_id,
            artifact_type="runtime_context",
            target_id=item.id,
            title=f"Runtime Context / {item.workload_name or item.id}",
            content=_runtime_context_markdown(item),
            payload={
                "runtime_context": item.model_dump(),
                "evidence": {
                    "source_repo": item.source_repo,
                    "source_path": item.source_path,
                    "app_name": item.app_name,
                    "clusters": item.clusters,
                    "namespace": item.namespace,
                    "workload_name": item.workload_name,
                    "log_bindings": item.log_bindings,
                },
            },
        )

    _append_task_event(task_id, "runtime_context_discovered", f"发现 {len(items)} 个运行时上下文候选")
    return len(items), {"runtime_contexts_discovered": len(items)}


def start_onboarding_task(
    *,
    project_id: str,
    repo_url: str = "",
    branch: str = "master",
    project_name: str = "",
    project_description: str = "",
    analysis_level: str = "standard",
    profile: Optional[dict[str, Any]] = None,
    connector_config_files: Optional[list[str]] = None,
    repo_urls: Optional[list[Any]] = None,
    repositories: Optional[list[Any]] = None,
    repos: Optional[list[Any]] = None,
) -> str:
    """创建接入审核任务并启动后台仓库分析。"""
    task_id = _new_id("onboard")
    merged_profile = dict(profile or {})
    if connector_config_files:
        merged_profile["connector_config_files"] = connector_config_files
    if project_name and "project_name" not in merged_profile:
        merged_profile["project_name"] = project_name
    if project_description and "project_description" not in merged_profile:
        merged_profile["project_description"] = project_description
    repo_specs = _normalize_repository_specs(
        project_id=project_id,
        repo_url=repo_url,
        branch=branch,
        profile=merged_profile,
        repo_urls=repo_urls,
        repositories=repositories,
        repos=repos,
    )
    merged_profile["repositories"] = repo_specs
    merged_profile["repo_urls"] = [spec["repo_url"] for spec in repo_specs]
    primary_repo = repo_specs[0]
    db = SessionLocal()
    try:
        task = OnboardingTaskModel(
            task_id=task_id,
            project_id=project_id,
            repo_url=primary_repo["repo_url"],
            branch=primary_repo["branch"] or "master",
            status="created",
            stage="created",
            message="接入分析已创建，完成后请审核并落地",
            analysis_level=analysis_level,
            profile=merged_profile,
            stats={},
        )
        db.add(task)
        db.commit()
    finally:
        db.close()

    thread = threading.Thread(
        target=analyze_onboarding_repo,
        args=(task_id,),
        daemon=True,
        name=f"onboarding-{task_id}",
    )
    thread.start()
    return task_id


def _repo_specs_for_task(task: OnboardingTaskModel) -> list[dict[str, Any]]:
    profile = task.profile or {}
    has_profile_repos = any(profile.get(key) for key in ("repositories", "repo_urls", "repos"))
    return _normalize_repository_specs(
        project_id=task.project_id,
        repo_url="" if has_profile_repos else task.repo_url,
        branch=task.branch,
        profile=profile,
    )


def _aggregate_repo_stats(
    results: list[dict[str, Any]],
    failures: list[dict[str, Any]],
    *,
    repo_count: int,
    repository_artifacts: int,
) -> dict[str, Any]:
    numeric_keys = [
        "files_seen",
        "files_selected",
        "files_read",
        "files_failed",
        "docs_selected",
        "directories_analyzed",
        "artifacts",
        "connector_candidates",
        "runtime_contexts_discovered",
    ]
    stats: dict[str, Any] = {
        "repositories_total": repo_count,
        "repositories_analyzed": len(results),
        "repositories_failed": len(failures),
        "repository_artifacts": repository_artifacts,
        "repository_results": results,
        "repository_failures": failures,
        "repo_urls": [item.get("repo_url") for item in results if item.get("repo_url")],
    }
    for key in numeric_keys:
        stats[key] = sum(int(item.get(key) or 0) for item in results)
    stats["artifacts"] += repository_artifacts
    stats["all_files_sample"] = [
        path
        for item in results
        for path in list(item.get("all_files_sample") or [])[:50]
    ][:200]
    return stats


def analyze_onboarding_repo(task_id: str) -> None:
    """后台分析仓库，生成待审核落地的候选产物。"""
    db = SessionLocal()
    try:
        task = db.get(OnboardingTaskModel, task_id)
        if not task:
            return
        project_id = task.project_id
        repo_specs = _repo_specs_for_task(task)
    finally:
        db.close()

    if not (llm_config.api_key or "").strip():
        _update_task(task_id, status="failed", stage="failed", message="未配置 LLM API Key")
        return

    try:
        _update_task(
            task_id,
            status="running",
            stage="analyzing_repo",
            message=f"正在分析 {len(repo_specs)} 个 GitLab 仓库...",
        )
        repository_artifacts = 0
        for spec in repo_specs:
            _create_repository_connector_artifact(task_id=task_id, project_id=project_id, spec=spec)
            repository_artifacts += 1

        results: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        for index, spec in enumerate(repo_specs, start=1):
            try:
                stats = _analyze_onboarding_repo_single(
                    task_id,
                    repo_spec=spec,
                    repo_index=index,
                    repo_count=len(repo_specs),
                )
                results.append(stats)
            except Exception as e:  # noqa: BLE001
                logger.exception("[Onboarding] 任务 {} 仓库 {} 分析失败: {}", task_id, spec.get("repo_url"), e)
                failure = {
                    "repo_url": spec.get("repo_url") or spec.get("git_url") or "",
                    "connector_id": spec.get("id") or "",
                    "branch": spec.get("branch") or spec.get("default_branch") or "master",
                    "error": str(e),
                }
                failures.append(failure)
                _append_task_event(task_id, "repo_analysis_failed", f"仓库分析失败：{failure['repo_url']} - {e}", failure)

        if not results:
            _update_task(
                task_id,
                status="failed",
                stage="failed",
                message=f"{len(repo_specs)} 个仓库均分析失败，请检查 GitLab Token、仓库地址和分支",
                stats=_aggregate_repo_stats(
                    results,
                    failures,
                    repo_count=len(repo_specs),
                    repository_artifacts=repository_artifacts,
                ),
            )
            return

        stats = {
            **_get_task_stats(task_id),
            **_aggregate_repo_stats(
                results,
                failures,
                repo_count=len(repo_specs),
                repository_artifacts=repository_artifacts,
            ),
        }
        status_message = (
            f"仓库分析完成：{len(results)}/{len(repo_specs)} 个成功，生成 {stats['artifacts']} 个候选产物，请审核并落地"
            if not failures
            else f"部分仓库分析完成：{len(results)}/{len(repo_specs)} 个成功、{len(failures)} 个失败，"
                 f"已生成 {stats['artifacts']} 个候选产物"
        )
        _update_task(
            task_id,
            status="waiting_review",
            stage="artifacts_ready",
            message=status_message,
            stats=stats,
        )
        logger.info(
            "[Onboarding] 任务 {} 多仓库分析完成，成功 {}，失败 {}，候选产物 {}",
            task_id, len(results), len(failures), stats["artifacts"],
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("[Onboarding] 任务 {} 仓库分析失败: {}", task_id, e)
        _update_task(task_id, status="failed", stage="failed", message=str(e))


def _analyze_onboarding_repo_single(
    task_id: str,
    *,
    repo_spec: dict[str, Any],
    repo_index: int,
    repo_count: int,
) -> dict[str, Any]:
    """Analyze one repository and append its artifacts to the shared onboarding task."""
    db = SessionLocal()
    try:
        task = db.get(OnboardingTaskModel, task_id)
        if not task:
            raise ValueError(f"接入审核任务不存在: {task_id}")
        project_id = task.project_id
        base_profile = task.profile or {}
        analysis_level = task.analysis_level
    finally:
        db.close()

    repo_url = str(repo_spec.get("repo_url") or repo_spec.get("git_url") or "").strip()
    branch = str(repo_spec.get("branch") or repo_spec.get("default_branch") or "master").strip() or "master"
    profile = _repository_profile(base_profile, repo_spec)
    connector_id = str(repo_spec.get("id") or "")

    try:
        _append_task_event(task_id, "analyzing_repo", "开始连接 GitLab", {"repo_url": repo_url, "branch": branch})
        token = (profile.get("gitlab_token") or gitlab_config.token_for_repo_url(repo_url) or "").strip()
        if not token:
            raise ValueError("缺少 GitLab Token")

        base_url = gitlab_config.resolve_base_url(repo_url)
        client = GitLabClient(base_url=base_url, private_token=token)
        project_path = GitLabClient.extract_project_path(repo_url)

        _append_task_event(
            task_id,
            "fetching_tree",
            f"正在获取文件树 ({repo_index}/{repo_count})",
            {"repo_url": repo_url, "branch": branch, "connector_id": connector_id},
        )
        tree = client.get_file_tree(project_path, ref=branch)
        file_tree_text = _build_file_tree_text(tree)
        tree_files = sorted([item["path"] for item in tree if item.get("type") == "blob"])
        limits = _analysis_limits(analysis_level)
        doc_files = _select_document_files(tree_files, limits["docs"])
        relevant_files = filter_relevant_files(
            tree,
            extensions=gitlab_config.file_extensions,
            exclude_dirs=gitlab_config.exclude_dirs,
            max_files=gitlab_config.max_total_files,
            low_priority_dirs=gitlab_config.low_priority_dirs,
        )
        if not relevant_files:
            raise ValueError("未找到可分析的代码文件")

        _append_task_event(
            task_id,
            "selecting_documents",
            f"文件树共 {len(tree_files)} 个文件，优先筛选出 {len(doc_files)} 个文档文件",
            {"doc_files": doc_files},
        )
        docs_content, read_doc_files, failed_doc_files = _collect_code_content_with_evidence(
            client,
            project_path,
            doc_files,
            ref=branch,
            max_file_size_kb=gitlab_config.max_file_size_kb,
        )
        _append_task_event(
            task_id,
            "analyzing_documents",
            f"文档读取完成：成功 {len(read_doc_files)} 个，失败 {len(failed_doc_files)} 个",
            {
                "read_doc_files": read_doc_files,
                "failed_doc_files": failed_doc_files,
            },
        )

        analyzer = CodeAnalyzer()
        slug = _repo_slug(repo_url)
        artifact_count = 0
        project_description = profile.get("project_description", "")

        _append_task_event(task_id, "summarizing_documents", "正在基于文档建立项目语义基线")
        docs_summary = analyzer.analyze_documentation(
            _truncate_for_llm(docs_content or "未发现可用文档。", limits["max_chars"]),
            project_description=project_description,
        )

        grouped_files = _group_files_by_directory(relevant_files, limits["dirs"], limits["files_per_dir"])
        directory_summaries: list[dict[str, Any]] = []
        directory_read_files: list[dict[str, Any]] = []
        directory_failed_files: list[dict[str, str]] = []
        max_workers = min(4, max(1, len(grouped_files)))
        _append_task_event(
            task_id,
            "dispatching_directory_agents",
            f"正在分派 {len(grouped_files)} 个目录调研任务，并发度 {max_workers}",
            {"directories": list(grouped_files.keys())},
        )
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(
                    _analyze_directory_worker,
                    base_url=base_url,
                    token=token,
                    project_path=project_path,
                    ref=branch,
                    directory=directory,
                    file_paths=file_paths,
                    docs_summary=docs_summary,
                    max_file_size_kb=gitlab_config.max_file_size_kb,
                    max_chars=limits["max_chars"],
                ): directory
                for directory, file_paths in grouped_files.items()
            }
            for index, future in enumerate(as_completed(futures), start=1):
                directory = futures[future]
                try:
                    item = future.result()
                except Exception as e:  # noqa: BLE001
                    logger.warning("[Onboarding] 目录 {} 调研失败: {}", directory, e)
                    item = {
                        "directory": directory,
                        "files": grouped_files[directory],
                        "summary": f"目录调研失败，待人工确认：{e}",
                        "read_files": [],
                        "failed_files": [{"path": directory, "error": str(e)}],
                    }
                directory_summaries.append(item)
                directory_read_files.extend(item["read_files"])
                directory_failed_files.extend(item["failed_files"])
                _append_task_event(
                    task_id,
                    "directory_agent_completed",
                    f"目录 {directory} 调研完成 ({index}/{len(grouped_files)})",
                    {
                        "directory": directory,
                        "files": item["files"],
                        "read_count": len(item["read_files"]),
                        "failed_count": len(item["failed_files"]),
                    },
                )

        directory_summaries.sort(key=lambda item: (0 if item["directory"] == ROOT_DIR else 1, item["directory"]))
        for index, (directory, file_paths) in enumerate(grouped_files.items(), start=1):
            _append_task_event(
                task_id,
                "directory_agent_joined",
                f"目录调研结果已归并：{directory} ({index}/{len(grouped_files)})",
                {"directory": directory, "files": file_paths},
            )

        directory_summary_text, directory_summary_truncated = _build_directory_digest(
            directory_summaries,
            per_dir_chars=DIRECTORY_ARTIFACT_SNIPPET_CHARS,
            max_chars=DIRECTORY_ARTIFACT_MAX_CHARS,
            include_files=True,
        )
        directory_synthesis_text, directory_synthesis_truncated = _build_directory_digest(
            directory_summaries,
            per_dir_chars=DIRECTORY_SYNTHESIS_SNIPPET_CHARS,
            max_chars=DIRECTORY_SYNTHESIS_INPUT_MAX_CHARS,
            include_files=False,
        )

        _append_task_event(
            task_id,
            "synthesizing_project",
            f"正在合并 {len(directory_summaries)} 个目录调研结果",
            {"directories": [item["directory"] for item in directory_summaries]},
        )
        comprehensive_summary = analyzer.synthesize_project_analysis(
            _truncate_for_llm(file_tree_text, 20_000),
            _truncate_for_llm(docs_summary, 30_000),
            directory_synthesis_text,
            project_description=project_description,
        )
        comprehensive_summary, comprehensive_truncated = _truncate_text(
            comprehensive_summary,
            max_chars=ARTIFACT_CONTENT_MAX_CHARS,
            suffix="\n\n...（综合分析已压缩为可审核上下文，目录覆盖范围见相关候选产物与 payload.evidence）",
        )

        _create_artifact(
            task_id=task_id,
            project_id=project_id,
            artifact_type="context",
            target_id=f"gitlab:{slug}:project-analysis",
            title="项目综合分析",
            content=comprehensive_summary,
            payload={
                "priority": 20,
                "source": "gitlab_onboarding",
                "repo_url": repo_url,
                "branch": branch,
                "content_compaction": {
                    "synthesis_input_truncated": directory_synthesis_truncated,
                    "output_truncated": comprehensive_truncated,
                    "max_chars": ARTIFACT_CONTENT_MAX_CHARS,
                },
                "evidence": {
                    "tree_files": len(tree_files),
                    "doc_files": doc_files,
                    "read_doc_files": read_doc_files,
                    "failed_doc_files": failed_doc_files,
                    "directories": _directory_evidence(directory_summaries),
                    "read_files": directory_read_files,
                    "failed_files": directory_failed_files,
                },
            },
        )
        artifact_count += 1

        _create_artifact(
            task_id=task_id,
            project_id=project_id,
            artifact_type="context",
            target_id=f"gitlab:{slug}:directory-summaries",
            title="目录调研摘要",
            content=directory_summary_text,
            payload={
                "priority": 21,
                "source": "gitlab_onboarding",
                "repo_url": repo_url,
                "branch": branch,
                "content_compaction": {
                    "directory_summary_truncated": directory_summary_truncated,
                    "max_chars": DIRECTORY_ARTIFACT_MAX_CHARS,
                    "per_directory_chars": DIRECTORY_ARTIFACT_SNIPPET_CHARS,
                },
                "evidence": {
                    "directories": _directory_evidence(directory_summaries),
                    "read_files": directory_read_files,
                    "failed_files": directory_failed_files,
                },
            },
        )
        artifact_count += 1

        api_related_files = [
            path for path in relevant_files
            if any(token in path.lower() for token in ["router", "route", "controller", "handler", "api"])
        ][: max(20, limits["files_per_dir"])]
        if api_related_files:
            _append_task_event(task_id, "generating_api_contracts", f"正在基于 {len(api_related_files)} 个 API 相关文件生成接口契约")
            api_code, read_api_files, failed_api_files = _collect_code_content_with_evidence(
                client,
                project_path,
                api_related_files,
                ref=branch,
                max_file_size_kb=gitlab_config.max_file_size_kb,
            )
            contracts = analyzer.analyze_api_contracts(_truncate_for_llm(api_code, limits["max_chars"]))
        else:
            read_api_files = []
            failed_api_files = []
            contracts = "未在本次筛选文件中识别到明确的 API 入口文件，待人工补充。"
        _create_artifact(
            task_id=task_id,
            project_id=project_id,
            artifact_type="context",
            target_id=f"gitlab:{slug}:api-contracts",
            title="API 契约概要",
            content=contracts,
            payload={
                "priority": 21,
                "source": "gitlab_onboarding",
                "repo_url": repo_url,
                "branch": branch,
                "evidence": {
                    "api_related_files": api_related_files,
                    "read_files": read_api_files,
                    "failed_files": failed_api_files,
                },
            },
        )
        artifact_count += 1

        connector_artifact_count, connector_stats = _discover_connector_artifacts(
            task_id=task_id,
            project_id=project_id,
            profile=profile,
            client=client,
            project_path=project_path,
            tree_files=tree_files,
            ref=branch,
            analyzer=analyzer,
            project_description=project_description,
        )
        artifact_count += connector_artifact_count

        _append_task_event(task_id, "extracting_knowledge", "正在从综合分析中提取结构化术语和知识笔记")
        try:
            import json as _json
            raw_json = analyzer.extract_glossary_and_notes(
                _truncate_for_llm(comprehensive_summary, 60_000),
                project_description=project_description,
            )
            cleaned = raw_json.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
                if cleaned.endswith("```"):
                    cleaned = cleaned[:-3]
            extracted = _json.loads(cleaned)

            for g in extracted.get("glossary", []):
                _create_artifact(
                    task_id=task_id,
                    project_id=project_id,
                    artifact_type="glossary",
                    target_id=f"onboarding:{slug}:{g['id']}",
                    title=g.get("term", g["id"]),
                    content=g.get("description", ""),
                    payload={
                        "term": g.get("term", ""),
                        "aliases": g.get("aliases", []),
                        "code_keywords": g.get("code_keywords", []),
                        "description": g.get("description", ""),
                        "enabled": True,
                        "source": "gitlab_onboarding",
                    },
                )
                artifact_count += 1

            for n in extracted.get("knowledge_notes", []):
                _create_artifact(
                    task_id=task_id,
                    project_id=project_id,
                    artifact_type="knowledge_note",
                    target_id=f"onboarding:{slug}:{n['id']}",
                    title=n.get("title", n["id"]),
                    content=n.get("content", ""),
                    payload={
                        "kind": n.get("kind", "pitfall"),
                        "scope": n.get("scope", ""),
                        "title": n.get("title", ""),
                        "content": n.get("content", ""),
                        "tags": n.get("tags", []),
                        "enabled": True,
                        "source": "import",
                    },
                )
                artifact_count += 1

            _append_task_event(
                task_id,
                "knowledge_extracted",
                f"提取了 {len(extracted.get('glossary', []))} 条术语、"
                f"{len(extracted.get('knowledge_notes', []))} 条知识笔记",
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("[Onboarding] 结构化知识提取失败: {}", e)
            _append_task_event(task_id, "knowledge_extraction_failed", f"结构化知识提取失败: {e}")

        runtime_artifact_count, runtime_stats = _discover_runtime_context_artifacts(
            task_id=task_id,
            project_id=project_id,
            repo_url=repo_url,
            profile=profile,
        )
        artifact_count += runtime_artifact_count

        stats = {
            "repo_url": repo_url,
            "connector_id": connector_id,
            "branch": branch,
            "files_seen": len(tree_files),
            "files_selected": len(relevant_files),
            "files_read": len(read_doc_files) + len(directory_read_files) + len(read_api_files),
            "files_failed": len(failed_doc_files) + len(directory_failed_files) + len(failed_api_files),
            "docs_selected": len(doc_files),
            "directories_analyzed": len(directory_summaries),
            "artifacts": artifact_count,
            "all_files_sample": tree_files[:200],
            "selected_files": relevant_files,
            "doc_files": doc_files,
            "read_doc_files": read_doc_files,
            "failed_doc_files": failed_doc_files,
            "directory_summaries": [
                {"directory": item["directory"], "files": item["files"]}
                for item in directory_summaries
            ],
            "read_files": directory_read_files + read_api_files,
            "failed_files": directory_failed_files + failed_api_files,
            **connector_stats,
            **runtime_stats,
        }
        logger.info("[Onboarding] 任务 {} 仓库 {} 分析完成，候选产物 {}", task_id, repo_url, artifact_count)
        return stats
    except Exception as e:  # noqa: BLE001
        logger.exception("[Onboarding] 任务 {} 仓库 {} 分析失败: {}", task_id, repo_url, e)
        raise


def _normalize_knowledge_note_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    if normalized.get("source") not in {"admin", "api", "import"}:
        normalized["source"] = "import"
    return normalized


def apply_artifacts(task_id: str, artifact_ids: list[str] | None = None) -> dict[str, Any]:
    """把用户采纳的候选产物写入正式知识库，并在此时落地正式项目。"""
    db = SessionLocal()
    applied = 0
    failed: list[dict[str, str]] = []
    try:
        task = db.get(OnboardingTaskModel, task_id)
        if not task:
            raise ValueError(f"接入审核任务不存在: {task_id}")

        query = db.query(OnboardingArtifactModel).filter(OnboardingArtifactModel.task_id == task_id)
        if artifact_ids is not None:
            query = query.filter(OnboardingArtifactModel.artifact_id.in_(artifact_ids))
        query = query.filter(OnboardingArtifactModel.status.in_(["pending", "accepted"]))
        rows = query.all()

        if rows:
            profile = dict(task.profile or {})
            repo_specs = _repo_specs_for_task(task)
            primary_repo = repo_specs[0]
            ensure_project(
                task.project_id,
                name=str(profile.get("project_name") or task.project_id),
                description=str(profile.get("project_description") or ""),
                repo_url=primary_repo["repo_url"],
                branch=primary_repo["branch"],
                repository_specs=[primary_repo],
            )

        for row in rows:
            try:
                if row.artifact_type == "repository_connector":
                    payload = dict(row.payload or {})
                    connector_payload = dict(payload.get("repository_connector") or payload)
                    connector_payload["project_id"] = row.project_id
                    connector_payload["id"] = row.target_id
                    item = RepositoryConnectorItem(**connector_payload)
                    registry.register_repository_connector(item)
                    upsert_repository_connector_model(db, item)
                elif row.artifact_type == "context":
                    item = ContextItem(
                        id=row.target_id,
                        project_id=row.project_id,
                        priority=int((row.payload or {}).get("priority", 20)),
                        content=row.content,
                    )
                    registry.register_context(item)
                    existing = db.get(ContextModel, (item.project_id, item.id))
                    if existing:
                        existing.priority = item.priority
                        existing.content = item.content
                    else:
                        db.add(ContextModel(
                            project_id=item.project_id,
                            context_id=item.id,
                            priority=item.priority,
                            content=item.content,
                        ))
                elif row.artifact_type == "glossary":
                    payload = dict(row.payload or {})
                    item = GlossaryItem(project_id=row.project_id, id=row.target_id, **payload)
                    registry.register_glossary(item)
                    upsert_glossary_model(db, item)
                elif row.artifact_type == "knowledge_note":
                    payload = _normalize_knowledge_note_payload(dict(row.payload or {}))
                    item = KnowledgeNoteItem(project_id=row.project_id, id=row.target_id, **payload)
                    registry.register_knowledge_note(item)
                    upsert_knowledge_note_model(db, item)
                elif row.artifact_type == "database_connector":
                    payload = dict(row.payload or {})
                    connector_payload = dict(payload.get("database_connector") or payload)
                    connector_payload["project_id"] = row.project_id
                    connector_payload["id"] = row.target_id
                    item = DatabaseConnectorItem(**connector_payload)
                    registry.register_database_connector(item)
                    upsert_database_connector_model(db, item)
                elif row.artifact_type == "log_connector":
                    payload = dict(row.payload or {})
                    connector_payload = dict(payload.get("log_connector") or payload)
                    connector_payload["project_id"] = row.project_id
                    connector_payload["id"] = row.target_id
                    item = LogConnectorItem(**connector_payload)
                    registry.register_log_connector(item)
                    upsert_log_connector_model(db, item)
                elif row.artifact_type == "external_connector":
                    payload = dict(row.payload or {})
                    connector_payload = dict(payload.get("external_connector") or payload)
                    connector_payload["project_id"] = row.project_id
                    connector_payload["id"] = row.target_id
                    item = ExternalConnectorItem(**connector_payload)
                    registry.register_external_connector(item)
                    upsert_external_connector_model(db, item)
                elif row.artifact_type == "runtime_context":
                    payload = dict(row.payload or {})
                    runtime_payload = dict(payload.get("runtime_context") or payload)
                    runtime_payload["project_id"] = row.project_id
                    runtime_payload["id"] = row.target_id
                    item = RuntimeContextItem(**runtime_payload)
                    registry.register_runtime_context(item)
                    upsert_runtime_context_model(db, item)
                else:
                    raise ValueError(f"不支持的候选产物类型: {row.artifact_type}")
                row.status = "applied"
                applied += 1
            except Exception as e:  # noqa: BLE001
                failed.append({"artifact_id": row.artifact_id, "error": str(e)})

        if task and applied:
            task.status = "partial_failed" if failed else "completed"
            task.stage = "artifacts_applied"
            if failed:
                task.message = f"已采纳 {applied} 个候选产物，{len(failed)} 个写入失败，请回到候选产物继续处理。"
            else:
                task.message = (
                    f"已采纳 {applied} 个候选产物并写入正式注册项。"
                    "可在管理后台继续编辑上下文/术语/知识笔记/连接器/运行时上下文，或在钉钉对话中实检验证。"
                )
        elif task and failed:
            task.status = "failed"
            task.stage = "apply_failed"
            task.message = f"候选产物写入失败，0 个成功，{len(failed)} 个失败。"
        db.commit()
    finally:
        db.close()
    message = (
        f"已采纳 {applied} 个候选产物"
        if not failed
        else f"已采纳 {applied} 个候选产物，{len(failed)} 个写入失败"
    )
    return {"applied": applied, "failed": failed, "message": message, "ok": not failed and applied > 0}
