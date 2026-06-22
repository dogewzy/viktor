import asyncio
import contextvars
import json
import unittest
from contextlib import contextmanager
from typing import Iterator

from api.ui_routes import DEFAULT_WEBCHAT_PROVIDER, ChatStreamRequest, _sse_json_lines
from settings import llm_config


_ctx: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar("test_sse_context", default={})


@contextmanager
def _ctx_scope() -> Iterator[None]:
    token = _ctx.set({"trace": "on"})
    try:
        yield
    finally:
        _ctx.reset(token)


class SSEJsonLinesTest(unittest.IsolatedAsyncioTestCase):
    async def test_async_generator_keeps_context_across_yields(self) -> None:
        async def events():
            with _ctx_scope():
                yield {"type": "delta", "text": "hello"}
                await asyncio.sleep(0)
                yield {"type": "done", "full_text": "hello"}

        lines = [line async for line in _sse_json_lines(events())]
        payloads = [
            json.loads(line.removeprefix("data: ").strip())
            for line in lines
            if line.startswith("data: {")
        ]

        self.assertEqual(payloads[0], {"type": "delta", "text": "hello"})
        self.assertEqual(payloads[1], {"type": "done", "full_text": "hello"})
        self.assertNotIn("created in a different Context", "\n".join(lines))
        self.assertEqual(lines[-1], "data: [DONE]\n\n")


class WebChatProviderDefaultTest(unittest.TestCase):
    def test_chat_stream_request_defaults_to_configured_default_provider(self) -> None:
        body = ChatStreamRequest(
            project_id="demo",
            message="hello",
            session_id="web:demo",
            topic_thread_id="12345678",
        )

        self.assertEqual(DEFAULT_WEBCHAT_PROVIDER, llm_config.default)
        self.assertEqual(body.llm_provider, llm_config.default)


if __name__ == "__main__":
    unittest.main()
