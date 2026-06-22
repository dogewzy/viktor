"""
多轮对话记忆管理器。

- thread_id（DB 列名保留）：钉钉 session 键 f"{conversation_id}:{sender_staff_id}"
- topic_thread_id：同一 session 下的议题段；/clear 开启新段，旧段数据保留
- turn_id：议题内一轮问答（human / ai / tool）

核心能力：
- load_history:  按 session + 议题段拉取最近 N 轮 → LangChain BaseMessage
- save_turn:     本轮消息落库
- record_topic_switch: 仅 /clear 无正文时写入 system_note 锚点，便于下次路由到新议题
- reset_session: 运维删除某 session 下**全部**议题（POST /chat/reset）
"""
from datetime import datetime, timedelta
import hashlib
from typing import Any, Iterable
from uuid import uuid4

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from loguru import logger
from sqlalchemy import case, delete, distinct, func, select

from core.database import SessionLocal
from core.models import AgentCheckpointModel, ChatMessageModel

# 单条 tool 消息内容裁剪阈值
_TOOL_CONTENT_MAX_BYTES = 8 * 1024
_TOOL_CONTENT_MAX_LINES = 50


def legacy_topic_thread_id(session_id: str) -> str:
    """无 topic 列的历史数据回填：每 session 一条稳定伪议题 id。"""
    h = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:24]
    return f"lgcy-{h}"


def new_topic_thread_id() -> str:
    return uuid4().hex


def get_latest_topic_thread_id(session_id: str) -> str | None:
    """取该 session 全局最新一行（含 system_note 锚点）的 topic_thread_id。"""
    if not session_id:
        return None
    with SessionLocal() as session:
        tid = session.execute(
            select(ChatMessageModel.topic_thread_id)
            .where(ChatMessageModel.thread_id == session_id)
            .order_by(ChatMessageModel.created_at.desc(), ChatMessageModel.id.desc())
            .limit(1)
        ).scalar_one_or_none()
    return tid


def record_topic_switch(session_id: str, topic_thread_id: str, project_id: str) -> None:
    """写入切议题锚点（不进入 Agent 上下文）。"""
    if not session_id or not topic_thread_id:
        return
    turn_id = f"topic-{uuid4().hex[:12]}"
    try:
        with SessionLocal() as session:
            session.add(
                ChatMessageModel(
                    thread_id=session_id,
                    topic_thread_id=topic_thread_id,
                    project_id=project_id,
                    turn_id=turn_id,
                    role="system_note",
                    content="topic_switch",
                )
            )
            session.commit()
        logger.info(
            "record_topic_switch session={} topic={}",
            session_id,
            topic_thread_id,
        )
    except Exception as e:  # noqa: BLE001
        logger.error("record_topic_switch 失败 session={}, err={}", session_id, e)


def _truncate_tool_content(text: str) -> tuple[str, bool]:
    """对超长 tool 返回做 head/tail 摘要。返回 (新内容, 是否裁剪)。"""
    if text is None:
        return "", False
    raw = str(text)
    lines = raw.splitlines()
    size = len(raw.encode("utf-8", errors="ignore"))

    if size <= _TOOL_CONTENT_MAX_BYTES and len(lines) <= _TOOL_CONTENT_MAX_LINES:
        return raw, False

    head = lines[:10]
    tail = lines[-5:] if len(lines) > 15 else []
    summary = (
        "\n".join(head)
        + f"\n... [已裁剪: 原 {len(lines)} 行 / {size} bytes] ...\n"
        + ("\n".join(tail) if tail else "")
    )
    return summary, True


def load_history(
    session_id: str,
    topic_thread_id: str,
    *,
    max_turns: int = 10,
    idle_minutes: int = 30,
) -> list[BaseMessage]:
    """按 session + 议题段拉取最近若干轮历史，还原为 LangChain BaseMessage。"""
    if not session_id or not topic_thread_id:
        return []

    cutoff = datetime.now() - timedelta(minutes=idle_minutes)

    with SessionLocal() as session:
        latest = session.execute(
            select(ChatMessageModel.created_at)
            .where(ChatMessageModel.thread_id == session_id)
            .where(ChatMessageModel.topic_thread_id == topic_thread_id)
            .order_by(ChatMessageModel.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()

        if latest is None or latest < cutoff:
            return []

        summary_row = session.execute(
            select(ChatMessageModel)
            .where(ChatMessageModel.thread_id == session_id)
            .where(ChatMessageModel.topic_thread_id == topic_thread_id)
            .where(ChatMessageModel.role == "summary")
            .order_by(ChatMessageModel.created_at.desc(), ChatMessageModel.id.desc())
            .limit(1)
        ).scalar_one_or_none()

        recent_turn_rows = session.execute(
            select(
                ChatMessageModel.turn_id,
            )
            .where(ChatMessageModel.thread_id == session_id)
            .where(ChatMessageModel.topic_thread_id == topic_thread_id)
            .where(ChatMessageModel.role != "system_note")
            .where(ChatMessageModel.role != "summary")
            .group_by(ChatMessageModel.turn_id)
            .order_by(ChatMessageModel.turn_id.desc())
            .limit(max_turns)
        ).all()
        turn_ids = {row.turn_id for row in recent_turn_rows}
        rows = []
        if turn_ids:
            rows = session.execute(
                select(ChatMessageModel)
                .where(ChatMessageModel.thread_id == session_id)
                .where(ChatMessageModel.topic_thread_id == topic_thread_id)
                .where(ChatMessageModel.turn_id.in_(turn_ids))
                .order_by(ChatMessageModel.created_at.asc(), ChatMessageModel.id.asc())
            ).scalars().all()

    messages: list[BaseMessage] = []
    if summary_row and summary_row.content:
        messages.append(SystemMessage(content=summary_row.content))
    skip_tool_call_ids: set[str] = set()
    for r in rows:
        try:
            if r.role == "human":
                messages.append(HumanMessage(content=r.content or ""))
            elif r.role == "ai":
                kwargs: dict[str, Any] = {"content": r.content or ""}
                if r.tool_calls:
                    reasoning_content = getattr(r, "reasoning_content", None)
                    if not reasoning_content:
                        skip_tool_call_ids.update(
                            str(c.get("id") or "")
                            for c in r.tool_calls
                            if isinstance(c, dict)
                        )
                        logger.warning(
                            "跳过缺少 reasoning_content 的历史 tool_call AI 消息, id={}, session={}, topic={}",
                            r.id,
                            session_id,
                            topic_thread_id,
                        )
                        continue
                    kwargs["tool_calls"] = r.tool_calls
                if getattr(r, "reasoning_content", None):
                    kwargs["additional_kwargs"] = {
                        "reasoning_content": r.reasoning_content,
                    }
                messages.append(AIMessage(**kwargs))
            elif r.role == "tool":
                if str(r.tool_call_id or "") in skip_tool_call_ids:
                    continue
                messages.append(
                    ToolMessage(
                        content=r.content or "",
                        tool_call_id=r.tool_call_id or "",
                        name=r.tool_name or None,
                    )
                )
        except Exception as e:  # noqa: BLE001
            logger.warning("还原历史消息失败, id={}, role={}, error={}", r.id, r.role, e)

    logger.debug(
        "load_history session={} topic={} turns={} messages={}",
        session_id,
        topic_thread_id,
        len(turn_ids),
        len(messages),
    )
    return messages


def _extract_tool_calls(msg: AIMessage) -> list[dict] | None:
    """从 AIMessage 中抽取标准化的 tool_calls 结构以便持久化。"""
    tcs = getattr(msg, "tool_calls", None)
    if not tcs:
        return None
    out: list[dict] = []
    for tc in tcs:
        if isinstance(tc, dict):
            out.append(
                {
                    "id": tc.get("id"),
                    "name": tc.get("name"),
                    "args": tc.get("args"),
                }
            )
        else:
            out.append(
                {
                    "id": getattr(tc, "id", None),
                    "name": getattr(tc, "name", None),
                    "args": getattr(tc, "args", None),
                }
            )
    return out or None


def save_turn(
    session_id: str,
    topic_thread_id: str,
    project_id: str,
    new_messages: Iterable[BaseMessage],
) -> None:
    """将本轮新增的 Human/AI/Tool 消息写入同一 turn_id。"""
    if not session_id or not topic_thread_id:
        return

    turn_id = datetime.now().strftime("%Y%m%d%H%M%S") + "-" + uuid4().hex[:8]
    rows: list[ChatMessageModel] = []

    for msg in new_messages:
        if isinstance(msg, SystemMessage):
            continue
        if isinstance(msg, HumanMessage):
            rows.append(
                ChatMessageModel(
                    thread_id=session_id,
                    topic_thread_id=topic_thread_id,
                    project_id=project_id,
                    turn_id=turn_id,
                    role="human",
                    content=msg.content or "",
                )
            )
        elif isinstance(msg, AIMessage):
            reasoning_content = msg.additional_kwargs.get("reasoning_content")
            rows.append(
                ChatMessageModel(
                    thread_id=session_id,
                    topic_thread_id=topic_thread_id,
                    project_id=project_id,
                    turn_id=turn_id,
                    role="ai",
                    content=msg.content or "",
                    tool_calls=_extract_tool_calls(msg),
                    reasoning_content=reasoning_content,
                )
            )
        elif isinstance(msg, ToolMessage):
            content, truncated = _truncate_tool_content(msg.content or "")
            rows.append(
                ChatMessageModel(
                    thread_id=session_id,
                    topic_thread_id=topic_thread_id,
                    project_id=project_id,
                    turn_id=turn_id,
                    role="tool",
                    content=content,
                    tool_call_id=getattr(msg, "tool_call_id", None) or "",
                    tool_name=getattr(msg, "name", None),
                    truncated=1 if truncated else 0,
                )
            )
        else:
            continue

    if not rows:
        return

    try:
        with SessionLocal() as session:
            session.add_all(rows)
            session.commit()
        logger.info(
            "save_turn session={} topic={} turn={} project={} count={}",
            session_id,
            topic_thread_id,
            turn_id,
            project_id,
            len(rows),
        )
    except Exception as e:  # noqa: BLE001
        logger.error("save_turn 失败, session={}, error={}", session_id, e)


def save_compaction_summary(
    session_id: str,
    topic_thread_id: str,
    project_id: str,
    summary: str,
) -> None:
    """Persist a compacted history summary for future turns."""
    if not session_id or not topic_thread_id or not summary.strip():
        return
    turn_id = "summary-" + uuid4().hex[:12]
    try:
        with SessionLocal() as session:
            session.add(
                ChatMessageModel(
                    thread_id=session_id,
                    topic_thread_id=topic_thread_id,
                    project_id=project_id,
                    turn_id=turn_id,
                    role="summary",
                    content=summary.strip(),
                )
            )
            session.commit()
        logger.info("save_compaction_summary session={} topic={}", session_id, topic_thread_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("save_compaction_summary 失败, session={}, error={}", session_id, e)


def reset_session(session_id: str) -> int:
    """删除某 session 下全部消息（各议题段），返回删除条数。"""
    if not session_id:
        return 0
    try:
        with SessionLocal() as session:
            result = session.execute(
                delete(ChatMessageModel).where(ChatMessageModel.thread_id == session_id)
            )
            session.commit()
            deleted = result.rowcount or 0
        logger.info("reset_session session={} deleted={}", session_id, deleted)
        return int(deleted)
    except Exception as e:  # noqa: BLE001
        logger.error("reset_session 失败, session={}, error={}", session_id, e)
        return 0


def list_threads(
    project_id: str | None = None, days: int = 7, limit: int = 200
) -> list[dict]:
    """按 topic_thread（议题段）聚合；system_note 不参与条数与轮数。"""
    cutoff = datetime.now() - timedelta(days=days)
    last_at = func.max(ChatMessageModel.created_at).label("last_at")
    msg_count = func.sum(
        case((ChatMessageModel.role.notin_(["system_note", "summary"]), 1), else_=0)
    ).label("msg_count")
    turn_key = case(
        (ChatMessageModel.role.notin_(["system_note", "summary"]), ChatMessageModel.turn_id),
        else_=None,
    )
    turn_count = func.count(distinct(turn_key)).label("turn_count")
    with SessionLocal() as session:
        stmt = (
            select(
                ChatMessageModel.thread_id,
                ChatMessageModel.topic_thread_id,
                ChatMessageModel.project_id,
                last_at,
                msg_count,
                turn_count,
            )
            .where(ChatMessageModel.created_at >= cutoff)
            .group_by(
                ChatMessageModel.thread_id,
                ChatMessageModel.topic_thread_id,
                ChatMessageModel.project_id,
            )
            .order_by(last_at.desc())
            .limit(limit)
        )
        if project_id:
            stmt = stmt.where(ChatMessageModel.project_id == project_id)
        rows = session.execute(stmt).all()
    return [
        {
            "session_id": r.thread_id,
            "topic_thread_id": r.topic_thread_id,
            "project_id": r.project_id,
            "last_at": r.last_at.isoformat() if r.last_at else None,
            "msg_count": int(r.msg_count or 0),
            "turn_count": int(r.turn_count or 0),
        }
        for r in rows
    ]


def fetch_thread_messages(topic_thread_id: str, limit: int = 1000) -> list[dict]:
    """按 topic_thread_id 拉取消息（不含 system_note 展示）。"""
    if not topic_thread_id:
        return []
    with SessionLocal() as session:
        rows = session.execute(
            select(ChatMessageModel)
            .where(ChatMessageModel.topic_thread_id == topic_thread_id)
            .where(ChatMessageModel.role != "system_note")
            .where(ChatMessageModel.role != "summary")
            .order_by(ChatMessageModel.created_at.asc(), ChatMessageModel.id.asc())
            .limit(limit)
        ).scalars().all()

    return [
        {
            "id": r.id,
            "turn_id": r.turn_id,
            "session_id": r.thread_id,
            "topic_thread_id": r.topic_thread_id,
            "project_id": r.project_id,
            "role": r.role,
            "content": r.content or "",
            "tool_calls": r.tool_calls,
            "tool_call_id": r.tool_call_id,
            "tool_name": r.tool_name,
            "truncated": bool(r.truncated),
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


def fetch_history_raw(topic_thread_id: str, limit: int = 50) -> list[dict]:
    """运维接口：指定议题段的原始记录（倒序）。"""
    with SessionLocal() as session:
        rows = session.execute(
            select(ChatMessageModel)
            .where(ChatMessageModel.topic_thread_id == topic_thread_id)
            .where(ChatMessageModel.role != "system_note")
            .where(ChatMessageModel.role != "summary")
            .order_by(ChatMessageModel.created_at.desc(), ChatMessageModel.id.desc())
            .limit(limit)
        ).scalars().all()

    return [
        {
            "id": r.id,
            "turn_id": r.turn_id,
            "session_id": r.thread_id,
            "topic_thread_id": r.topic_thread_id,
            "project_id": r.project_id,
            "role": r.role,
            "content": r.content,
            "tool_calls": r.tool_calls,
            "tool_call_id": r.tool_call_id,
            "tool_name": r.tool_name,
            "truncated": bool(r.truncated),
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


# 兼容旧代码名
reset_thread = reset_session


# ---------------------------------------------------------------------------
# Agent Checkpoint CRUD
# ---------------------------------------------------------------------------


def save_agent_checkpoint(
    session_id: str,
    topic_thread_id: str,
    project_id: str,
    user_message: str,
    system_prompt: str,
    *,
    intent_route: Any | None = None,
    retrieval_context: str = "",
    llm_feature: str = "sse_agent",
    provider_order: list[str] | None = None,
    ttl_minutes: int = 30,
) -> None:
    """保存 checkpoint（同一议题只保留最新一个）。"""
    if not session_id or not topic_thread_id:
        return
    expires_at = datetime.now() + timedelta(minutes=ttl_minutes)
    try:
        with SessionLocal() as session:
            # 删旧
            session.execute(
                delete(AgentCheckpointModel)
                .where(AgentCheckpointModel.session_id == session_id)
                .where(AgentCheckpointModel.topic_thread_id == topic_thread_id)
            )
            # 写新
            row = AgentCheckpointModel(
                session_id=session_id,
                topic_thread_id=topic_thread_id,
                project_id=project_id,
                user_message=user_message,
                system_prompt=system_prompt,
                intent_route_json=_serialize_intent_route(intent_route),
                retrieval_context=retrieval_context,
                llm_feature=llm_feature,
                provider_order_json=provider_order,
                expires_at=expires_at,
            )
            session.add(row)
            session.commit()
        logger.info(
            "save_agent_checkpoint session={} topic={} project={} expires={}",
            session_id, topic_thread_id, project_id, expires_at.isoformat(),
        )
    except Exception as e:  # noqa: BLE001
        logger.error("save_agent_checkpoint 失败: {}", e)


def load_agent_checkpoint(session_id: str, topic_thread_id: str) -> dict[str, Any] | None:
    """加载未过期的最新 checkpoint，返回字段 dict 或 None。"""
    if not session_id or not topic_thread_id:
        return None
    now = datetime.now()
    with SessionLocal() as session:
        row = session.execute(
            select(AgentCheckpointModel)
            .where(AgentCheckpointModel.session_id == session_id)
            .where(AgentCheckpointModel.topic_thread_id == topic_thread_id)
            .where(AgentCheckpointModel.expires_at > now)
            .order_by(AgentCheckpointModel.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if row is None:
            return None
        return {
            "id": row.id,
            "session_id": row.session_id,
            "topic_thread_id": row.topic_thread_id,
            "project_id": row.project_id,
            "user_message": row.user_message,
            "system_prompt": row.system_prompt,
            "intent_route_json": row.intent_route_json,
            "retrieval_context": row.retrieval_context or "",
            "llm_feature": row.llm_feature or "sse_agent",
            "provider_order_json": row.provider_order_json,
            "created_at": row.created_at,
        }


def clear_agent_checkpoint(session_id: str, topic_thread_id: str) -> None:
    """删除该 session/topic 下所有 checkpoint。"""
    if not session_id or not topic_thread_id:
        return
    try:
        with SessionLocal() as session:
            session.execute(
                delete(AgentCheckpointModel)
                .where(AgentCheckpointModel.session_id == session_id)
                .where(AgentCheckpointModel.topic_thread_id == topic_thread_id)
            )
            session.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("clear_agent_checkpoint 失败: {}", e)


def cleanup_interrupted_messages(
    session_id: str, topic_thread_id: str, after_time: datetime
) -> int:
    """删除 checkpoint 之后被中断保存的 partial messages，返回删除行数。"""
    if not session_id or not topic_thread_id:
        return 0
    try:
        with SessionLocal() as session:
            result = session.execute(
                delete(ChatMessageModel)
                .where(ChatMessageModel.thread_id == session_id)
                .where(ChatMessageModel.topic_thread_id == topic_thread_id)
                .where(ChatMessageModel.created_at > after_time)
            )
            session.commit()
            deleted = result.rowcount  # type: ignore[attr-defined]
            if deleted:
                logger.info(
                    "cleanup_interrupted_messages session={} topic={} deleted={}",
                    session_id, topic_thread_id, deleted,
                )
            return deleted
    except Exception as e:  # noqa: BLE001
        logger.warning("cleanup_interrupted_messages 失败: {}", e)
        return 0


def _serialize_intent_route(route: Any) -> dict | None:
    """将 IntentRoute dataclass 转为可 JSON 序列化的 dict。"""
    if route is None:
        return None
    if isinstance(route, dict):
        return route
    if hasattr(route, "__dict__"):
        return {k: v for k, v in route.__dict__.items() if not k.startswith("_")}
    return None
