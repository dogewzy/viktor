"""
K8s 只读工具：查看 Pod 状态和日志。

安全设计：
- 只提供 get/list/logs 操作
- 不提供 delete/exec/scale 等写操作
- namespace 从全局配置获取
"""
from typing import Optional

from loguru import logger

from settings import k8s_config

_k8s_client_initialized = False
_core_v1 = None
_batch_v1 = None


def _ensure_k8s_client() -> None:
    """延迟初始化 K8s 客户端，避免启动时未配置导致崩溃。

    鉴权优先级：in_cluster > token 直连 > kubeconfig 文件。
    """
    global _k8s_client_initialized, _core_v1, _batch_v1
    if _k8s_client_initialized:
        return

    try:
        from kubernetes import client, config as k8s_config_loader

        if k8s_config.in_cluster:
            k8s_config_loader.load_incluster_config()
            logger.info("K8s 客户端：in-cluster 模式")
        elif k8s_config.api_server and k8s_config.token:
            _configure_token_auth(client)
            logger.info(
                "K8s 客户端：token 直连模式, server={}", k8s_config.api_server
            )
        else:
            k8s_config_loader.load_kube_config(
                config_file=k8s_config.kubeconfig_path,
                context=k8s_config.context or None,
            )
            logger.info(
                "K8s 客户端：kubeconfig 文件模式, path={}, context={}",
                k8s_config.kubeconfig_path, k8s_config.context or "<default>"
            )

        _core_v1 = client.CoreV1Api()
        _batch_v1 = client.BatchV1Api()
        _k8s_client_initialized = True
        logger.info("K8s 客户端初始化成功, namespace={}", k8s_config.namespace)
    except Exception as e:
        logger.error("K8s 客户端初始化失败, error: {}", e)
        raise


def get_core_v1():
    """返回已初始化的 CoreV1Api（按需初始化客户端）。"""
    _ensure_k8s_client()
    return _core_v1


def get_batch_v1():
    """返回已初始化的 BatchV1Api（按需初始化客户端）。"""
    _ensure_k8s_client()
    return _batch_v1


def _configure_token_auth(client_module) -> None:
    """基于 api_server + bearer token + CA 组装 kubernetes 全局配置。"""
    import base64
    import tempfile

    cfg = client_module.Configuration()
    cfg.host = k8s_config.api_server
    cfg.api_key = {"authorization": f"Bearer {k8s_config.token}"}

    if k8s_config.ca_data:
        ca_bytes = base64.b64decode(k8s_config.ca_data)
        ca_file = tempfile.NamedTemporaryFile(
            delete=False, suffix=".crt", prefix="viktor-k8s-ca-"
        )
        ca_file.write(ca_bytes)
        ca_file.close()
        cfg.ssl_ca_cert = ca_file.name
        cfg.verify_ssl = True
    else:
        cfg.verify_ssl = not k8s_config.insecure_skip_tls_verify

    client_module.Configuration.set_default(cfg)


def get_pod_status(app_label: str) -> str:
    """
    查询指定应用的 Pod 状态。

    Args:
        app_label: Pod 的 app 标签值，用于 labelSelector 过滤。

    Returns:
        格式化的 Pod 状态信息。
    """
    try:
        _ensure_k8s_client()
        pods = _core_v1.list_namespaced_pod(
            namespace=k8s_config.namespace,
            label_selector=f"app={app_label}",
        )

        if not pods.items:
            return f"未找到 app={app_label} 的 Pod"

        lines = [f"共 {len(pods.items)} 个 Pod："]
        for pod in pods.items:
            name = pod.metadata.name
            phase = pod.status.phase
            restart_count = 0
            ready_count = 0
            total_containers = len(pod.spec.containers)

            if pod.status.container_statuses:
                for cs in pod.status.container_statuses:
                    restart_count += cs.restart_count
                    if cs.ready:
                        ready_count += 1

            age = ""
            if pod.metadata.creation_timestamp:
                from datetime import datetime, timezone
                delta = datetime.now(timezone.utc) - pod.metadata.creation_timestamp
                hours = int(delta.total_seconds() // 3600)
                minutes = int((delta.total_seconds() % 3600) // 60)
                age = f"{hours}h{minutes}m"

            lines.append(
                f"  - {name} | 状态: {phase} | "
                f"就绪: {ready_count}/{total_containers} | "
                f"重启: {restart_count} | 运行: {age}"
            )
        return "\n".join(lines)

    except Exception as e:
        logger.error("查询 Pod 状态失败, app={}, error: {}", app_label, e)
        return f"查询 Pod 状态失败：{e}"


def get_pod_logs(
    app_label: str,
    lines: int = 100,
    keyword: Optional[str] = None,
) -> str:
    """
    获取指定应用的 Pod 最近日志。

    Args:
        app_label: Pod 的 app 标签值。
        lines: 获取最近多少行日志。
        keyword: 可选，按关键字过滤日志行。

    Returns:
        日志内容字符串。
    """
    try:
        _ensure_k8s_client()
        pods = _core_v1.list_namespaced_pod(
            namespace=k8s_config.namespace,
            label_selector=f"app={app_label}",
        )

        if not pods.items:
            return f"未找到 app={app_label} 的 Pod"

        pod_name = pods.items[0].metadata.name

        log_text = _core_v1.read_namespaced_pod_log(
            name=pod_name,
            namespace=k8s_config.namespace,
            tail_lines=lines,
        )

        if not log_text:
            return f"Pod {pod_name} 无日志输出"

        if keyword:
            filtered = [
                line for line in log_text.split("\n") if keyword in line
            ]
            if not filtered:
                return (
                    f"Pod {pod_name} 最近 {lines} 行日志中"
                    f"未找到包含 '{keyword}' 的内容"
                )
            return f"Pod {pod_name} 日志（关键字: {keyword}）：\n" + "\n".join(
                filtered[-50:]
            )

        log_lines = log_text.split("\n")
        if len(log_lines) > 50:
            return (
                f"Pod {pod_name} 最近日志（显示最后 50 行）：\n"
                + "\n".join(log_lines[-50:])
            )
        return f"Pod {pod_name} 日志：\n{log_text}"

    except Exception as e:
        logger.error("获取 Pod 日志失败, app={}, error: {}", app_label, e)
        return f"获取 Pod 日志失败：{e}"
