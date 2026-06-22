"""
LLM 客户端封装。

使用官方 langchain-deepseek 的 ChatDeepSeek，对接 DeepSeek API，
并补齐「思考模式 + 多轮工具调用」所需的 reasoning_content 回传逻辑。

背景：
  - DeepSeek 文档要求：思考模式下，发生过 tool_call 的 assistant 消息，
    后续所有请求必须原样回传 reasoning_content，否则 API 400。
  - langchain-openai.ChatOpenAI：设计上只支持 OpenAI 原生协议，丢弃 reasoning_content。
  - langchain-deepseek 1.0.1 的 ChatDeepSeek：能解析响应中的 reasoning_content 并
    写入 AIMessage.additional_kwargs，但在构造下一轮请求 payload 时
    尚未把 additional_kwargs["reasoning_content"] 回写到 assistant 消息。
  - 本模块定义的 _ChatDeepSeekThinking / _ChatKimiThinking 子类补齐
    reasoning_content 的入站保存与出站回写。

接口文档：https://api-docs.deepseek.com/zh-cn/
思考模式：https://api-docs.deepseek.com/zh-cn/guides/thinking_mode
"""
from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Iterator, Sequence
from datetime import datetime
from typing import Any

from langchain_core.language_models.chat_models import LanguageModelInput
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_deepseek import ChatDeepSeek
from langchain_openai import ChatOpenAI
from loguru import logger
from pydantic import ConfigDict, Field

from core.audit.recorder import record_trace_event
from core.llm_metrics import (
    LLMCallRecord,
    current_llm_context,
    mark_provider_cooldown,
    provider_cooldown_remaining,
    record_llm_call,
)
from settings import LLMProviderConfig, llm_config


# 思考模式是否开启。Viktor 二期目标是完整的研发闭环，推理质量优先于成本，
# 因此默认开启；需要临时关闭可改成 False。
ENABLE_THINKING: bool = True

# 思考强度。high 适合 Agent 多步推理；max 仅在 Claude Code 这类极重推理场景使用。
# DeepSeek 当前会把 low/medium 映射为 high，把 xhigh 映射为 max。
REASONING_EFFORT: str = "high"


class _ChatDeepSeekThinking(ChatDeepSeek):
    """
    在 ChatDeepSeek 之上补齐：把 AIMessage.additional_kwargs["reasoning_content"]
    回写到发往 DeepSeek 的 assistant 消息中，从而支持「思考模式 + 多轮 tool call」。

    如果未来 langchain-deepseek 官方实现了这一步，可直接移除本子类。
    """

    def _get_request_payload(
        self,
        input_: LanguageModelInput,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> dict:
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        _inject_reasoning_content_into_payload(self, input_, payload)
        return payload


class _ChatKimiThinking(ChatOpenAI):
    """
    Kimi K2 thinking 兼容层：保留响应里的 reasoning_content，并在多轮
    tool call 请求中回写到历史 assistant message。
    """

    def _get_request_payload(
        self,
        input_: LanguageModelInput,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> dict:
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        _inject_reasoning_content_into_payload(self, input_, payload)
        return payload

    def _create_chat_result(
        self,
        response: dict | Any,
        generation_info: dict | None = None,
    ) -> ChatResult:
        result = super()._create_chat_result(response, generation_info=generation_info)
        choices = _response_to_dict(response).get("choices") or []
        for idx, choice in enumerate(choices):
            if idx >= len(result.generations) or not isinstance(choice, dict):
                continue
            reasoning = _extract_reasoning_content(choice.get("message"))
            if not reasoning:
                reasoning = _extract_typed_response_reasoning(response, idx)
            message = result.generations[idx].message
            if reasoning and isinstance(message, AIMessage):
                message.additional_kwargs["reasoning_content"] = reasoning
        return result

    def _convert_chunk_to_generation_chunk(
        self,
        chunk: dict,
        default_chunk_class: type,
        base_generation_info: dict | None,
    ) -> ChatGenerationChunk | None:
        generation_chunk = super()._convert_chunk_to_generation_chunk(
            chunk,
            default_chunk_class,
            base_generation_info,
        )
        reasoning = _extract_stream_reasoning_content(chunk)
        message = getattr(generation_chunk, "message", None)
        if reasoning and isinstance(message, AIMessageChunk):
            message.additional_kwargs["reasoning_content"] = reasoning
        return generation_chunk


class LLMRouter(BaseChatModel):
    """带 fallback 与观测的 LangChain ChatModel 包装器。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    thinking: bool | None = None
    feature: str = "agent"
    provider_order: list[str] = Field(default_factory=list)
    bound_tools: list[Any] | None = None
    bind_kwargs: dict[str, Any] = Field(default_factory=dict)

    @property
    def _llm_type(self) -> str:
        return "viktor-llm-router"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {
            "feature": self.feature,
            "provider_order": self._ordered_provider_ids(),
            "thinking": self.thinking,
        }

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> "LLMRouter":
        return self.model_copy(
            update={
                "bound_tools": list(tools),
                "bind_kwargs": dict(kwargs),
            }
        )

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        request_id = uuid.uuid4().hex
        last_error: Exception | None = None
        fallback_from: str | None = None
        for attempt_index, provider_id in enumerate(self._ordered_provider_ids(), start=1):
            if self._skip_provider(provider_id):
                continue
            cfg = llm_config.providers[provider_id]
            started_at = datetime.now()
            t0 = time.perf_counter()
            try:
                _record_llm_trace_request(
                    request_id=request_id,
                    feature=self.feature,
                    provider_id=provider_id,
                    cfg=cfg,
                    attempt_index=attempt_index,
                    fallback_from=fallback_from,
                    streaming=False,
                    messages=messages,
                )
                model = self._build_provider_model(provider_id)
                result = self._invoke_provider_sync(model, messages, stop=stop, run_manager=run_manager, **kwargs)
                duration_ms = (time.perf_counter() - t0) * 1000
                record_llm_call(
                    self._record_from_result(
                        result,
                        request_id=request_id,
                        cfg=cfg,
                        provider_id=provider_id,
                        attempt_index=attempt_index,
                        fallback_from=fallback_from,
                        started_at=started_at,
                        duration_ms=duration_ms,
                        messages=messages,
                    )
                )
                _record_llm_trace_response(
                    request_id=request_id,
                    feature=self.feature,
                    provider_id=provider_id,
                    cfg=cfg,
                    attempt_index=attempt_index,
                    status="success",
                    streaming=False,
                    duration_ms=duration_ms,
                    result=result,
                )
                return result
            except Exception as e:  # noqa: BLE001
                last_error = e
                status = _error_status(e)
                if _should_cooldown_provider(e):
                    mark_provider_cooldown(provider_id)
                record_llm_call(
                    self._record_error(
                        request_id=request_id,
                        cfg=cfg,
                        provider_id=provider_id,
                        attempt_index=attempt_index,
                        fallback_from=fallback_from,
                        started_at=started_at,
                        duration_ms=(time.perf_counter() - t0) * 1000,
                        status=status,
                        error=e,
                    )
                )
                _record_llm_trace_error(
                    request_id=request_id,
                    feature=self.feature,
                    provider_id=provider_id,
                    cfg=cfg,
                    attempt_index=attempt_index,
                    status=status,
                    streaming=False,
                    duration_ms=(time.perf_counter() - t0) * 1000,
                    error=e,
                )
                logger.warning(
                    "LLM 调用失败，尝试 fallback: feature={}, provider={}, status={}, error={}",
                    self.feature,
                    provider_id,
                    status,
                    e,
                )
                fallback_from = provider_id
                if not self._should_fallback(e):
                    break
        if last_error:
            raise last_error
        raise RuntimeError("没有可用的 LLM provider")

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        request_id = uuid.uuid4().hex
        last_error: Exception | None = None
        fallback_from: str | None = None
        for attempt_index, provider_id in enumerate(self._ordered_provider_ids(), start=1):
            if self._skip_provider(provider_id):
                continue
            cfg = llm_config.providers[provider_id]
            started_at = datetime.now()
            t0 = time.perf_counter()
            try:
                _record_llm_trace_request(
                    request_id=request_id,
                    feature=self.feature,
                    provider_id=provider_id,
                    cfg=cfg,
                    attempt_index=attempt_index,
                    fallback_from=fallback_from,
                    streaming=False,
                    messages=messages,
                )
                model = self._build_provider_model(provider_id)
                result = await self._invoke_provider_async(model, messages, stop=stop, run_manager=run_manager, **kwargs)
                duration_ms = (time.perf_counter() - t0) * 1000
                record_llm_call(
                    self._record_from_result(
                        result,
                        request_id=request_id,
                        cfg=cfg,
                        provider_id=provider_id,
                        attempt_index=attempt_index,
                        fallback_from=fallback_from,
                        started_at=started_at,
                        duration_ms=duration_ms,
                        messages=messages,
                    )
                )
                _record_llm_trace_response(
                    request_id=request_id,
                    feature=self.feature,
                    provider_id=provider_id,
                    cfg=cfg,
                    attempt_index=attempt_index,
                    status="success",
                    streaming=False,
                    duration_ms=duration_ms,
                    result=result,
                )
                return result
            except Exception as e:  # noqa: BLE001
                last_error = e
                status = _error_status(e)
                if _should_cooldown_provider(e):
                    mark_provider_cooldown(provider_id)
                record_llm_call(
                    self._record_error(
                        request_id=request_id,
                        cfg=cfg,
                        provider_id=provider_id,
                        attempt_index=attempt_index,
                        fallback_from=fallback_from,
                        started_at=started_at,
                        duration_ms=(time.perf_counter() - t0) * 1000,
                        status=status,
                        error=e,
                    )
                )
                _record_llm_trace_error(
                    request_id=request_id,
                    feature=self.feature,
                    provider_id=provider_id,
                    cfg=cfg,
                    attempt_index=attempt_index,
                    status=status,
                    streaming=False,
                    duration_ms=(time.perf_counter() - t0) * 1000,
                    error=e,
                )
                logger.warning(
                    "LLM 异步调用失败，尝试 fallback: feature={}, provider={}, status={}, error={}",
                    self.feature,
                    provider_id,
                    status,
                    e,
                )
                fallback_from = provider_id
                if not self._should_fallback(e):
                    break
        if last_error:
            raise last_error
        raise RuntimeError("没有可用的 LLM provider")

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        request_id = uuid.uuid4().hex
        last_error: Exception | None = None
        fallback_from: str | None = None
        for attempt_index, provider_id in enumerate(self._ordered_provider_ids(), start=1):
            if self._skip_provider(provider_id):
                continue
            cfg = llm_config.providers[provider_id]
            started_at = datetime.now()
            t0 = time.perf_counter()
            first_token_ms: float | None = None
            output_chars = 0
            output_text_parts: list[str] = []
            reasoning_parts: list[str] = []
            yielded = False
            try:
                _record_llm_trace_request(
                    request_id=request_id,
                    feature=self.feature,
                    provider_id=provider_id,
                    cfg=cfg,
                    attempt_index=attempt_index,
                    fallback_from=fallback_from,
                    streaming=True,
                    messages=messages,
                )
                model = self._build_provider_model(provider_id)
                async for chunk in self._stream_provider_async(model, messages, stop=stop, run_manager=run_manager, **kwargs):
                    text = _chunk_text(chunk)
                    if text:
                        output_text_parts.append(text)
                        output_chars += len(text)
                        if first_token_ms is None:
                            first_token_ms = (time.perf_counter() - t0) * 1000
                    reasoning = _chunk_reasoning(chunk)
                    if reasoning:
                        reasoning_parts.append(reasoning)
                    yielded = True
                    yield chunk
                duration_ms = (time.perf_counter() - t0) * 1000
                prompt_tokens = _estimated_message_tokens(messages)
                completion_tokens = _estimated_output_tokens(output_chars)
                total_tokens = prompt_tokens + completion_tokens
                record_llm_call(
                    LLMCallRecord(
                        request_id=request_id,
                        feature=self.feature,
                        provider=provider_id,
                        model=cfg.model,
                        attempt_index=attempt_index,
                        fallback_from=fallback_from,
                        status="success",
                        streaming=True,
                        started_at=started_at,
                        first_token_ms=first_token_ms,
                        duration_ms=duration_ms,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        total_tokens=total_tokens,
                        output_chars=output_chars,
                        tokens_per_second=_estimated_tps(output_chars, duration_ms),
                        meta=_record_meta(
                            token_count_source="estimated",
                            estimated_total_tokens=total_tokens,
                        ),
                    )
                )
                _record_llm_trace_stream_response(
                    request_id=request_id,
                    feature=self.feature,
                    provider_id=provider_id,
                    cfg=cfg,
                    attempt_index=attempt_index,
                    status="success",
                    streaming=True,
                    duration_ms=duration_ms,
                    content="".join(output_text_parts),
                    reasoning_content="".join(reasoning_parts),
                    output_chars=output_chars,
                )
                return
            except Exception as e:  # noqa: BLE001
                last_error = e
                status = _error_status(e)
                if _should_cooldown_provider(e):
                    mark_provider_cooldown(provider_id)
                record_llm_call(
                    self._record_error(
                        request_id=request_id,
                        cfg=cfg,
                        provider_id=provider_id,
                        attempt_index=attempt_index,
                        fallback_from=fallback_from,
                        started_at=started_at,
                        duration_ms=(time.perf_counter() - t0) * 1000,
                        status=status,
                        error=e,
                        streaming=True,
                        first_token_ms=first_token_ms,
                        output_chars=output_chars,
                    )
                )
                _record_llm_trace_error(
                    request_id=request_id,
                    feature=self.feature,
                    provider_id=provider_id,
                    cfg=cfg,
                    attempt_index=attempt_index,
                    status=status,
                    streaming=True,
                    duration_ms=(time.perf_counter() - t0) * 1000,
                    error=e,
                    output_chars=output_chars,
                )
                if yielded:
                    raise
                logger.warning(
                    "LLM 流式调用首 token 前失败，尝试 fallback: feature={}, provider={}, status={}, error={}",
                    self.feature,
                    provider_id,
                    status,
                    e,
                )
                fallback_from = provider_id
                if not self._should_fallback(e):
                    break
        if last_error:
            raise last_error
        raise RuntimeError("没有可用的 LLM provider")

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        request_id = uuid.uuid4().hex
        last_error: Exception | None = None
        fallback_from: str | None = None
        for attempt_index, provider_id in enumerate(self._ordered_provider_ids(), start=1):
            if self._skip_provider(provider_id):
                continue
            cfg = llm_config.providers[provider_id]
            started_at = datetime.now()
            t0 = time.perf_counter()
            first_token_ms: float | None = None
            output_chars = 0
            output_text_parts: list[str] = []
            reasoning_parts: list[str] = []
            yielded = False
            try:
                _record_llm_trace_request(
                    request_id=request_id,
                    feature=self.feature,
                    provider_id=provider_id,
                    cfg=cfg,
                    attempt_index=attempt_index,
                    fallback_from=fallback_from,
                    streaming=True,
                    messages=messages,
                )
                model = self._build_provider_model(provider_id)
                for chunk in self._stream_provider_sync(model, messages, stop=stop, run_manager=run_manager, **kwargs):
                    text = _chunk_text(chunk)
                    if text:
                        output_text_parts.append(text)
                        output_chars += len(text)
                        if first_token_ms is None:
                            first_token_ms = (time.perf_counter() - t0) * 1000
                    reasoning = _chunk_reasoning(chunk)
                    if reasoning:
                        reasoning_parts.append(reasoning)
                    yielded = True
                    yield chunk
                duration_ms = (time.perf_counter() - t0) * 1000
                prompt_tokens = _estimated_message_tokens(messages)
                completion_tokens = _estimated_output_tokens(output_chars)
                total_tokens = prompt_tokens + completion_tokens
                record_llm_call(
                    LLMCallRecord(
                        request_id=request_id,
                        feature=self.feature,
                        provider=provider_id,
                        model=cfg.model,
                        attempt_index=attempt_index,
                        fallback_from=fallback_from,
                        status="success",
                        streaming=True,
                        started_at=started_at,
                        first_token_ms=first_token_ms,
                        duration_ms=duration_ms,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        total_tokens=total_tokens,
                        output_chars=output_chars,
                        tokens_per_second=_estimated_tps(output_chars, duration_ms),
                        meta=_record_meta(
                            token_count_source="estimated",
                            estimated_total_tokens=total_tokens,
                        ),
                    )
                )
                _record_llm_trace_stream_response(
                    request_id=request_id,
                    feature=self.feature,
                    provider_id=provider_id,
                    cfg=cfg,
                    attempt_index=attempt_index,
                    status="success",
                    streaming=True,
                    duration_ms=duration_ms,
                    content="".join(output_text_parts),
                    reasoning_content="".join(reasoning_parts),
                    output_chars=output_chars,
                )
                return
            except Exception as e:  # noqa: BLE001
                last_error = e
                status = _error_status(e)
                if _should_cooldown_provider(e):
                    mark_provider_cooldown(provider_id)
                record_llm_call(
                    self._record_error(
                        request_id=request_id,
                        cfg=cfg,
                        provider_id=provider_id,
                        attempt_index=attempt_index,
                        fallback_from=fallback_from,
                        started_at=started_at,
                        duration_ms=(time.perf_counter() - t0) * 1000,
                        status=status,
                        error=e,
                        streaming=True,
                        first_token_ms=first_token_ms,
                        output_chars=output_chars,
                    )
                )
                _record_llm_trace_error(
                    request_id=request_id,
                    feature=self.feature,
                    provider_id=provider_id,
                    cfg=cfg,
                    attempt_index=attempt_index,
                    status=status,
                    streaming=True,
                    duration_ms=(time.perf_counter() - t0) * 1000,
                    error=e,
                    output_chars=output_chars,
                )
                if yielded:
                    raise
                fallback_from = provider_id
                if not self._should_fallback(e):
                    break
        if last_error:
            raise last_error
        raise RuntimeError("没有可用的 LLM provider")

    def _ordered_provider_ids(self) -> list[str]:
        configured = llm_config.providers
        feature_order = llm_config.feature_provider_order.get(self.feature, [])
        order = self.provider_order or feature_order or llm_config.fallback_order or [llm_config.default]
        return [provider_id for provider_id in order if provider_id in configured]

    def _skip_provider(self, provider_id: str) -> bool:
        remaining = provider_cooldown_remaining(provider_id)
        if remaining > 0:
            logger.info("跳过 LLM provider cooldown: provider={}, remaining={:.1f}s", provider_id, remaining)
            return True
        return False

    def _build_provider_model(self, provider_id: str) -> BaseChatModel:
        cfg = llm_config.providers[provider_id]
        enable_thinking = bool(ENABLE_THINKING if self.thinking is None else self.thinking)
        enable_thinking = enable_thinking and cfg.supports_thinking
        logger.info(
            "初始化 LLM: provider_id={}, provider={}, model={}, thinking={}",
            provider_id,
            cfg.provider,
            cfg.model,
            "enabled" if enable_thinking else "disabled",
        )
        kwargs: dict[str, Any] = {
            "model": cfg.model,
            "api_key": cfg.api_key,
            "max_tokens": cfg.max_tokens,
        }
        if cfg.provider == "deepseek":
            kwargs["api_base"] = cfg.base_url
            kwargs["extra_body"] = {"thinking": {"type": "enabled" if enable_thinking else "disabled"}}
            if enable_thinking:
                kwargs["reasoning_effort"] = REASONING_EFFORT
            elif cfg.temperature is not None:
                kwargs["temperature"] = cfg.temperature
            model: BaseChatModel = _ChatDeepSeekThinking(**kwargs)
        else:
            kwargs["base_url"] = cfg.base_url
            if cfg.temperature is not None:
                kwargs["temperature"] = cfg.temperature
            if cfg.model.startswith("kimi-k2.") and cfg.supports_thinking:
                thinking_body: dict[str, Any] = {"type": "enabled" if enable_thinking else "disabled"}
                if enable_thinking:
                    thinking_body["keep"] = "all"
                kwargs["extra_body"] = {"thinking": thinking_body}
                model = _ChatKimiThinking(**kwargs)
            else:
                model = ChatOpenAI(**kwargs)
        return model

    def _invoke_provider_sync(
        self,
        model: BaseChatModel,
        messages: list[BaseMessage],
        *,
        stop: list[str] | None,
        run_manager: Any | None,
        **kwargs: Any,
    ) -> ChatResult:
        if self.bound_tools is None:
            return model._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        bound = model.bind_tools(self.bound_tools, **self.bind_kwargs)
        message = bound.invoke(messages, stop=stop, **kwargs)
        return ChatResult(generations=[ChatGeneration(message=message)])

    async def _invoke_provider_async(
        self,
        model: BaseChatModel,
        messages: list[BaseMessage],
        *,
        stop: list[str] | None,
        run_manager: Any | None,
        **kwargs: Any,
    ) -> ChatResult:
        if self.bound_tools is None:
            return await model._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)
        bound = model.bind_tools(self.bound_tools, **self.bind_kwargs)
        message = await bound.ainvoke(messages, stop=stop, **kwargs)
        return ChatResult(generations=[ChatGeneration(message=message)])

    def _stream_provider_sync(
        self,
        model: BaseChatModel,
        messages: list[BaseMessage],
        *,
        stop: list[str] | None,
        run_manager: Any | None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        if self.bound_tools is None:
            yield from model._stream(messages, stop=stop, run_manager=run_manager, **kwargs)
            return
        bound = model.bind_tools(self.bound_tools, **self.bind_kwargs)
        for message_chunk in bound.stream(messages, stop=stop, **kwargs):
            yield ChatGenerationChunk(message=message_chunk)

    async def _stream_provider_async(
        self,
        model: BaseChatModel,
        messages: list[BaseMessage],
        *,
        stop: list[str] | None,
        run_manager: Any | None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        if self.bound_tools is None:
            async for chunk in model._astream(messages, stop=stop, run_manager=run_manager, **kwargs):
                yield chunk
            return
        bound = model.bind_tools(self.bound_tools, **self.bind_kwargs)
        async for message_chunk in bound.astream(messages, stop=stop, **kwargs):
            yield ChatGenerationChunk(message=message_chunk)

    def _record_from_result(
        self,
        result: ChatResult,
        *,
        request_id: str,
        cfg: LLMProviderConfig,
        provider_id: str,
        attempt_index: int,
        fallback_from: str | None,
        started_at: datetime,
        duration_ms: float,
        messages: list[BaseMessage],
    ) -> LLMCallRecord:
        usage = _usage_from_result(result)
        output_chars = _result_output_chars(result)
        estimated_prompt = _estimated_message_tokens(messages)
        estimated_completion = _estimated_text_tokens(_result_output_text(result))
        prompt_tokens = usage.get("prompt_tokens") or estimated_prompt
        completion_tokens = usage.get("completion_tokens") or estimated_completion
        total_tokens = usage.get("total_tokens") or prompt_tokens + completion_tokens
        token_count_source = "actual" if usage.get("total_tokens") is not None else "estimated"
        tps = _token_tps(completion_tokens, duration_ms) or _estimated_tps(output_chars, duration_ms)
        return LLMCallRecord(
            request_id=request_id,
            feature=self.feature,
            provider=provider_id,
            model=cfg.model,
            attempt_index=attempt_index,
            fallback_from=fallback_from,
            status="success",
            streaming=False,
            started_at=started_at,
            first_token_ms=duration_ms,
            duration_ms=duration_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            output_chars=output_chars,
            tokens_per_second=tps,
            meta=_record_meta(
                token_count_source=token_count_source,
                estimated_total_tokens=estimated_prompt + estimated_completion,
            ),
        )

    def _record_error(
        self,
        *,
        request_id: str,
        cfg: LLMProviderConfig,
        provider_id: str,
        attempt_index: int,
        fallback_from: str | None,
        started_at: datetime,
        duration_ms: float,
        status: str,
        error: Exception,
        streaming: bool = False,
        first_token_ms: float | None = None,
        output_chars: int = 0,
    ) -> LLMCallRecord:
        return LLMCallRecord(
            request_id=request_id,
            feature=self.feature,
            provider=provider_id,
            model=cfg.model,
            attempt_index=attempt_index,
            fallback_from=fallback_from,
            status=status,
            streaming=streaming,
            started_at=started_at,
            first_token_ms=first_token_ms,
            duration_ms=duration_ms,
            output_chars=output_chars,
            error_type=error.__class__.__name__,
            error_message=str(error),
            meta=_record_meta(),
        )

    @staticmethod
    def _should_fallback(error: Exception) -> bool:
        status = getattr(error, "status_code", None) or getattr(getattr(error, "response", None), "status_code", None)
        return (
            _is_rate_limit(error)
            or _is_provider_exhausted(error)
            or status in {500, 502, 503, 504}
            or isinstance(error, TimeoutError)
        )


def create_llm(
    thinking: bool | None = None,
    *,
    feature: str = "agent",
    provider_order: list[str] | None = None,
) -> BaseChatModel:
    """
    创建 LangChain ChatDeepSeek 实例（带思考模式回传补丁）。

    Args:
        thinking: 显式指定是否开启思考模式；缺省 None 使用全局 ENABLE_THINKING。
                  sub-agent 场景建议传 False，避开“思考+多轮 tool call”的兼容性问题。

    注意：DeepSeek 思考模式下 temperature / top_p / presence_penalty /
    frequency_penalty 会被服务端静默忽略，所以仅在非思考模式下传 temperature。
    """
    return LLMRouter(thinking=thinking, feature=feature, provider_order=provider_order or [])


def _inject_reasoning_content_into_payload(model: Any, input_: LanguageModelInput, payload: dict) -> None:
    """Copy LangChain AIMessage reasoning_content into outgoing assistant payloads."""
    try:
        lc_messages = model._convert_input(input_).to_messages()
    except Exception:  # noqa: BLE001
        return

    ai_messages_iter = iter(m for m in lc_messages if isinstance(m, AIMessage))
    for msg in payload.get("messages", []):
        if msg.get("role") != "assistant":
            continue
        try:
            lc_ai = next(ai_messages_iter)
        except StopIteration:
            break
        reasoning = lc_ai.additional_kwargs.get("reasoning_content")
        if reasoning and not msg.get("reasoning_content"):
            msg["reasoning_content"] = reasoning


def _response_to_dict(response: Any) -> dict[str, Any]:
    if isinstance(response, dict):
        return response
    model_dump = getattr(response, "model_dump", None)
    if callable(model_dump):
        try:
            dumped = model_dump()
            return dumped if isinstance(dumped, dict) else {}
        except Exception:  # noqa: BLE001
            return {}
    return {}


def _extract_reasoning_content(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        raw = value.get("reasoning_content")
    else:
        raw = getattr(value, "reasoning_content", None)
    return raw if isinstance(raw, str) else str(raw or "")


def _extract_typed_response_reasoning(response: Any, index: int) -> str:
    try:
        message = response.choices[index].message
    except Exception:  # noqa: BLE001
        return ""
    return _extract_reasoning_content(message)


def _extract_stream_reasoning_content(chunk: Any) -> str:
    choices = chunk.get("choices") if isinstance(chunk, dict) else getattr(chunk, "choices", None)
    if not choices:
        return ""
    choice = choices[0]
    delta = choice.get("delta") if isinstance(choice, dict) else getattr(choice, "delta", None)
    return _extract_reasoning_content(delta)


def _is_rate_limit(error: Exception) -> bool:
    status = getattr(error, "status_code", None) or getattr(getattr(error, "response", None), "status_code", None)
    if status == 429:
        return True
    text = f"{error.__class__.__name__}: {error}".lower()
    return "rate limit" in text or "too many requests" in text or "429" in text


def _is_provider_exhausted(error: Exception) -> bool:
    status = getattr(error, "status_code", None) or getattr(getattr(error, "response", None), "status_code", None)
    if status == 402:
        return True
    text = f"{error.__class__.__name__}: {error}".lower()
    exhausted_markers = (
        "insufficient balance",
        "insufficient quota",
        "quota exceeded",
        "billing",
        "no credit",
        "余额不足",
        "余额不够",
        "欠费",
    )
    return any(marker in text for marker in exhausted_markers)


def _should_cooldown_provider(error: Exception) -> bool:
    return _is_rate_limit(error) or _is_provider_exhausted(error)


def _error_status(error: Exception) -> str:
    if _is_rate_limit(error):
        return "rate_limited"
    if _is_provider_exhausted(error):
        return "provider_exhausted"
    return "error"


def _usage_from_result(result: ChatResult) -> dict[str, int]:
    usage: dict[str, Any] = {}
    if isinstance(result.llm_output, dict):
        raw = result.llm_output.get("token_usage") or result.llm_output.get("usage") or {}
        if isinstance(raw, dict):
            usage.update(raw)
    if result.generations:
        message = result.generations[0].message
        raw = getattr(message, "usage_metadata", None)
        if isinstance(raw, dict):
            usage.update(raw)
    prompt = usage.get("prompt_tokens") or usage.get("input_tokens")
    completion = usage.get("completion_tokens") or usage.get("output_tokens")
    total = usage.get("total_tokens")
    if total is None and prompt is not None and completion is not None:
        total = int(prompt) + int(completion)
    return {
        "prompt_tokens": int(prompt) if prompt is not None else None,
        "completion_tokens": int(completion) if completion is not None else None,
        "total_tokens": int(total) if total is not None else None,
    }


def _result_output_chars(result: ChatResult) -> int:
    return len(_result_output_text(result))


def _result_output_text(result: ChatResult) -> str:
    if not result.generations:
        return ""
    content = result.generations[0].message.content
    if isinstance(content, str):
        return content
    return str(content or "")


def _chunk_text(chunk: ChatGenerationChunk) -> str:
    content = getattr(getattr(chunk, "message", None), "content", "")
    if isinstance(content, str):
        return content
    return str(content or "")


def _chunk_reasoning(chunk: ChatGenerationChunk) -> str:
    message = getattr(chunk, "message", None)
    additional_kwargs = dict(getattr(message, "additional_kwargs", None) or {})
    value = additional_kwargs.get("reasoning_content")
    return value if isinstance(value, str) else str(value or "")


def _token_tps(completion_tokens: int | None, duration_ms: float) -> float | None:
    if not completion_tokens or duration_ms <= 0:
        return None
    return round(completion_tokens / (duration_ms / 1000), 2)


def _estimated_tps(output_chars: int, duration_ms: float) -> float | None:
    if output_chars <= 0 or duration_ms <= 0:
        return None
    estimated_tokens = max(1, output_chars / 4)
    return round(estimated_tokens / (duration_ms / 1000), 2)


def _estimated_message_tokens(messages: list[BaseMessage]) -> int:
    text = "\n\n".join(_message_text_for_estimate(message) for message in messages)
    return _estimated_text_tokens(text)


def _message_text_for_estimate(message: BaseMessage) -> str:
    role = getattr(message, "type", None) or message.__class__.__name__
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


def _record_llm_trace_request(
    *,
    request_id: str,
    feature: str,
    provider_id: str,
    cfg: LLMProviderConfig,
    attempt_index: int,
    fallback_from: str | None,
    streaming: bool,
    messages: list[BaseMessage],
) -> None:
    meta = current_llm_context()
    record_trace_event(
        trace_id=str(meta.get("trace_id") or ""),
        event_type="llm_request",
        project_id=str(meta.get("project_id") or ""),
        session_id=str(meta.get("session_id") or ""),
        topic_thread_id=str(meta.get("topic_thread_id") or ""),
        payload={
            "request_id": request_id,
            "feature": feature,
            "provider": provider_id,
            "model": cfg.model,
            "attempt_index": attempt_index,
            "fallback_from": fallback_from,
            "streaming": streaming,
            "messages": [_serialize_message(message) for message in messages],
        },
    )


def _record_llm_trace_response(
    *,
    request_id: str,
    feature: str,
    provider_id: str,
    cfg: LLMProviderConfig,
    attempt_index: int,
    status: str,
    streaming: bool,
    duration_ms: float,
    result: ChatResult,
) -> None:
    message = result.generations[0].message if result.generations else None
    _record_llm_trace_payload(
        request_id=request_id,
        feature=feature,
        provider_id=provider_id,
        cfg=cfg,
        attempt_index=attempt_index,
        status=status,
        streaming=streaming,
        duration_ms=duration_ms,
        payload={
            "message": _serialize_message(message) if isinstance(message, BaseMessage) else None,
            "usage": _usage_from_result(result),
        },
    )


def _record_llm_trace_stream_response(
    *,
    request_id: str,
    feature: str,
    provider_id: str,
    cfg: LLMProviderConfig,
    attempt_index: int,
    status: str,
    streaming: bool,
    duration_ms: float,
    content: str,
    reasoning_content: str,
    output_chars: int,
) -> None:
    _record_llm_trace_payload(
        request_id=request_id,
        feature=feature,
        provider_id=provider_id,
        cfg=cfg,
        attempt_index=attempt_index,
        status=status,
        streaming=streaming,
        duration_ms=duration_ms,
        payload={
            "message": {
                "role": "ai",
                "content": content,
                "reasoning_content": reasoning_content,
            },
            "output_chars": output_chars,
        },
    )


def _record_llm_trace_error(
    *,
    request_id: str,
    feature: str,
    provider_id: str,
    cfg: LLMProviderConfig,
    attempt_index: int,
    status: str,
    streaming: bool,
    duration_ms: float,
    error: Exception,
    output_chars: int = 0,
) -> None:
    _record_llm_trace_payload(
        request_id=request_id,
        feature=feature,
        provider_id=provider_id,
        cfg=cfg,
        attempt_index=attempt_index,
        status=status,
        streaming=streaming,
        duration_ms=duration_ms,
        event_type="error",
        payload={
            "where": "llm_client",
            "error_type": error.__class__.__name__,
            "error": str(error),
            "output_chars": output_chars,
        },
    )


def _record_llm_trace_payload(
    *,
    request_id: str,
    feature: str,
    provider_id: str,
    cfg: LLMProviderConfig,
    attempt_index: int,
    status: str,
    streaming: bool,
    duration_ms: float,
    payload: dict[str, Any],
    event_type: str = "llm_response",
) -> None:
    meta = current_llm_context()
    record_trace_event(
        trace_id=str(meta.get("trace_id") or ""),
        event_type=event_type,
        project_id=str(meta.get("project_id") or ""),
        session_id=str(meta.get("session_id") or ""),
        topic_thread_id=str(meta.get("topic_thread_id") or ""),
        payload={
            "request_id": request_id,
            "feature": feature,
            "provider": provider_id,
            "model": cfg.model,
            "attempt_index": attempt_index,
            "status": status,
            "streaming": streaming,
            "duration_ms": round(duration_ms, 3),
            **payload,
        },
    )


def _serialize_message(message: BaseMessage | None) -> dict[str, Any]:
    if message is None:
        return {}
    content = getattr(message, "content", "")
    additional_kwargs = dict(getattr(message, "additional_kwargs", None) or {})
    payload = {
        "role": getattr(message, "type", message.__class__.__name__),
        "content": content,
    }
    tool_calls = getattr(message, "tool_calls", None)
    if tool_calls:
        payload["tool_calls"] = tool_calls
    invalid_tool_calls = getattr(message, "invalid_tool_calls", None)
    if invalid_tool_calls:
        payload["invalid_tool_calls"] = invalid_tool_calls
    reasoning = additional_kwargs.get("reasoning_content")
    if reasoning:
        payload["reasoning_content"] = reasoning
    name = getattr(message, "name", None)
    if name:
        payload["name"] = name
    return payload


def _estimated_output_tokens(output_chars: int) -> int:
    return max(1, output_chars // 4) if output_chars > 0 else 0


def _estimated_text_tokens(text: str) -> int:
    if not text:
        return 0
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    non_ascii_chars = len(text) - ascii_chars
    return max(1, ascii_chars // 4 + non_ascii_chars // 2)


def _record_meta(**extra: Any) -> dict[str, Any]:
    meta = current_llm_context()
    meta.update({key: value for key, value in extra.items() if value not in (None, "")})
    return meta
