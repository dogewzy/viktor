"""对话记忆（topic thread）运维接口。

- GET  /api/v1/chat/history   查看指定 topic_thread_id 的原始历史记录
- POST /api/v1/chat/reset    清空某 session（conversation_id:sender）下全部议题（慎用）
- GET  /api/v1/chat/threads   列出最近活跃的 topic thread（议题段）
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from core.memory import fetch_history_raw, list_threads, reset_session

router = APIRouter(prefix="/api/v1/chat", tags=["对话记忆"])


class ResetRequest(BaseModel):
    session_id: str | None = Field(None, description="conversation_id:sender_staff_id")
    thread_id: str | None = Field(
        None,
        description="兼容旧请求体字段，与 session_id 等价",
    )


class ResetResponse(BaseModel):
    session_id: str
    deleted: int


@router.get("/history", summary="查看指定议题段的历史记录")
def get_history(
    topic_thread_id: str | None = None,
    thread_id: str | None = None,
    limit: int = 50,
) -> dict:
    tid = (topic_thread_id or thread_id or "").strip()
    if not tid:
        raise HTTPException(status_code=400, detail="topic_thread_id 不能为空")
    if limit <= 0 or limit > 500:
        raise HTTPException(status_code=400, detail="limit 需在 1~500 之间")
    records = fetch_history_raw(tid, limit=limit)
    return {"topic_thread_id": tid, "count": len(records), "records": records}


@router.post("/reset", summary="清空某 session 下全部议题记录", response_model=ResetResponse)
def post_reset(body: ResetRequest) -> ResetResponse:
    session_id = (body.session_id or body.thread_id or "").strip()
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id 不能为空")
    deleted = reset_session(session_id)
    return ResetResponse(session_id=session_id, deleted=deleted)


@router.get("/threads", summary="列出最近活跃 topic thread（按议题段聚合）")
def get_threads(project_id: str | None = None, days: int = 7, limit: int = 200) -> dict:
    if days <= 0 or days > 90:
        raise HTTPException(status_code=400, detail="days 需在 1~90 之间")
    if limit <= 0 or limit > 1000:
        raise HTTPException(status_code=400, detail="limit 需在 1~1000 之间")
    items = list_threads(project_id=project_id, days=days, limit=limit)
    return {"count": len(items), "items": items}
