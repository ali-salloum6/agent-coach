"""Tests for photos and PDFs staying visible to the model until /new."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("OPENROUTER_API_KEY", "test-key")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import attachments  # noqa: E402
import bot  # noqa: E402
import config  # noqa: E402
import llm  # noqa: E402


def _url(raw: bytes, mime: str = "image/jpeg") -> str:
    return attachments.image_part(raw, mime)["image_url"]["url"]


def _images(messages: list[dict]) -> list[str]:
    """The image data URLs in the messages, oldest first."""
    return [
        part["image_url"]["url"]
        for message in messages if isinstance(message.get("content"), list)
        for part in message["content"] if part.get("type") == "image_url"
    ]


class ExpandTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.object(config, "DATA_DIR", Path(tmp)))

    def _photos(self, *files: bytes) -> dict:
        parts = [{"type": "text", "text": "The user attached photos."}]
        parts += [
            attachments.save("coach", raw, label=f"Image {i}", mime_type="image/jpeg")
            for i, raw in enumerate(files, start=1)
        ]
        return {"role": "user", "content": parts}

    def test_files_go_back_into_every_later_request(self) -> None:
        history = [
            {"role": "system", "content": "sys"},
            self._photos(b"menu-1", b"menu-2"),
            {"role": "assistant", "content": "Try the omelette."},
            {"role": "user", "content": "Something cheaper?"},
        ]
        saved = json.dumps(history)
        messages = attachments.expand("coach", history, max_files=10, max_bytes=10_000)
        self.assertEqual(_images(messages), [_url(b"menu-1"), _url(b"menu-2")])
        self.assertEqual(messages[1]["content"][0]["text"], "The user attached photos.")
        # The saved history is untouched and holds references, not the files.
        self.assertEqual(json.dumps(history), saved)
        self.assertNotIn("base64", saved)

    def test_newest_files_win_when_over_a_limit(self) -> None:
        history = [self._photos(b"old"), self._photos(b"mid"), self._photos(b"new")]
        for limits in ({"max_files": 2, "max_bytes": 10_000}, {"max_files": 10, "max_bytes": 6}):
            messages = attachments.expand("coach", history, **limits)
            self.assertEqual(_images(messages), [_url(b"mid"), _url(b"new")], limits)
            self.assertIn("Image 1 from this message is no longer attached", messages[0]["content"][1]["text"])

    def test_a_missing_file_becomes_a_note(self) -> None:
        history = [self._photos(b"gone")]
        attachments.clear("coach")
        messages = attachments.expand("coach", history, max_files=10, max_bytes=10_000)
        self.assertEqual(_images(messages), [])
        self.assertIn("no longer available", messages[0]["content"][1]["text"])

    def test_pdfs_go_back_as_files(self) -> None:
        ref = attachments.save(
            "coach", b"%PDF-1.4", label="PDF 1 (labs.pdf)", mime_type="application/pdf", file_name="labs.pdf"
        )
        [message] = attachments.expand("coach", [{"role": "user", "content": [ref]}], max_files=10, max_bytes=10_000)
        part = message["content"][0]
        self.assertEqual(part["file"]["filename"], "labs.pdf")
        self.assertTrue(part["file"]["file_data"].startswith("data:application/pdf;base64,"))


class PhotoConversationTests(unittest.IsolatedAsyncioTestCase):
    async def test_photos_stay_visible_until_new(self) -> None:
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        requests: list[list[dict]] = []

        async def fake_stream(**kwargs):
            requests.append(kwargs["messages"])
            yield ("content", "Noted.")

        message = SimpleNamespace(reply_text=AsyncMock(), chat=SimpleNamespace(send_action=AsyncMock()))
        update = SimpleNamespace(message=message, effective_chat=SimpleNamespace(id=1, type="private"))
        ctx = SimpleNamespace(bot=SimpleNamespace(do_api_request=AsyncMock()))
        history = [{"role": "system", "content": "sys"}]
        photo = {"kind": "photo", "file_id": "p1", "caption": "menu", "mime_type": "image/jpeg"}
        with patch.object(config, "DATA_DIR", Path(tmp)), \
                patch.dict(bot.session, {"model": "openai/gpt-6.1-sol", "history": history,
                                         "tts": False, "web_search": False}), \
                patch.object(bot, "_is_allowed_user", return_value=True), \
                patch.object(bot, "_download_telegram_file", AsyncMock(return_value=b"menu-photo")), \
                patch.object(bot.llm, "chat_stream", fake_stream), \
                patch.object(bot, "_send_reply", AsyncMock()), \
                patch.object(bot, "_persist_session"), \
                patch.object(bot, "_background_extract", AsyncMock()), \
                patch.object(bot.memory_v2, "enabled", return_value=False):
            await bot._handle_media_bundle(ctx, update, [photo])
            await bot._conversation_reply(
                update, ctx, user_content="Cheaper options?", memory_user_text="Cheaper options?",
                stream=True, web_search=False, allow_fallback=True,
            )
            self.assertNotIn("base64", json.dumps(history))
            with patch.object(bot.memory, "prune_recent", return_value=0), \
                    patch.object(bot.memory, "load", return_value=""):
                bot._reset_session()
            self.assertFalse(attachments.folder(bot.AGENT.slug).exists())
        self.assertEqual(_images(requests[0]), [_url(b"menu-photo")])
        self.assertEqual(_images(requests[1]), [_url(b"menu-photo")])


class TextOnlyModelTests(unittest.IsolatedAsyncioTestCase):
    def _client(self, response: httpx.Response):
        transport = httpx.MockTransport(lambda request: response)
        return patch.object(llm, "_httpx_client", lambda timeout: httpx.AsyncClient(transport=transport))

    async def test_models_that_cannot_see_images_get_a_note(self) -> None:
        listing = {"data": [
            {"id": "z-ai/glm-5", "architecture": {"input_modalities": ["text"]}},
            {"id": "openai/gpt-6.1-sol", "architecture": {"input_modalities": ["text", "image", "file"]}},
        ]}
        messages = [{"role": "user", "content": [
            {"type": "text", "text": "menu"}, attachments.image_part(b"x", "image/png"),
        ]}]
        with patch.dict(llm._model_inputs, clear=True), self._client(httpx.Response(200, json=listing)):
            await llm.load_model_inputs()
            glm = llm._build_request_body(messages, "z-ai/glm-5", False)["messages"]
            gpt = llm._build_request_body(messages, "openai/gpt-6.1-sol", False)["messages"]
            unknown = llm._build_request_body(messages, "some/new-model", False)["messages"]
        self.assertEqual(_images(glm), [])
        self.assertIn("cannot see images", glm[0]["content"][1]["text"])
        self.assertEqual(len(_images(gpt)), 1)
        self.assertEqual(len(_images(unknown)), 1)
        self.assertEqual(len(_images(messages)), 1)

    async def test_a_failed_listing_keeps_images(self) -> None:
        with patch.dict(llm._model_inputs, clear=True), self._client(httpx.Response(503)), \
                self.assertLogs("llm", "ERROR"):
            await llm.load_model_inputs()
            self.assertTrue(llm.reads_images("z-ai/glm-5"))


if __name__ == "__main__":
    unittest.main()
