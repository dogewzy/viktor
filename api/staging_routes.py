"""Staging acceptance API."""
from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from core.auth import CurrentUser, get_current_user
from core.staging_acceptance_service import (
    get_staging_run,
    list_staging_events,
    list_staging_runs,
    release_lock,
)
from settings import staging_acceptance_config

router = APIRouter(prefix="/api/v1/staging", tags=["Staging Acceptance"])


@router.get("/runs", summary="Staging 验收批次列表")
def get_runs(
    project_id: str | None = None,
    link_id: str | None = None,
    status: str | None = None,
    limit: int = Query(default=100, ge=1, le=300),
    offset: int = Query(default=0, ge=0),
    _: CurrentUser = Depends(get_current_user),
) -> dict:
    return list_staging_runs(project_id=project_id, link_id=link_id, status=status, limit=limit, offset=offset)


@router.get("/runs/{run_id}", summary="Staging 验收批次详情")
def get_run(run_id: str, _: CurrentUser = Depends(get_current_user)) -> dict:
    run = get_staging_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Staging run 不存在")
    return {"ok": True, "run": run}


@router.get("/runs/{run_id}/events", summary="Staging 验收事件")
def get_run_events(
    run_id: str,
    after_seq: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=1000),
    _: CurrentUser = Depends(get_current_user),
) -> dict:
    if not get_staging_run(run_id):
        raise HTTPException(status_code=404, detail="Staging run 不存在")
    return list_staging_events(run_id, after_seq=after_seq, limit=limit)


@router.get("/runs/{run_id}/events/stream", summary="Staging 验收事件 SSE")
async def stream_run_events(
    run_id: str,
    after_seq: int = 0,
    _: CurrentUser = Depends(get_current_user),
) -> StreamingResponse:
    if not get_staging_run(run_id):
        raise HTTPException(status_code=404, detail="Staging run 不存在")

    async def gen() -> AsyncIterator[str]:
        seq = after_seq
        idle = 0
        while idle < 3600:
            data = list_staging_events(run_id, after_seq=seq, limit=100)
            items = data["items"]
            if items:
                idle = 0
                for item in items:
                    seq = max(seq, int(item["seq"]))
                    yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
            else:
                idle += 1
                yield ": heartbeat\n\n"
            run = get_staging_run(run_id)
            if run and run.get("status") in {"passed", "test_failed", "infra_failed", "cancelled", "superseded"} and not items:
                yield "data: [DONE]\n\n"
                break
            await asyncio.sleep(1.0)

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.post("/runs/{run_id}/retry", summary="重新入队 staging 验收")
def retry_run(run_id: str, _: CurrentUser = Depends(get_current_user)) -> dict:
    run = get_staging_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Staging run 不存在")
    from core.staging_acceptance_service import enqueue_staging_for_link
    new_run = enqueue_staging_for_link(str(run.get("link_id") or ""))
    return {"ok": True, "run": new_run}


@router.post("/locks/{env_id}/release", summary="强制释放 staging 锁")
def force_release_lock(env_id: str, run_id: str = "", _: CurrentUser = Depends(get_current_user)) -> dict:
    ok = release_lock(run_id or "-", env_id, force=True)
    return {"ok": ok, "env_id": env_id}


@router.post("/runs/{run_id}/deploy-callback", summary="Staging CD 回调占位")
def deploy_callback(run_id: str, payload: dict, _: CurrentUser = Depends(get_current_user)) -> dict:
    # 当前实现使用 push dev 后的保守等待；保留回调接口，后续可把 workflow 等待改成 signal。
    if not get_staging_run(run_id):
        raise HTTPException(status_code=404, detail="Staging run 不存在")
    return {"ok": True, "run_id": run_id, "enabled": staging_acceptance_config.enabled, "payload": payload}


@router.post("/runs/{run_id}/test-callback", summary="Playwright 回调占位")
def test_callback(run_id: str, payload: dict, _: CurrentUser = Depends(get_current_user)) -> dict:
    if not get_staging_run(run_id):
        raise HTTPException(status_code=404, detail="Staging run 不存在")
    return {"ok": True, "run_id": run_id, "payload": payload}
