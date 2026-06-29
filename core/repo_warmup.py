"""仓库预热器。

代码自省 / 脚本执行依赖两件一次性、较慢的准备工作：把仓库浅 clone 到本地缓存、为
仓库建好 venv 并安装依赖。如果留给对话里第一次用到时才做（懒加载），用户会在聊天
界面干等几分钟，体验不可接受。

本模块在**启动期**与**注册期**用后台线程把这些活并行做掉，让对话进来时常用仓库已
ready。预热是幂等的：`ensure_workspace` 已 clone 秒回，`ensure_repo_venv` 依赖指纹
未变即跳过，所以重启 / 重复触发代价很低。预热失败（无网络、pip 失败等）只记录状态，
绝不影响 HTTP、钉钉与对话本身（会退化到懒加载）。
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from loguru import logger

from core.code_sync import ensure_workspace
from core.registry import registry
from settings import code_inspection_config, repo_venv_config, repo_warmup_config
from tools.repo_venv import ensure_repo_venv

# stage: pending / cloning / building_venv / ready / failed / skipped
_status: dict[str, dict] = {}
_status_lock = threading.Lock()


def _key(project_id: str, connector_id: str) -> str:
    return f"{project_id}:{connector_id or 'default'}"


def _set_status(project_id: str, connector_id: str, stage: str, error: str = "") -> None:
    with _status_lock:
        _status[_key(project_id, connector_id)] = {
            "project_id": project_id,
            "connector_id": connector_id or "default",
            "stage": stage,
            "error": error,
            "updated_at": int(time.time()),
        }


def warmup_status() -> dict:
    """返回各仓库预热状态快照（供运维 / 可观测使用）。"""
    with _status_lock:
        return {"items": list(_status.values())}


def _repo_targets() -> list[tuple[str, str]]:
    """枚举所有需预热的 (project_id, connector_id)；connector_id='' 表示项目主仓库。"""
    targets: list[tuple[str, str]] = []
    for project in registry.list_projects():
        if project.git_url:
            targets.append((project.id, ""))
        for repo in registry.get_repository_connectors(project.id):
            if repo.git_url:
                targets.append((project.id, repo.id))
    return targets


def warmup_one(project_id: str, connector_id: str = "") -> dict:
    """预热单个仓库：clone + （可选）建 venv 装依赖。幂等、不抛异常。"""
    if not code_inspection_config.enabled:
        _set_status(project_id, connector_id, "skipped", "code_inspection 已关闭")
        return warmup_status()

    try:
        _set_status(project_id, connector_id, "cloning")
        ensure_workspace(project_id, connector_id=connector_id or None)
    except Exception as e:  # noqa: BLE001
        logger.warning("[warmup] clone 失败 project={} repo={}: {}", project_id, connector_id or "default", e)
        _set_status(project_id, connector_id, "failed", f"clone: {e}")
        return warmup_status()

    if not (repo_warmup_config.build_venv and repo_venv_config.enabled):
        _set_status(project_id, connector_id, "ready")
        return warmup_status()

    # 仓库级开关：build_venv=False 的仓库只 clone 不建 venv（无需跑脚本的 worker 仓库）。
    # 项目主仓库（connector_id 为空）无此字段，默认建 venv，保持既有行为。
    if connector_id:
        repo = registry.get_repository_connector(project_id, connector_id)
        if repo is not None and not repo.build_venv:
            logger.info("[warmup] 仓库 {}/{} build_venv=false，跳过建 venv（仅 clone）", project_id, connector_id)
            _set_status(project_id, connector_id, "ready")
            return warmup_status()

    try:
        _set_status(project_id, connector_id, "building_venv")
        res = ensure_repo_venv(project_id, install=True, connector_id=connector_id or None)
        if res.get("ok"):
            _set_status(project_id, connector_id, "ready")
        else:
            _set_status(project_id, connector_id, "failed", f"venv: {res.get('error')}")
    except Exception as e:  # noqa: BLE001
        logger.warning("[warmup] 建 venv 失败 project={} repo={}: {}", project_id, connector_id or "default", e)
        _set_status(project_id, connector_id, "failed", f"venv: {e}")
    return warmup_status()


def _run_warmup_all() -> None:
    targets = _repo_targets()
    if not targets:
        logger.info("[warmup] 没有可预热的仓库")
        return
    concurrency = max(1, int(repo_warmup_config.concurrency or 1))
    logger.info("[warmup] 开始预热 {} 个仓库，并发 {}", len(targets), concurrency)
    started = time.monotonic()
    for project_id, connector_id in targets:
        _set_status(project_id, connector_id, "pending")
    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="repo-warmup") as pool:
        for project_id, connector_id in targets:
            pool.submit(warmup_one, project_id, connector_id)
    logger.info("[warmup] 预热完成，耗时 {:.1f}s", time.monotonic() - started)


def start_background_warmup() -> None:
    """在后台 daemon 线程中并行预热全部已注册仓库，不阻塞启动。"""
    if not repo_warmup_config.enabled:
        logger.info("[warmup] repo_warmup.enabled=false，跳过启动期预热（退回懒加载）")
        return
    if not code_inspection_config.enabled:
        logger.info("[warmup] code_inspection 已关闭，跳过预热")
        return
    threading.Thread(target=_run_warmup_all, daemon=True, name="repo-warmup-all").start()


def warmup_repo_async(project_id: str, connector_id: str = "") -> None:
    """注册期对单个新仓库做增量预热（后台线程，不阻塞注册响应）。"""
    if not (repo_warmup_config.enabled and code_inspection_config.enabled):
        return
    threading.Thread(
        target=warmup_one, args=(project_id, connector_id), daemon=True,
        name=f"repo-warmup-{project_id}",
    ).start()


def warmup_project_async(project_id: str) -> None:
    """注册项目后预热其主仓库（Repository Connector 在各自注册时再增量预热）。"""
    warmup_repo_async(project_id, "")
