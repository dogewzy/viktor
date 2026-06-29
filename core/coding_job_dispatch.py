"""每任务一个 K8s Job：coding task 的执行从 web pod 线程迁出，跑在独立 Job 里。

设计要点（详见计划）：
- 复用 viktor 镜像，入口换成 `python -m core.coding_job_runner <task_id> <mode>`。
- 状态/control 全走 MySQL，Job 自治；web 只读库渲染，不与 Job 流式对接。
- Job 不属于任何 Deployment → 对 web 的滚动重启完全免疫（本方案核心目标）。

鉴权复用 tools/k8s_tool 的单点初始化（in_cluster > token > kubeconfig）。
"""
from __future__ import annotations

import os
import re
from typing import Any, Optional

from loguru import logger

from settings import coding_agent_config, k8s_config
from tools.k8s_tool import get_batch_v1, get_core_v1

JOB_APP_LABEL = "viktor-coding-job"
_TASK_ID_LABEL = "viktor/task-id"
_MODE_LABEL = "viktor/mode"


class JobConcurrencyError(RuntimeError):
    """活跃 Job 数已达 job_concurrency_limit，暂不派发（调用方置「排队中」由 sweep 重投）。"""


def _job_namespace() -> str:
    configured = (coding_agent_config.job_namespace or "").strip()
    if configured:
        return configured
    fallback = (k8s_config.namespace or "").strip()
    if k8s_config.in_cluster and "K8S_NAMESPACE" not in os.environ:
        # In-cluster workloads usually want to create Jobs next to the web pod.
        # The config default is "default", which is easy to forget to override.
        try:
            with open("/var/run/secrets/kubernetes.io/serviceaccount/namespace", encoding="utf-8") as f:
                current = f.read().strip()
            if current:
                return current
        except OSError:
            pass
    return fallback or "default"


def _mode_suffix(mode: str) -> str:
    return "plan" if mode == "planning" else "exec"


def job_name(task_id: str, mode: str) -> str:
    """确定性 Job 名（DNS-1123，≤63）。task_id 形如 ct_<16hex>，含非法 '_'。"""
    safe = re.sub(r"[^a-z0-9-]+", "-", task_id.lower()).strip("-")
    name = f"viktor-{safe}-{_mode_suffix(mode)}"
    return name[:63].rstrip("-")


def _self_pod_name() -> str:
    """本 web pod 名（容器内 /etc/hostname == pod 名；退回 HOSTNAME）。"""
    try:
        name = open("/etc/hostname", encoding="utf-8").read().strip()
    except OSError:
        name = ""
    return name or os.environ.get("HOSTNAME", "").strip()


def _self_container() -> Any:
    """读本 pod 的 viktor 容器 spec（取 image 与 env，确保 Job 与 web 一致，需 pods get 权限）。"""
    pod_name = _self_pod_name()
    if not pod_name:
        raise RuntimeError("取不到本 pod 名（/etc/hostname 与 HOSTNAME 均空）")
    pod = get_core_v1().read_namespaced_pod(name=pod_name, namespace=_job_namespace())
    containers = pod.spec.containers or []
    for c in containers:
        if c.name == "viktor":
            return c
    if not containers:
        raise RuntimeError("无法读取本 pod 容器 spec")
    return containers[0]


def resolve_job_image() -> str:
    """解析 Job 容器镜像，确保 = 发起时 web 自身镜像（部署后代码一致）。

    优先级：配置 job_image > 环境变量 VIKTOR_JOB_IMAGE > 自查本 pod 镜像。
    """
    if coding_agent_config.job_image:
        return coding_agent_config.job_image
    env_image = os.environ.get("VIKTOR_JOB_IMAGE", "").strip()
    if env_image:
        return env_image
    image = _self_container().image
    if not image:
        raise RuntimeError("无法解析 Job 镜像：本 pod 容器 image 为空")
    return image


# Job 自己覆盖的 env，从 web pod 继承时跳过（避免重复 key）。
_ENV_OVERRIDE = {"K8S_IN_CLUSTER", "K8S_NAMESPACE", "PYTHONUNBUFFERED"}


def _inherited_env() -> list[dict[str, Any]]:
    """继承 web 本 pod 的 env（含 secretKeyRef 映射），序列化为 manifest dict。

    关键：viktor-secrets 的 key 是横杠命名（hiart-api-key），不能用 envFrom 平铺
    ——会变成非法 env 名被 K8s 丢弃，导致 HIART_API_KEY 等为空 → Missing credentials。
    直接复制 web pod 已逐个 secretKeyRef 映射好的 env，永不漂移。
    """
    from kubernetes.client import ApiClient

    serializer = ApiClient()
    out: list[dict[str, Any]] = []
    for env_var in (_self_container().env or []):
        if env_var.name in _ENV_OVERRIDE:
            continue
        out.append(serializer.sanitize_for_serialization(env_var))
    return out


def _inherited_image_pull_secrets() -> list[dict[str, Any]]:
    """继承 web 本 pod 的 imagePullSecrets：私有 registry 拉镜像必需，否则 ImagePullBackOff。"""
    pod_name = _self_pod_name()
    if not pod_name:
        return []
    pod = get_core_v1().read_namespaced_pod(name=pod_name, namespace=_job_namespace())
    secrets = pod.spec.image_pull_secrets or []
    return [{"name": s.name} for s in secrets if s.name]


def build_job_manifest(task_id: str, mode: str) -> dict[str, Any]:
    """构造 Job manifest（dict 形式，直接喂给 create_namespaced_job）。"""
    cfg = coding_agent_config
    name = job_name(task_id, mode)
    labels = {
        "app": JOB_APP_LABEL,
        _TASK_ID_LABEL: task_id,
        _MODE_LABEL: mode,
    }
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": name,
            "namespace": _job_namespace(),
            "labels": labels,
        },
        "spec": {
            "backoffLimit": 0,                              # 不重试，失败即终态（resume 显式）
            "activeDeadlineSeconds": cfg.task_timeout_sec,  # 硬超时，与线程时代一致
            "ttlSecondsAfterFinished": cfg.job_ttl_seconds,
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "restartPolicy": "Never",
                    "serviceAccountName": cfg.job_service_account,
                    "imagePullSecrets": _inherited_image_pull_secrets(),
                    "containers": [{
                        "name": "runner",
                        "image": resolve_job_image(),
                        "command": [
                            "python", "-m", "core.coding_job_runner", task_id, mode,
                        ],
                        # 继承 web pod 的 env（逐个 secretKeyRef 映射，含 HIART/DEEPSEEK 等 LLM key）
                        # + Job 自己的运行时 env。不用 envFrom：secret key 是横杠命名，平铺会被丢弃。
                        "env": _inherited_env() + [
                            {"name": "K8S_IN_CLUSTER", "value": "true"},
                            {"name": "K8S_NAMESPACE", "value": _job_namespace()},
                            {"name": "PYTHONUNBUFFERED", "value": "1"},
                        ],
                        "resources": cfg.job_resources,
                        "volumeMounts": [{
                            "name": "viktor-cache",
                            "mountPath": "/var/cache/viktor",
                        }],
                    }],
                    "volumes": [{
                        "name": "viktor-cache",
                        "persistentVolumeClaim": {"claimName": cfg.job_pvc_name},
                    }],
                },
            },
        },
    }


def _list_jobs(label_selector: str) -> list[Any]:
    resp = get_batch_v1().list_namespaced_job(
        namespace=_job_namespace(), label_selector=label_selector,
    )
    return list(resp.items or [])


def _is_active(job: Any) -> bool:
    st = job.status
    if st is None:
        return True  # 刚创建、status 未填，视为活跃
    if (st.succeeded or 0) > 0 or (st.failed or 0) > 0:
        return False
    return True


def count_active_jobs() -> int:
    """活跃 coding Job 数（并发闸）。"""
    return sum(1 for j in _list_jobs(f"app={JOB_APP_LABEL}") if _is_active(j))


def find_active_job(task_id: str, mode: Optional[str] = None) -> Optional[Any]:
    selector = f"{_TASK_ID_LABEL}={task_id}"
    if mode:
        selector += f",{_MODE_LABEL}={mode}"
    for j in _list_jobs(selector):
        if _is_active(j):
            return j
    return None


def job_exists_for_task(task_id: str) -> bool:
    """该 task 是否还有 Job 对象（活跃 或 在 TTL 内未被 GC）。reconcile 用。"""
    return bool(_list_jobs(f"{_TASK_ID_LABEL}={task_id}"))


def _read_job(name: str) -> Optional[Any]:
    from kubernetes.client.rest import ApiException
    try:
        return get_batch_v1().read_namespaced_job(name=name, namespace=_job_namespace())
    except ApiException as e:
        if e.status == 404:
            return None
        raise


def delete_job(task_id: str, mode: str, *, propagation: str = "Background") -> bool:
    """删除 Job（连带 pod）。硬取消用。容忍 404。"""
    from kubernetes.client.rest import ApiException
    name = job_name(task_id, mode)
    try:
        get_batch_v1().delete_namespaced_job(
            name=name, namespace=_job_namespace(), propagation_policy=propagation,
        )
        logger.info("[coding-job] 已删除 Job {}", name)
        return True
    except ApiException as e:
        if e.status == 404:
            return False
        raise


def create_coding_job(task_id: str, mode: str) -> str:
    """为 (task_id, mode) 创建 Job；返回 Job 名。幂等：已活跃则直接返回。

    并发已满抛 JobConcurrencyError（调用方置「排队中」，由周期 sweep 重投）。
    """
    from kubernetes.client.rest import ApiException

    if mode not in ("planning", "execution"):
        raise ValueError(f"非法 mode: {mode}")

    name = job_name(task_id, mode)
    existing = _read_job(name)
    if existing is not None:
        if _is_active(existing):
            logger.info("[coding-job] Job {} 已活跃，幂等返回", name)
            return name
        # 已结束（succeeded/failed）但还在 TTL 内：先删再重建，复用确定性命名。
        delete_job(task_id, mode, propagation="Foreground")
        # 等待删除完成；Foreground 下 read 仍可能短暂可见，重建用 try/409 兜底。

    if count_active_jobs() >= coding_agent_config.job_concurrency_limit:
        raise JobConcurrencyError(
            f"活跃 Job 数已达上限 {coding_agent_config.job_concurrency_limit}"
        )

    manifest = build_job_manifest(task_id, mode)
    try:
        get_batch_v1().create_namespaced_job(namespace=_job_namespace(), body=manifest)
        logger.info("[coding-job] 已创建 Job {} (task={}, mode={})", name, task_id, mode)
    except ApiException as e:
        if e.status == 409:
            # 同名仍在终止中：视为已派发（幂等）。
            logger.warning("[coding-job] Job {} 已存在(409)，视为已派发", name)
            return name
        raise
    return name
