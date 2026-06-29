"""Runtime Context discovery and query tools.

The discovery side parses KubeVela/GitOps YAML and produces RuntimeContextItem
objects. The query side exposes registered Runtime Contexts to the agent.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from core.registry import RuntimeContextItem, registry


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def _repo_slug(repo_url: str) -> str:
    raw = repo_url.rstrip("/").split("/")[-1]
    if ":" in raw and not raw.startswith("http"):
        raw = raw.split(":")[-1]
    if raw.endswith(".git"):
        raw = raw[:-4]
    return raw


def _workspace_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def _default_k8s_config_root() -> Path:
    return _workspace_root() / "deploy-config"


def _safe_load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return data if isinstance(data, dict) else {}


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _find_flux_entry(config_root: Path, environment: str, app_name: str) -> Path | None:
    candidates = sorted((config_root / environment / "fluxcd").glob(f"**/{app_name}.yaml"))
    if candidates:
        return candidates[0]
    candidates = sorted((config_root / environment / "fluxcd").glob(f"**/*{app_name}*.yaml"))
    return candidates[0] if candidates else None


def _flux_app_path(flux_path: Path, config_root: Path) -> Path | None:
    data = _safe_load_yaml(flux_path)
    components = (((data.get("spec") or {}).get("components")) or [])
    for component in components:
        props = component.get("properties") or {}
        raw_path = props.get("path")
        if isinstance(raw_path, str) and raw_path:
            return (config_root / raw_path.strip("./")).resolve()
    return None


def _find_app_dir(config_root: Path, environment: str, app_name: str) -> Path | None:
    candidates = sorted((config_root / environment / "apps").glob(f"**/{app_name}"))
    if candidates:
        return candidates[0]
    candidates = sorted((config_root / environment / "apps").glob(f"**/*{app_name}*"))
    dirs = [p for p in candidates if p.is_dir()]
    return dirs[0] if dirs else None


def _clusters(app: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for policy in (app.get("spec") or {}).get("policies") or []:
        if policy.get("type") == "topology":
            raw = (policy.get("properties") or {}).get("clusters") or []
            out.extend(str(x) for x in raw if x)
    return sorted(set(out))


def _objects(app: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for component in (app.get("spec") or {}).get("components") or []:
        props = component.get("properties") or {}
        for obj in props.get("objects") or []:
            if isinstance(obj, dict):
                out.append(obj)
    return out


def _first_container(workload: dict[str, Any]) -> dict[str, Any]:
    pod_spec = (
        (((workload.get("spec") or {}).get("template") or {}).get("spec") or {})
    )
    containers = pod_spec.get("containers") or []
    return containers[0] if containers else {}


def _pod_spec(workload: dict[str, Any]) -> dict[str, Any]:
    return (((workload.get("spec") or {}).get("template") or {}).get("spec") or {})


def _image_tag(image: str) -> str:
    if not image:
        return ""
    tail = image.rsplit("/", 1)[-1]
    return tail.split(":", 1)[1] if ":" in tail else ""


_SLS_SUFFIXES = {
    "project": "sls_project",
    "logstore": "logstore",
    "ttl": "ttl",
    "tags": "tags",
}


def _parse_sls_log_bindings(env: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for item in env:
        name = str(item.get("name") or "")
        if not name.startswith("aliyun_logs_"):
            continue
        value = item.get("value")
        key = name.removeprefix("aliyun_logs_")
        base = key
        field = "path"
        for suffix, mapped in _SLS_SUFFIXES.items():
            marker = f"_{suffix}"
            if key.endswith(marker):
                base = key[: -len(marker)]
                field = mapped
                break
        row = grouped.setdefault(base, {"id": base, "provider": "aliyun_sls"})
        row[field] = value
    return sorted(grouped.values(), key=lambda row: row.get("id") or "")


def _container_ports(container: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for port in container.get("ports") or []:
        if isinstance(port, dict):
            out.append({
                "container_port": port.get("containerPort"),
                "name": port.get("name"),
                "protocol": port.get("protocol"),
            })
    return out


def _service_exposure(service: dict[str, Any]) -> dict[str, Any]:
    spec = service.get("spec") or {}
    return {
        "type": "service",
        "name": (service.get("metadata") or {}).get("name"),
        "service_type": spec.get("type"),
        "selector": spec.get("selector") or {},
        "ports": spec.get("ports") or [],
    }


def _ingress_exposures(config_root: Path, environment: str, service_names: set[str]) -> list[dict[str, Any]]:
    exposures: list[dict[str, Any]] = []
    roots = [
        config_root / environment / "manual" / "ingress",
        config_root / environment / "manaul" / "ingress",
    ]
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.glob("**/*.yaml")):
            data = _safe_load_yaml(path)
            if data.get("kind") != "Ingress":
                continue
            matched = False
            spec = data.get("spec") or {}
            backends: list[dict[str, Any]] = []
            default_service = (((spec.get("defaultBackend") or {}).get("service")) or {})
            if default_service:
                backends.append(default_service)
            for rule in spec.get("rules") or []:
                http = rule.get("http") or {}
                for item in http.get("paths") or []:
                    svc = ((((item.get("backend") or {}).get("service")) or {}))
                    if svc:
                        svc = dict(svc)
                        svc["host"] = rule.get("host")
                        svc["path"] = item.get("path")
                        svc["path_type"] = item.get("pathType")
                        backends.append(svc)
            for backend in backends:
                if backend.get("name") in service_names:
                    matched = True
            if not matched:
                continue
            exposures.append({
                "type": "ingress",
                "name": (data.get("metadata") or {}).get("name"),
                "namespace": (data.get("metadata") or {}).get("namespace"),
                "source_path": _rel(path, config_root),
                "ingress_class": spec.get("ingressClassName"),
                "annotations": (data.get("metadata") or {}).get("annotations") or {},
                "rules": [
                    {
                        "host": rule.get("host"),
                        "paths": [
                            {
                                "path": p.get("path"),
                                "path_type": p.get("pathType"),
                                "service": ((((p.get("backend") or {}).get("service")) or {}).get("name")),
                                "port": (((((p.get("backend") or {}).get("service")) or {}).get("port")) or {}),
                            }
                            for p in ((rule.get("http") or {}).get("paths") or [])
                        ],
                    }
                    for rule in spec.get("rules") or []
                ],
            })
    return exposures


def discover_runtime_context_items(
    project_id: str,
    repo_url: str,
    *,
    k8s_config_root: str | None = None,
    app_name: str | None = None,
    environment: str = "prod",
    source_repo: str = "deploy-config",
) -> list[RuntimeContextItem]:
    """Discover RuntimeContextItem records from a KubeVela/GitOps repository."""
    app_name = app_name or _repo_slug(repo_url)
    config_root = Path(k8s_config_root).expanduser().resolve() if k8s_config_root else _default_k8s_config_root()
    flux_entry = _find_flux_entry(config_root, environment, app_name)
    app_dir = _flux_app_path(flux_entry, config_root) if flux_entry else None
    if app_dir is None or not app_dir.exists():
        app_dir = _find_app_dir(config_root, environment, app_name)
    if app_dir is None or not app_dir.exists():
        raise FileNotFoundError(f"未在 {config_root} 中找到 {environment} 环境的 {app_name} 部署目录")

    items: list[RuntimeContextItem] = []
    for yaml_path in sorted(app_dir.glob("*.yaml")):
        app = _safe_load_yaml(yaml_path)
        namespace = ((app.get("metadata") or {}).get("namespace") or "")
        app_meta_name = ((app.get("metadata") or {}).get("name") or yaml_path.stem)
        clusters = _clusters(app)
        objects = _objects(app)
        services = [obj for obj in objects if obj.get("kind") == "Service"]
        service_by_selector = []
        service_names = set()
        for service in services:
            service_names.add(str((service.get("metadata") or {}).get("name") or ""))
            service_by_selector.append((service, ((service.get("spec") or {}).get("selector") or {})))
        ingress_exposures = _ingress_exposures(config_root, environment, service_names)

        for workload in objects:
            kind = workload.get("kind")
            if kind not in {"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"}:
                continue
            meta = workload.get("metadata") or {}
            spec = workload.get("spec") or {}
            template_meta = ((spec.get("template") or {}).get("metadata") or {})
            labels = template_meta.get("labels") or meta.get("labels") or {}
            selector = (spec.get("selector") or {}).get("matchLabels") or labels
            container = _first_container(workload)
            pod_spec = _pod_spec(workload)
            matched_services = []
            for service, svc_selector in service_by_selector:
                if svc_selector and all(selector.get(k) == v for k, v in svc_selector.items()):
                    matched_services.append(service)
            exposures = [_service_exposure(svc) for svc in matched_services] + ingress_exposures
            command = container.get("command") or []
            if isinstance(command, str):
                command = [command]
            env = container.get("env") or []
            image = str(container.get("image") or "")
            workload_name = str(meta.get("name") or app_meta_name)
            runtime_id = re.sub(r"[^a-zA-Z0-9_.-]+", "-", workload_name).strip("-")
            item = RuntimeContextItem(
                id=runtime_id,
                project_id=project_id,
                environment=environment,
                source_type="kubevela",
                source_repo=source_repo,
                source_path=_rel(yaml_path, config_root),
                app_name=app_name,
                namespace=namespace,
                workload_type=str(kind),
                workload_name=workload_name,
                service_name=str(((matched_services[0].get("metadata") or {}).get("name")) if matched_services else ""),
                clusters=clusters,
                selector=dict(selector or {}),
                labels=dict(labels or {}),
                replicas=spec.get("replicas"),
                image=image,
                command=[str(x) for x in command],
                ports=_container_ports(container),
                resources=container.get("resources") or {},
                probes={k: container.get(k) for k in ["livenessProbe", "readinessProbe", "startupProbe"] if container.get(k)},
                log_bindings=_parse_sls_log_bindings(env),
                exposures=exposures,
                scheduling={
                    "node_selector": pod_spec.get("nodeSelector"),
                    "tolerations": pod_spec.get("tolerations"),
                    "affinity": pod_spec.get("affinity"),
                    "termination_grace_period_seconds": pod_spec.get("terminationGracePeriodSeconds"),
                },
                config={
                    "repo_url": repo_url,
                    "image_tag": _image_tag(image),
                    "container_name": container.get("name"),
                    "flux_entry": _rel(flux_entry, config_root) if flux_entry else "",
                    "app_directory": _rel(app_dir, config_root),
                },
                enabled=True,
            )
            items.append(item)
    return items


def discover_runtime_contexts(
    project_id: str,
    repo_url: str,
    k8s_config_root: str | None = None,
    app_name: str | None = None,
    environment: str = "prod",
) -> str:
    """Generate Runtime Context JSON from repo_url and local KubeVela config."""
    try:
        items = discover_runtime_context_items(
            project_id,
            repo_url,
            k8s_config_root=k8s_config_root,
            app_name=app_name,
            environment=environment,
        )
        return _json({"project_id": project_id, "repo_url": repo_url, "count": len(items), "items": [i.model_dump() for i in items]})
    except Exception as e:  # noqa: BLE001
        return _json({"project_id": project_id, "repo_url": repo_url, "error": str(e)})


def list_runtime_contexts(
    project_id: str,
    environment: str | None = None,
    cluster: str | None = None,
    keyword: str = "",
) -> str:
    items = registry.get_runtime_contexts(project_id, environment=environment, cluster=cluster, only_enabled=True)
    keyword = (keyword or "").strip().lower()
    if keyword:
        items = [
            item for item in items
            if keyword in " ".join([
                item.id,
                item.app_name,
                item.workload_name,
                item.service_name,
                " ".join(item.command),
                " ".join(item.clusters),
                _json(item.log_bindings),
            ]).lower()
        ]
    if not items:
        return f"项目 '{project_id}' 尚未注册匹配的 Runtime Context"
    rows = []
    for item in items:
        rows.append({
            "id": item.id,
            "environment": item.environment,
            "app_name": item.app_name,
            "namespace": item.namespace,
            "workload_type": item.workload_type,
            "workload_name": item.workload_name,
            "service_name": item.service_name,
            "clusters": item.clusters,
            "replicas": item.replicas,
            "selector": item.selector,
            "image": item.image,
            "command": item.command,
            "ports": item.ports,
            "log_bindings": item.log_bindings,
            "exposures": item.exposures,
            "source_path": item.source_path,
        })
    return _json(rows)


def get_runtime_context(project_id: str, runtime_id: str) -> str:
    item = registry.get_runtime_context(project_id, runtime_id)
    if not item:
        return f"Runtime Context '{runtime_id}' 在项目 '{project_id}' 中未注册"
    return _json(item.model_dump())
