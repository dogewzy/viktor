"""
代码自省（一期）· 代码同步 Worker。

职责：
1. 从 K8s Deployment/StatefulSet 读当前线上 image，反查 commit sha（真相源）；
2. 按需把Repository Connector clone/checkout 到本地缓存目录，返回 workspace 路径；
3. 并发安全（文件锁）+ 磁盘受限（每项目 LRU 只保留最近 N 个 commit）。

**懒加载**：不做启动期批量 pull，第一次用到该项目时才 clone。
"""
from __future__ import annotations

import base64
import fcntl
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urlparse

import httpx
from loguru import logger

from core.registry import ProjectItem, K8sWorkloadRef, registry
from settings import code_inspection_config, gitlab_config, k8s_config


# ============================================================
# 常量 / 工具
# ============================================================

_SHA_SHORT = re.compile(r"^[0-9a-f]{7,40}$")
_SHA_LONG = re.compile(r"^[0-9a-f]{40}$")


@dataclass
class CommitResolution:
    """image→commit 解析结果。"""
    sha: str
    source: str           # "image_tag" / "image_label" / "default_branch"
    raw_image: str = ""
    branch: Optional[str] = None

    def short(self) -> str:
        return self.sha[:12]


# ============================================================
# image → commit 解析
# ============================================================

def _load_apps_v1():
    """延迟初始化 K8s AppsV1Api（与 tools/k8s_tool.py 共享鉴权逻辑）。"""
    from tools.k8s_tool import _ensure_k8s_client  # noqa: WPS437
    from kubernetes import client

    _ensure_k8s_client()
    return client.AppsV1Api()


def _read_image_from_workload(wl: K8sWorkloadRef) -> str:
    """从 Deployment/StatefulSet 读 container image。多容器时按 container 名匹配，否则取第一个。"""
    apps_v1 = _load_apps_v1()
    if wl.kind == "StatefulSet":
        obj = apps_v1.read_namespaced_stateful_set(name=wl.name, namespace=wl.namespace)
    else:
        obj = apps_v1.read_namespaced_deployment(name=wl.name, namespace=wl.namespace)

    containers = obj.spec.template.spec.containers or []
    if not containers:
        raise RuntimeError(f"{wl.kind}/{wl.name} 没有可用 container")

    if wl.container:
        for c in containers:
            if c.name == wl.container:
                return c.image
        raise RuntimeError(
            f"{wl.kind}/{wl.name} 未找到 container={wl.container}，"
            f"现有 containers={[c.name for c in containers]}"
        )
    return containers[0].image


def _split_image_tag(image: str) -> tuple[str, str]:
    """拆分 image 为 (repo, tag)。支持 repo@sha256:... 以及 repo:tag。"""
    if "@" in image:
        repo, digest = image.rsplit("@", 1)
        return repo, digest  # digest 形如 sha256:xxx
    # 区分 port 与 tag：只把最后一段 : 之后的识别为 tag
    if ":" in image.rsplit("/", 1)[-1]:
        repo, _, tag = image.rpartition(":")
        return repo, tag
    return image, "latest"


def _try_registry_label(image: str) -> Optional[str]:
    """尝试从 docker registry manifest 读 label 'org.opencontainers.image.revision'。

    一期实现：只在显式配置 registry 访问凭证时尝试；不做就返回 None 走兜底。
    为避免把一期工作量放大，这里只解析 sha256 digest，不访问网络。
    """
    repo, tag = _split_image_tag(image)
    if tag.startswith("sha256:"):
        # digest 本身不是 commit，无法直接映射；需要 registry manifest，一期先跳过
        return None
    return None


def _resolve_default_branch_commit(git_url: str, branch: str) -> str:
    """用 `git ls-remote` 拿远端分支最新 commit，本地不 clone。"""
    url_with_auth = _inject_git_credentials(git_url)
    cmd = [code_inspection_config.git_binary, "ls-remote", url_with_auth, f"refs/heads/{branch}"]
    logger.debug("[code_sync] ls-remote: {} {}", code_inspection_config.git_binary, branch)
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=30,
        env=_git_env(),
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git ls-remote 失败 (branch={branch}): {result.stderr.strip()}"
        )
    line = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
    if not line:
        raise RuntimeError(f"分支 {branch} 不存在于远端 {git_url}")
    return line.split()[0]


def resolve_live_commit(project: ProjectItem) -> CommitResolution:
    """根据 project.k8s_workload 读线上 image，反查 commit sha。

    优先级：
      1. image tag 本身是 7/40 位 hex → 视为 commit
      2. registry manifest label 'org.opencontainers.image.revision'（一期未启用）
      3. 兜底：git ls-remote origin refs/heads/<default_branch>
    """
    if not project.git_url:
        raise RuntimeError(f"项目 {project.id} 未配置 git_url，无法启用代码自省")

    image = ""
    if project.k8s_workload:
        try:
            image = _read_image_from_workload(project.k8s_workload)
        except Exception as e:
            logger.warning(
                "[code_sync] 读取 {}/{}/{} image 失败，退化到 default_branch: {}",
                project.k8s_workload.namespace, project.k8s_workload.kind,
                project.k8s_workload.name, e,
            )

    if image:
        _, tag = _split_image_tag(image)
        if _SHA_SHORT.match(tag):
            # 7/40 位 hex 直接当 commit
            if _SHA_LONG.match(tag):
                return CommitResolution(sha=tag, source="image_tag", raw_image=image)
            # 短 sha：后续 ensure_workspace 做 fetch + rev-parse 补全
            return CommitResolution(sha=tag, source="image_tag", raw_image=image)

        label_sha = _try_registry_label(image)
        if label_sha:
            return CommitResolution(sha=label_sha, source="image_label", raw_image=image)

    # 兜底：default_branch 最新 commit
    branch = project.default_branch or "master"
    logger.warning(
        "[code_sync] project={} image={} 无 commit 标识，兜底到 {} 最新",
        project.id, image or "<none>", branch,
    )
    sha = _resolve_default_branch_commit(project.git_url, branch)
    return CommitResolution(sha=sha, source="default_branch", raw_image=image, branch=branch)


# ============================================================
# git 凭证注入
# ============================================================

def _inject_git_credentials(git_url: str) -> str:
    """把 GitLab token 注入 https URL；git@ / ssh 协议直接原样返回（依赖 SSH key）。"""
    token = gitlab_config.token_for_repo_url(git_url)
    if not token:
        return git_url
    raw = gitlab_config.access_url_for_repo_url(git_url)
    if raw.startswith("git@") or raw.startswith("ssh://"):
        return raw
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return raw
    if "@" in parsed.netloc:
        # URL 里已有凭证，不覆盖
        return raw
    auth = f"oauth2:{quote(token, safe='')}"
    new_netloc = f"{auth}@{parsed.netloc}"
    return parsed._replace(netloc=new_netloc).geturl()


def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    env.setdefault("GIT_ASKPASS", "/bin/true")
    return env


def _mask_secret_arg(arg: str) -> str:
    return re.sub(r"(https?://[^:\s/]+:)[^@\s]+@", r"\1<redacted>@", arg)


def _masked_git_args(args: list[str]) -> str:
    return " ".join(_mask_secret_arg(a) for a in args)


# ============================================================
# workspace 目录管理
# ============================================================

_INFLIGHT_LOCK = threading.Lock()
_INFLIGHT: dict[str, threading.Lock] = {}


def _per_project_lock(project_id: str) -> threading.Lock:
    with _INFLIGHT_LOCK:
        lock = _INFLIGHT.get(project_id)
        if lock is None:
            lock = threading.Lock()
            _INFLIGHT[project_id] = lock
        return lock


def _cache_root() -> Path:
    root = Path(os.path.expanduser(code_inspection_config.cache_root)).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _project_dir(project_id: str) -> Path:
    d = _cache_root() / project_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _repo_dir(project_id: str, connector_id: str) -> Path:
    d = _project_dir(project_id) / connector_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _workspace_path(project_id: str, sha: str, connector_id: str = "") -> Path:
    if connector_id:
        return _repo_dir(project_id, connector_id) / sha
    return _project_dir(project_id) / sha


def _is_valid_workspace(path: Path) -> bool:
    return path.is_dir() and (path / ".git").exists()


def _run_git(args: list[str], cwd: Optional[Path] = None, timeout: Optional[int] = None) -> None:
    cmd = [code_inspection_config.git_binary, *args]
    logger.debug("[code_sync] git {}", _masked_git_args(args))
    res = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout or code_inspection_config.clone_timeout_sec,
        env=_git_env(),
    )
    if res.returncode != 0:
        raise RuntimeError(f"git {args[0]} 失败: {res.stderr.strip() or res.stdout.strip()}")


def _clone_commit(git_url: str, commit_sha: str, dst: Path, branch: Optional[str] = None) -> str:
    """clone 到 dst 并 checkout 指定 commit。返回最终完整 sha。"""
    url = _inject_git_credentials(git_url)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        shutil.rmtree(dst, ignore_errors=True)

    # default_branch 场景下 commit_sha 来自 ls-remote(refs/heads/<branch>)。
    # 直接浅克隆该分支可避免老 GitLab 忽略 --filter=blob:none 后拉取全量 pack。
    if branch:
        try:
            _run_git([
                "clone",
                "--depth", "1",
                "--single-branch",
                "--branch", branch,
                "--no-checkout",
                url,
                str(dst),
            ])
            _run_git(["checkout", "--detach", commit_sha], cwd=dst)
        except RuntimeError as e:
            logger.warning(
                "[code_sync] shallow branch clone 失败，回退到 commit fetch: branch={} commit={} error={}",
                branch, commit_sha[:12], e,
            )
            shutil.rmtree(dst, ignore_errors=True)
        else:
            res = subprocess.run(
                [code_inspection_config.git_binary, "rev-parse", "HEAD"],
                cwd=str(dst), capture_output=True, text=True, timeout=15, env=_git_env(),
            )
            return res.stdout.strip() if res.returncode == 0 else commit_sha

    # 通用路径：先 clone，再 fetch 指定 commit。部分 GitLab 会忽略 --filter=blob:none，
    # 因此 default_branch 情况应优先走上面的 shallow branch clone。
    _run_git(["clone", "--filter=blob:none", "--no-checkout", url, str(dst)])
    try:
        _run_git(["fetch", "--depth", "1", "origin", commit_sha], cwd=dst)
    except RuntimeError:
        # 有些 server 不允许按 sha fetch，退化为全量 fetch
        _run_git(["fetch", "--unshallow"], cwd=dst)
    _run_git(["checkout", "--detach", commit_sha], cwd=dst)

    # 取规范化 sha（补齐短 sha）
    res = subprocess.run(
        [code_inspection_config.git_binary, "rev-parse", "HEAD"],
        cwd=str(dst), capture_output=True, text=True, timeout=15, env=_git_env(),
    )
    full_sha = res.stdout.strip() if res.returncode == 0 else commit_sha
    return full_sha


def _lru_cleanup(project_id: str, keep_sha: str) -> None:
    """按目录 mtime 保留最近 N 个 commit workspace。"""
    project_dir = _project_dir(project_id)
    entries = [
        p for p in project_dir.iterdir()
        if p.is_dir() and p.name != keep_sha and _is_valid_workspace(p)
    ]
    entries.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    keep = max(code_inspection_config.max_commits_per_repo - 1, 0)
    for stale in entries[keep:]:
        logger.info("[code_sync] LRU 清理旧 workspace: {}", stale)
        shutil.rmtree(stale, ignore_errors=True)


def ensure_workspace(
    project_id: str,
    commit_sha: Optional[str] = None,
    connector_id: Optional[str] = None,
) -> Path:
    """确保项目代码在本地可用，返回 workspace 路径。

    - 只读：不写入，单纯检出
    - commit_sha=None 时自动解析 live commit
    - connector_id=None 时使用项目主 git_url；
      指定 connector_id 时查找 Repository Connector 中的对应仓库
    - 已存在则直接返回；不存在则 clone+checkout
    - 并发安全：同一 (project, commit) 用进程内锁 + 目录 file lock 防竞态
    """
    if not code_inspection_config.enabled:
        raise RuntimeError("code_inspection.enabled=false，代码自省已关闭")

    project = registry.get_project(project_id)
    if not project:
        raise RuntimeError(f"项目 {project_id} 未注册")

    git_url: Optional[str] = None
    k8s_workload: Optional[K8sWorkloadRef] = None
    default_branch = "master"
    effective_connector_id = connector_id or ""

    if connector_id:
        repo = registry.get_repository_connector(project_id, connector_id)
        if repo:
            git_url = repo.git_url
            k8s_workload = repo.k8s_workload
            default_branch = repo.default_branch
        else:
            raise RuntimeError(f"项目 {project_id} 下未找到仓库 {connector_id}")
    else:
        git_url = project.git_url
        k8s_workload = project.k8s_workload
        default_branch = project.default_branch

    if not git_url:
        raise RuntimeError(f"项目 {project_id} 未配置 git_url，无法启用代码自省")

    if commit_sha is None:
        temp_project = ProjectItem(
            id=project_id, name=project.name, description=project.description,
            git_url=git_url, default_branch=default_branch, k8s_workload=k8s_workload,
        )
        resolution = resolve_live_commit(temp_project)
        commit_sha = resolution.sha
        resolved_branch = resolution.branch if resolution.source == "default_branch" else None
        logger.info(
            "[code_sync] 解析 live commit: project={} repo={} source={} sha={} image={}",
            project_id, effective_connector_id or "default",
            resolution.source, resolution.short(), resolution.raw_image or "<none>",
        )
    else:
        resolved_branch = None

    ws = _workspace_path(project_id, commit_sha, effective_connector_id)
    if _is_valid_workspace(ws):
        ws.touch(exist_ok=True)
        return ws

    lock_key = f"{project_id}:{effective_connector_id}" if effective_connector_id else project_id
    lock = _per_project_lock(lock_key)
    with lock:
        if _is_valid_workspace(ws):
            return ws

        parent_dir = _repo_dir(project_id, effective_connector_id) if effective_connector_id else _project_dir(project_id)
        lock_file = parent_dir / f".lock-{commit_sha[:16]}"
        with open(lock_file, "w") as lf:
            try:
                fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
                if _is_valid_workspace(ws):
                    return ws
                start = time.time()
                logger.info(
                    "[code_sync] clone project={} repo={} commit={} url={}",
                    project_id, effective_connector_id or "default", commit_sha[:12], git_url,
                )
                full_sha = _clone_commit(git_url, commit_sha, ws, branch=resolved_branch)
                if full_sha != commit_sha:
                    target = _workspace_path(project_id, full_sha, effective_connector_id)
                    if target.exists():
                        shutil.rmtree(ws, ignore_errors=True)
                    else:
                        ws.rename(target)
                    ws = target
                elapsed = time.time() - start
                logger.info(
                    "[code_sync] clone 完成 project={} repo={} commit={} 耗时 {:.1f}s",
                    project_id, effective_connector_id or "default", full_sha[:12], elapsed,
                )
            finally:
                try:
                    fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass

        try:
            _lru_cleanup(project_id, ws.name)
        except Exception as e:
            logger.warning("[code_sync] LRU 清理失败: {}", e)

        return ws


def current_workspace_info(project_id: str) -> Optional[dict]:
    """返回当前 project 下已有的 workspace 列表（最近使用在前）。供管理 API / 运维前端展示。"""
    project_dir = _project_dir(project_id)
    if not project_dir.exists():
        return None
    entries = [
        p for p in project_dir.iterdir()
        if p.is_dir() and _is_valid_workspace(p)
    ]
    entries.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return {
        "project_id": project_id,
        "cache_root": str(_cache_root()),
        "workspaces": [
            {"commit": p.name, "path": str(p), "mtime": p.stat().st_mtime}
            for p in entries
        ],
    }


# 静默占位：未来接入 registry manifest label 解析时替换
_ = httpx  # 引入 httpx 留作后续 registry API 使用，避免依赖丢失
_ = base64
