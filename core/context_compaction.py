"""Token-budget based context compaction utilities."""
from __future__ import annotations

from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from loguru import logger

from core.llm_client import create_llm


def _message_text(message: Any) -> str:
    role = getattr(message, "type", None) or getattr(message, "role", None) or message.__class__.__name__
    content = getattr(message, "content", "")
    if isinstance(content, list):
        content_text = "\n".join(str(item) for item in content)
    else:
        content_text = str(content or "")
    tool_calls = getattr(message, "tool_calls", None)
    if tool_calls:
        content_text += "\n工具调用: " + str(tool_calls)
    name = getattr(message, "name", None)
    if name:
        role = f"{role}:{name}"
    return f"[{role}]\n{content_text}"


def estimate_tokens(value: Any) -> int:
    """Cheap, provider-agnostic token estimate."""
    if isinstance(value, list):
        text = "\n\n".join(_message_text(item) for item in value)
    else:
        text = str(value or "")
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    non_ascii_chars = len(text) - ascii_chars
    return max(1, ascii_chars // 4 + non_ascii_chars // 2)


def should_compact(value: Any, threshold_tokens: int) -> bool:
    return threshold_tokens > 0 and estimate_tokens(value) > threshold_tokens


def _split_recent_turns(messages: list[BaseMessage], keep_recent_turns: int) -> tuple[list[BaseMessage], list[BaseMessage]]:
    if keep_recent_turns <= 0:
        return messages, []
    human_seen = 0
    split_index = 0
    for index in range(len(messages) - 1, -1, -1):
        if isinstance(messages[index], HumanMessage):
            human_seen += 1
            if human_seen >= keep_recent_turns:
                split_index = index
                break
    if human_seen < keep_recent_turns:
        return [], messages
    return messages[:split_index], messages[split_index:]


async def compact_text(
    text: str,
    *,
    title: str,
    target_tokens: int,
) -> str:
    if not text.strip():
        return ""
    llm = create_llm(thinking=False, feature="context_compaction")
    result = await llm.ainvoke([
        SystemMessage(
            content=(
                "你是上下文压缩器。请把输入压缩成后续 Agent 可依赖的中文 Markdown 摘要。"
                "保留：用户目标、已确认口径、关键证据、重要文件/表/字段、失败尝试、未解决问题。"
                "删除：重复寒暄、冗余日志、大段原始工具输出。不要编造新事实。"
            )
        ),
        HumanMessage(
            content=(
                f"## 压缩标题\n{title}\n\n"
                f"## 目标 token 上限\n{target_tokens}\n\n"
                f"## 待压缩内容\n{text}"
            )
        ),
    ])
    content = result.content if isinstance(result.content, str) else str(result.content or "")
    return content.strip()


async def compact_messages(
    messages: list[BaseMessage],
    *,
    threshold_tokens: int,
    target_tokens: int,
    keep_recent_turns: int,
    title: str = "历史上下文摘要",
) -> tuple[list[BaseMessage], str | None]:
    """Compact older messages and keep recent turns intact."""
    if not should_compact(messages, threshold_tokens):
        return messages, None

    old_messages, recent_messages = _split_recent_turns(messages, keep_recent_turns)
    if not old_messages:
        return messages, None

    old_text = "\n\n".join(_message_text(item) for item in old_messages)
    try:
        summary = await compact_text(old_text, title=title, target_tokens=target_tokens)
    except Exception as e:  # noqa: BLE001
        logger.warning("context compaction failed: {}", e)
        return messages, None
    if not summary:
        return messages, None

    summary_message = SystemMessage(content=f"## {title}\n{summary}")
    return [summary_message, *recent_messages], summary
