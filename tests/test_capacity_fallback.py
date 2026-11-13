"""Tests for the one-time fallback when a provider answers "high demand"."""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("OPENROUTER_API_KEY", "test-key")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import llm  # noqa: E402

BUSY = {"error": {"message": "The system is currently experiencing high demand and cannot process your request. "
                             "Your request exceeds the maximum usage size allowed during peak load."}}
PRIMARY, FALLBACK = "openai/gpt-6.1-sol", "moonshotai/kimi-k3.1-pro"


def _client_for(handler):
    transport = httpx.MockTransport(handler)

    def client(timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, timeout=timeout)

    return client


def _sse(*chunks: dict) -> bytes:
    return ("".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n").encode()


class ChatFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_capacity_error_retries_once_with_the_fallback(self) -> None:
        models = []

        def handler(request: httpx.Request) -> httpx.Response:
            model = json.loads(request.content)["model"]
            models.append(model)
            if model == PRIMARY:
                return httpx.Response(200, json=BUSY)
            return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})

        meta: dict = {}
        with patch.object(llm, "_httpx_client", _client_for(handler)):
            reply = await llm.chat([], model=PRIMARY, response_meta=meta)
        self.assertEqual(reply, "hi")
        self.assertEqual(models, [PRIMARY, FALLBACK])
        self.assertEqual(meta["fallback_used"], (PRIMARY, FALLBACK))

    async def test_no_fallback_when_not_allowed_or_for_other_errors(self) -> None:
        def busy(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=BUSY)

        with patch.object(llm, "_httpx_client", _client_for(busy)):
            with self.assertRaises(RuntimeError):
                await llm.chat([], model=PRIMARY, allow_fallback=False)

        def other(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"error": {"message": "invalid image"}})

        with patch.object(llm, "_httpx_client", _client_for(other)):
            with self.assertRaises(RuntimeError):
                await llm.chat([], model=PRIMARY)


class StreamFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_capacity_error_before_any_text_switches_model(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if json.loads(request.content)["model"] == PRIMARY:
                return httpx.Response(200, content=_sse(BUSY))
            return httpx.Response(200, content=_sse({"choices": [{"delta": {"content": "hello"}}]}))

        meta: dict = {}
        with patch.object(llm, "_httpx_client", _client_for(handler)):
            got = [d async for d in llm.chat_stream([], model=PRIMARY, response_meta=meta)]
        self.assertEqual(got, [("content", "hello")])
        self.assertEqual(meta["fallback_used"], (PRIMARY, FALLBACK))

    async def test_capacity_error_after_text_is_not_retried(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=_sse({"choices": [{"delta": {"content": "par"}}]}, BUSY))

        got = []
        with patch.object(llm, "_httpx_client", _client_for(handler)):
            with self.assertRaises(RuntimeError):
                async for d in llm.chat_stream([], model=PRIMARY):
                    got.append(d)
        self.assertEqual(got, [("content", "par")])


if __name__ == "__main__":
    unittest.main()
