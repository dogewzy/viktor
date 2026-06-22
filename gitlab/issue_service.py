"""GitLab Issue 读写能力。"""
from __future__ import annotations

from typing import Any
from urllib.parse import quote_plus

import httpx

from gitlab.service import GitLabClient
from settings import gitlab_config


def _client_for_project(project_url: str) -> tuple[str, str, str]:
    base_url = gitlab_config.resolve_base_url(project_url)
    token = gitlab_config.token_for_base_url(base_url)
    if not token:
        raise RuntimeError(f"缺少 GitLab Token，无法访问 {base_url} 的 Issue")
    project_path = GitLabClient.extract_project_path(project_url)
    encoded = quote_plus(project_path)
    api_root = GitLabClient.normalize_base_url(base_url).rstrip("/")
    return api_root, token, encoded


def _headers(token: str) -> dict[str, str]:
    return {"PRIVATE-TOKEN": token}


def list_issues(
    *,
    project_url: str,
    state: str = "opened",
    labels: list[str] | None = None,
    per_page: int = 100,
    max_pages: int = 5,
) -> list[dict[str, Any]]:
    """列出项目 Issue。labels 使用 GitLab 逗号语义，传空则不过滤。"""
    api_root, token, encoded = _client_for_project(project_url)
    items: list[dict[str, Any]] = []
    with httpx.Client(headers=_headers(token), timeout=30.0) as client:
        for page in range(1, max_pages + 1):
            params: dict[str, Any] = {
                "state": state,
                "per_page": per_page,
                "page": page,
                "order_by": "updated_at",
                "sort": "desc",
            }
            if labels:
                params["labels"] = ",".join(labels)
            resp = client.get(f"{api_root}/api/v4/projects/{encoded}/issues", params=params)
            resp.raise_for_status()
            page_items = resp.json()
            if not page_items:
                break
            items.extend(page_items)
            if len(page_items) < per_page:
                break
    return items


def get_issue(*, project_url: str, issue_iid: int | str) -> dict[str, Any]:
    api_root, token, encoded = _client_for_project(project_url)
    iid = quote_plus(str(issue_iid))
    with httpx.Client(headers=_headers(token), timeout=30.0) as client:
        resp = client.get(f"{api_root}/api/v4/projects/{encoded}/issues/{iid}")
        resp.raise_for_status()
        return resp.json()


def create_issue(
    *,
    project_url: str,
    title: str,
    description: str,
    labels: list[str] | None = None,
    assignee_ids: list[int] | None = None,
    confidential: bool = True,
) -> dict[str, Any]:
    api_root, token, encoded = _client_for_project(project_url)
    payload: dict[str, Any] = {
        "title": title,
        "description": description,
        "confidential": confidential,
    }
    if labels:
        payload["labels"] = ",".join(labels)
    if assignee_ids:
        # 新老 GitLab 对 assignee 字段兼容性不完全一致；优先用复数数组。
        payload["assignee_ids"] = assignee_ids
    with httpx.Client(headers=_headers(token), timeout=30.0) as client:
        resp = client.post(f"{api_root}/api/v4/projects/{encoded}/issues", json=payload)
        resp.raise_for_status()
        return resp.json()


def update_issue(
    *,
    project_url: str,
    issue_iid: int | str,
    title: str | None = None,
    description: str | None = None,
    labels: list[str] | None = None,
    state_event: str | None = None,
) -> dict[str, Any]:
    api_root, token, encoded = _client_for_project(project_url)
    iid = quote_plus(str(issue_iid))
    payload: dict[str, Any] = {}
    if title is not None:
        payload["title"] = title
    if description is not None:
        payload["description"] = description
    if labels is not None:
        payload["labels"] = ",".join(labels)
    if state_event:
        payload["state_event"] = state_event
    with httpx.Client(headers=_headers(token), timeout=30.0) as client:
        resp = client.put(f"{api_root}/api/v4/projects/{encoded}/issues/{iid}", json=payload)
        resp.raise_for_status()
        return resp.json()


def close_issue(*, project_url: str, issue_iid: int | str) -> dict[str, Any]:
    return update_issue(project_url=project_url, issue_iid=issue_iid, state_event="close")


def create_issue_note(
    *,
    project_url: str,
    issue_iid: int | str,
    body: str,
) -> dict[str, Any]:
    api_root, token, encoded = _client_for_project(project_url)
    iid = quote_plus(str(issue_iid))
    with httpx.Client(headers=_headers(token), timeout=30.0) as client:
        resp = client.post(
            f"{api_root}/api/v4/projects/{encoded}/issues/{iid}/notes",
            json={"body": body},
        )
        resp.raise_for_status()
        return resp.json()


def add_issue_labels(
    *,
    project_url: str,
    issue_iid: int | str,
    labels: list[str],
) -> dict[str, Any]:
    issue = get_issue(project_url=project_url, issue_iid=issue_iid)
    current = [str(item) for item in issue.get("labels") or []]
    merged = list(dict.fromkeys([*current, *labels]))
    return update_issue(project_url=project_url, issue_iid=issue_iid, labels=merged)
