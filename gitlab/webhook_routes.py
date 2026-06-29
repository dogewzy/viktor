"""GitLab Webhook 路由。

这些入口由 GitLab 服务器调用，不能依赖 Viktor 网页 Bearer Token；
仅使用 GitLab webhook secret 校验。
"""
from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Request
from loguru import logger

from core.coding_service import complete_code_review_by_merge_request
from core.temporal import trigger
from settings import gitlab_config

router = APIRouter(prefix="/api/v1/gitlab", tags=["GitLab Webhook"])


def _resolve_task_id(attrs: dict) -> str:
    branch = str(attrs.get("source_branch") or "")
    return branch[len("viktor/"):] if branch.startswith("viktor/") else ""


def _mr_state(attrs: dict) -> str:
    action = str(attrs.get("action") or "").lower()
    state = str(attrs.get("state") or "").lower()
    if action == "merge" or state == "merged":
        return "merged"
    if action == "close" or state == "closed":
        return "closed"
    return ""


@router.post("/webhooks/merge-request", summary="接收 GitLab MR webhook")
async def receive_merge_request_webhook(
    request: Request,
    x_gitlab_token: str = Header(default=""),
) -> dict:
    if gitlab_config.webhook_secret and x_gitlab_token != gitlab_config.webhook_secret:
        raise HTTPException(status_code=403, detail="invalid GitLab webhook token")
    payload = await request.json()
    if payload.get("object_kind") != "merge_request":
        return {"ok": True, "ignored": True}

    # 接管模式：把 MR 终态作为 signal 发给对应 CodingTaskWorkflow，由它 + 父流程驱动收尾。
    if trigger.enabled():
        attrs = payload.get("object_attributes") if isinstance(payload.get("object_attributes"), dict) else {}
        task_id = _resolve_task_id(attrs)
        state = _mr_state(attrs)
        action = str(attrs.get("action") or "").lower()
        if task_id and state:
            mr_url = str(attrs.get("url") or attrs.get("web_url") or "")
            try:
                await trigger.asignal_coding_task(task_id, "mr_state_changed", state, mr_url)
                logger.info("[GitLab webhook] signaled mr_state_changed task={} state={}", task_id, state)
                return {"ok": True, "signaled": True, "task_id": task_id, "state": state}
            except Exception as e:  # noqa: BLE001 - 回退旧路径，避免漏处理
                logger.warning("[GitLab webhook] signal 失败，回退旧路径 task={}: {}", task_id, e)
        if task_id and action in {"update", "reopen"}:
            try:
                from core.staging_acceptance_service import handle_task_mr_updated
                result = handle_task_mr_updated(task_id)
                logger.info("[GitLab webhook] staging reset task={} action={}", task_id, action)
                return {"ok": True, "staging_reset": True, "task_id": task_id, **result}
            except Exception as e:  # noqa: BLE001
                logger.warning("[GitLab webhook] staging reset failed task={}: {}", task_id, e)

    result = complete_code_review_by_merge_request(payload)
    logger.info("[GitLab webhook] merge_request result={}", result)
    return {"ok": True, **result}
