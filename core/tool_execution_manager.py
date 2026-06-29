"""并行工具执行管理器。

把一轮 LLM 产生的多个 tool call 作为独立 job 调度，负责并发、超时、
单 job 错误隔离和结果归并。Agent 循环保留决策权，manager 只管执行。
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool


@dataclass(frozen=True)
class ToolJob:
    """一条待执行工具调用。"""

    seq: int
    call_id: str
    name: str
    args: dict[str, Any]


@dataclass
class ToolJobResult:
    """单条工具执行结果。"""

    job: ToolJob
    ok: bool
    content: str
    elapsed_ms: int
    error: str | None = None


class ToolExecutionManager:
    """并行执行工具，并把结果还原成 LangChain ToolMessage。"""

    def __init__(
        self,
        tools: list[BaseTool],
        *,
        max_concurrency: int,
        timeout_sec: int,
        tool_timeout_overrides: dict[str, int] | None = None,
    ) -> None:
        self._tools = {tool.name: tool for tool in tools}
        self._semaphore = asyncio.Semaphore(max(max_concurrency, 1))
        self._timeout_sec = max(timeout_sec, 1)
        # 个别长任务工具（建 venv、跑复现脚本等）天然需要远超通用 tool_timeout_sec
        # 的时间预算。把它们从通用上限里豁免，改用各自更大的预算，否则通用 75s
        # 会先于工具内部超时触发，导致这类工具永远「超时」而无法完成。
        self._tool_timeout_overrides = {
            name: max(int(sec), 1)
            for name, sec in (tool_timeout_overrides or {}).items()
        }

    def _timeout_for(self, tool_name: str) -> int:
        return self._tool_timeout_overrides.get(tool_name, self._timeout_sec)

    async def iter_results(self, jobs: list[ToolJob]) -> AsyncIterator[ToolJobResult]:
        """并发执行 jobs，按完成顺序逐个产出结果。"""
        if not jobs:
            return

        tasks = [asyncio.create_task(self._run_one(job)) for job in jobs]
        try:
            for task in asyncio.as_completed(tasks):
                yield await task
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()

    def to_tool_messages(
        self,
        jobs: list[ToolJob],
        results: dict[str, ToolJobResult],
    ) -> list[ToolMessage]:
        """按原始 tool_call 顺序生成 ToolMessage，确保下一轮 LLM 能正确消费。"""
        messages: list[ToolMessage] = []
        for job in jobs:
            result = results.get(job.call_id)
            if result is None:
                result = ToolJobResult(
                    job=job,
                    ok=False,
                    content="工具执行被取消或未返回结果",
                    elapsed_ms=0,
                    error="missing_result",
                )
            messages.append(_make_tool_message(result))
        return messages

    async def _run_one(self, job: ToolJob) -> ToolJobResult:
        t0 = time.perf_counter()
        timeout_sec = self._timeout_for(job.name)
        try:
            tool = self._tools.get(job.name)
            if tool is None:
                raise ValueError(f"未知工具：{job.name}")
            async with self._semaphore:
                raw = await asyncio.wait_for(self._invoke(tool, job.args), timeout=timeout_sec)
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            return ToolJobResult(
                job=job,
                ok=True,
                content=str(raw if raw is not None else ""),
                elapsed_ms=elapsed_ms,
            )
        except asyncio.TimeoutError:
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            return ToolJobResult(
                job=job,
                ok=False,
                content=_timeout_message(job.name, timeout_sec, job.name in self._tool_timeout_overrides),
                elapsed_ms=elapsed_ms,
                error="timeout",
            )
        except Exception as e:  # noqa: BLE001
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            return ToolJobResult(
                job=job,
                ok=False,
                content=f"工具执行失败：{e}",
                elapsed_ms=elapsed_ms,
                error=str(e),
            )

    async def _invoke(self, tool: BaseTool, args: dict[str, Any]) -> Any:
        try:
            return await tool.ainvoke(args)
        except NotImplementedError:
            return await asyncio.to_thread(tool.invoke, args)


def _timeout_message(tool_name: str, timeout_sec: int, is_long_running: bool) -> str:
    """超时提示语。长任务工具（建 venv / 跑脚本）与普通查询的收口建议不同。"""
    if is_long_running:
        return (
            f"工具 {tool_name} 执行超时：超过 {timeout_sec} 秒仍未完成，已中止等待。"
            "这通常是依赖较多或网络较慢导致。请不要立刻原样重试；可改为缩小范围"
            "（如只装必要的 extra_packages），或提示用户该仓库 venv 仍在后台预热、稍后再试。"
        )
    return (
        f"工具执行超时：超过 {timeout_sec} 秒，已中止等待。"
        "请不要重复发起同类大范围查询；应基于已有证据给出部分结论，"
        "或请用户补充更窄的时间、平台、状态等过滤条件。"
    )


def _make_tool_message(result: ToolJobResult) -> ToolMessage:
    kwargs = {
        "content": result.content,
        "tool_call_id": result.job.call_id,
        "name": result.job.name,
    }
    try:
        return ToolMessage(**kwargs, status="success" if result.ok else "error")
    except TypeError:
        return ToolMessage(**kwargs)
