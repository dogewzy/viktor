"""GitLab Merge Request 写入能力。"""
from __future__ import annotations

import time
from typing import Any, Callable, TypeVar
from urllib.parse import quote_plus

import httpx
from loguru import logger

from gitlab.service import GitLabClient
from settings import gitlab_config

_T = TypeVar("_T")

# 重试退避（秒），固定值不用随机（subagent 环境可能禁随机）。
_RETRY_BACKOFF = (1, 2, 4)
# 触发退避重试的 5xx 状态码。
_RETRYABLE_STATUS = {500, 502, 503, 504}


def _with_retry(fn: Callable[[], _T], *, op_name: str = "") -> _T:
    """执行无参可调用并按需重试。

    重试规则：
    - httpx.HTTPStatusError：429 → 读 Retry-After（秒）后重试，无则指数退避；
      5xx（500/502/503/504）→ 指数退避重试；其它 4xx（尤其 409）不重试，直接抛。
    - httpx.RequestError（网络/超时）→ 指数退避重试。
    最多重试 len(_RETRY_BACKOFF) 次，耗尽抛最后一次异常。
    """
    last_exc: Exception | None = None
    attempts = len(_RETRY_BACKOFF)
    for i in range(attempts + 1):
        try:
            return fn()
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            if status == 429:
                last_exc = e
                if i >= attempts:
                    break
                retry_after = e.response.headers.get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after else _RETRY_BACKOFF[i]
                except (TypeError, ValueError):
                    delay = _RETRY_BACKOFF[i]
                logger.warning(
                    "[gitlab-retry] {} 命中 429，{}s 后重试（第 {}/{} 次）",
                    op_name or "request", delay, i + 1, attempts,
                )
                time.sleep(delay)
                continue
            if status in _RETRYABLE_STATUS:
                last_exc = e
                if i >= attempts:
                    break
                delay = _RETRY_BACKOFF[i]
                logger.warning(
                    "[gitlab-retry] {} 命中 {}，{}s 后重试（第 {}/{} 次）",
                    op_name or "request", status, delay, i + 1, attempts,
                )
                time.sleep(delay)
                continue
            # 其它 4xx（含 409）不重试。
            raise
        except httpx.RequestError as e:
            last_exc = e
            if i >= attempts:
                break
            delay = _RETRY_BACKOFF[i]
            logger.warning(
                "[gitlab-retry] {} 网络错误 {}，{}s 后重试（第 {}/{} 次）",
                op_name or "request", type(e).__name__, delay, i + 1, attempts,
            )
            time.sleep(delay)
            continue
    assert last_exc is not None
    raise last_exc


def create_merge_request(
    *,
    repo_url: str,
    source_branch: str,
    target_branch: str,
    title: str,
    description: str,
) -> dict[str, Any]:
    base_url = gitlab_config.resolve_base_url(repo_url)
    token = gitlab_config.token_for_base_url(base_url)
    if not token:
        raise RuntimeError(f"缺少 GitLab Token，无法为 {base_url} 创建 Merge Request")
    project_path = GitLabClient.extract_project_path(repo_url)
    encoded = quote_plus(project_path)
    url = f"{GitLabClient.normalize_base_url(base_url)}/api/v4/projects/{encoded}/merge_requests"

    def _call() -> dict[str, Any]:
        with httpx.Client(headers={"PRIVATE-TOKEN": token}, timeout=30.0) as client:
            resp = client.post(
                url,
                json={
                    "source_branch": source_branch,
                    "target_branch": target_branch,
                    "title": title,
                    "description": description,
                    "remove_source_branch": True,
                },
            )
            resp.raise_for_status()
            return resp.json()

    return _with_retry(_call, op_name="create_merge_request")


def get_merge_request(
    *,
    repo_url: str,
    merge_request_iid: int | str,
) -> dict[str, Any]:
    base_url = gitlab_config.resolve_base_url(repo_url)
    token = gitlab_config.token_for_base_url(base_url)
    if not token:
        raise RuntimeError(f"缺少 GitLab Token，无法查询 {base_url} 的 Merge Request")
    project_path = GitLabClient.extract_project_path(repo_url)
    encoded = quote_plus(project_path)
    iid = quote_plus(str(merge_request_iid))
    url = f"{GitLabClient.normalize_base_url(base_url)}/api/v4/projects/{encoded}/merge_requests/{iid}"

    def _call() -> dict[str, Any]:
        with httpx.Client(headers={"PRIVATE-TOKEN": token}, timeout=30.0) as client:
            resp = client.get(url)
            resp.raise_for_status()
            return resp.json()

    return _with_retry(_call, op_name="get_merge_request")


def create_merge_request_note(
    *,
    repo_url: str,
    merge_request_iid: int | str,
    body: str,
) -> dict[str, Any]:
    base_url = gitlab_config.resolve_base_url(repo_url)
    token = gitlab_config.token_for_base_url(base_url)
    if not token:
        raise RuntimeError(f"缺少 GitLab Token，无法为 {base_url} 评论 Merge Request")
    project_path = GitLabClient.extract_project_path(repo_url)
    encoded = quote_plus(project_path)
    iid = quote_plus(str(merge_request_iid))
    url = f"{GitLabClient.normalize_base_url(base_url)}/api/v4/projects/{encoded}/merge_requests/{iid}/notes"

    def _call() -> dict[str, Any]:
        with httpx.Client(headers={"PRIVATE-TOKEN": token}, timeout=30.0) as client:
            resp = client.post(url, json={"body": body})
            resp.raise_for_status()
            return resp.json()

    return _with_retry(_call, op_name="create_merge_request_note")


def list_open_merge_requests_by_source_branch(*, repo_url: str, source_branch: str) -> list:
    """GET /projects/:id/merge_requests?source_branch=X&state=opened，返回 MR 列表。"""
    base_url = gitlab_config.resolve_base_url(repo_url)
    token = gitlab_config.token_for_base_url(base_url)
    if not token:
        raise RuntimeError(f"缺少 GitLab Token，无法查询 {base_url} 的 Merge Request")
    project_path = GitLabClient.extract_project_path(repo_url)
    encoded = quote_plus(project_path)
    url = f"{GitLabClient.normalize_base_url(base_url)}/api/v4/projects/{encoded}/merge_requests"

    def _call() -> list:
        with httpx.Client(headers={"PRIVATE-TOKEN": token}, timeout=30.0) as client:
            resp = client.get(url, params={"source_branch": source_branch, "state": "opened"})
            resp.raise_for_status()
            return resp.json()

    return _with_retry(_call, op_name="list_open_merge_requests_by_source_branch")
