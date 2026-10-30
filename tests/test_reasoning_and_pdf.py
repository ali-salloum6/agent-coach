"""Tests for reasoning streaming and PDF attachments."""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("OPENROUTER_API_KEY", "test-key")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot  # noqa: E402
import config  # noqa: E402
import llm  # noqa: E402


def _sse(*chunks: dict) -> bytes:
    lines = [": OPENROUTER PROCESSING"]
    lines += [f"data: {json.dumps(c)}" for c in chunks]
    lines.append("data: [DONE]")
    return ("\n\n".join(lines) + "\n\n").encode()


def _delta(**delta) -> dict:
    return {"choices": [{"delta": delta}]}


class RequestBodyTests(unittest.TestCase):
    def test_gpt6_gets_configured_effort(self) -> None:
        with patch.object(config, "OPENAI_REASONING_EFFORT", "xhigh"):
            body = llm._build_request_body([], "openai/gpt-6.1-sol", False)
        self.assertEqual(body["reasoning"], {"effort": "xhigh"})

    def test_verbosity_only_when_set_and_only_for_openai(self) -> None:
        body = llm._build_request_body([], "openai/gpt-6.1-sol", False)
        self.assertNotIn("verbosity", body)
        with patch.object(config, "OPENAI_VERBOSITY", "high"):
            body = llm._build_request_body([], "openai/gpt-6.1-sol", False)
            self.assertEqual(body["verbosity"], "high")
            body = llm._build_request_body([], "z-ai/glm-5.2", False)
            self.assertNotIn("verbosity", body)


class StreamParsingTests(unittest.IsolatedAsyncioTestCase):
    async def test_yields_reasoning_then_content(self) -> None:
        payload = _sse(
            _delta(role="assistant"),
            _delta(
                reasoning="**Planning**\n\nThinking about it.",
                reasoning_details=[{"type": "reasoning.summary"}],
            ),
            _delta(reasoning_details=[{"type": "reasoning.encrypted", "data": "x"}]),
            _delta(content="Hello"),
            _delta(content=" there"),
        )
        transport = httpx.MockTransport(lambda request: httpx.Response(200, content=payload))

        def client(timeout: float) -> httpx.AsyncClient:
            return httpx.AsyncClient(transport=transport, timeout=timeout)

        with patch.object(llm, "_httpx_client", client):
            got = [d async for d in llm.chat_stream([], model="openai/gpt-6.1-sol")]
        self.assertEqual(
            got,
            [
                ("reasoning", "**Planning**\n\nThinking about it."),
                ("content", "Hello"),
                ("content", " there"),
            ],
        )


class ReasoningDraftTextTests(unittest.TestCase):
    def test_sections_are_split_and_markers_dropped(self) -> None:
        text = bot._reasoning_draft_text(
            "**Checking knee**\n\nTracks inward.**Fitting the time**\n\nFifty-five minutes."
        )
        self.assertEqual(
            text,
            f"{bot.THINKING_DRAFT}\n\nChecking knee\n\nTracks inward.\n\n"
            "Fitting the time\n\nFifty-five minutes.",
        )

    def test_long_reasoning_keeps_the_tail(self) -> None:
        text = bot._reasoning_draft_text("a" * 10_000 + "END")
        self.assertLessEqual(len(text), bot.MAX_TG_LEN)
        self.assertTrue(text.endswith("END"))
        self.assertIn("…", text)


class ConversationDraftTests(unittest.IsolatedAsyncioTestCase):
    async def test_drafts_show_thinking_then_answer_and_history_skips_reasoning(self) -> None:
        async def fake_stream(**kwargs):
            yield ("reasoning", "**Weighing options**\n\nComparing two plans in detail.")
            yield ("content", "Do the second plan, it fits your schedule.")

        drafts: list[str] = []

        async def do_api_request(method: str, params: dict) -> None:
            drafts.append(params["text"])

        message = SimpleNamespace(
            reply_text=AsyncMock(),
            chat=SimpleNamespace(send_action=AsyncMock()),
        )
        update = SimpleNamespace(
            message=message,
            effective_chat=SimpleNamespace(id=1, type="private"),
        )
        ctx = SimpleNamespace(bot=SimpleNamespace(do_api_request=do_api_request))
        history = [{"role": "system", "content": "sys"}]

        with patch.dict(bot.session, {"model": "openai/gpt-6.1-sol", "history": history, "tts": False}), \
                patch.object(bot.llm, "chat_stream", fake_stream), \
                patch.object(bot, "_persist_session"), \
                patch.object(bot.memory, "extract_and_save", AsyncMock()):
            await bot._conversation_reply(
                update,
                ctx,
                user_content="hi",
                memory_user_text="hi",
                stream=True,
                web_search=False,
                allow_fallback=True,
            )

        self.assertEqual(drafts[0], bot.THINKING_DRAFT)
        self.assertIn("Weighing options\n\nComparing two plans in detail.", drafts[1])
        self.assertEqual(drafts[-1], "Do the second plan, it fits your schedule.")
        self.assertEqual(
            history[-1],
            {"role": "assistant", "content": "Do the second plan, it fits your schedule."},
        )
        message.reply_text.assert_awaited()


class PdfAttachmentTests(unittest.TestCase):
    def _message(self, mime: str, name: str | None = "labs.pdf") -> SimpleNamespace:
        return SimpleNamespace(
            photo=None,
            caption="my blood work",
            document=SimpleNamespace(mime_type=mime, file_id="f1", file_name=name),
        )

    def test_pdf_document_becomes_pdf_item(self) -> None:
        item = bot._media_item_from_message(self._message("application/pdf"))
        self.assertEqual(item["kind"], "pdf")
        self.assertEqual(item["file_name"], "labs.pdf")

    def test_other_documents_are_ignored(self) -> None:
        self.assertIsNone(bot._media_item_from_message(self._message("text/plain")))

    def test_pdf_part_is_openrouter_file(self) -> None:
        part = bot._openrouter_pdf_part(b"%PDF-1.4", "labs.pdf")
        self.assertEqual(part["type"], "file")
        self.assertEqual(part["file"]["filename"], "labs.pdf")
        self.assertTrue(part["file"]["file_data"].startswith("data:application/pdf;base64,"))

    def test_memory_summary_names_pdfs(self) -> None:
        items = [
            {"kind": "photo", "caption": None},
            {"kind": "pdf", "caption": "my blood work", "file_name": "labs.pdf"},
        ]
        self.assertEqual(
            bot._attachment_memory_summary("2026-10-30 12:00", items),
            "[Sent at 2026-10-30 12:00]\n"
            "User sent 1 image(s) and 1 PDF(s).\n"
            "Image 1 caption: (none)\n"
            "PDF 2 (labs.pdf) caption: my blood work",
        )


if __name__ == "__main__":
    unittest.main()
