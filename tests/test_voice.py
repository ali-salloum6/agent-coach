"""Tests for voice messages: transcription, replies on every failure, and the catch-all."""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("OPENROUTER_API_KEY", "test-key")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot  # noqa: E402
import config  # noqa: E402
import stt  # noqa: E402


def _update(duration: int = 12, caption: str | None = None) -> SimpleNamespace:
    message = SimpleNamespace(
        voice=SimpleNamespace(file_id="v1", duration=duration),
        audio=None,
        video_note=None,
        caption=caption,
        reply_text=AsyncMock(),
        chat=SimpleNamespace(send_action=AsyncMock()),
    )
    return SimpleNamespace(message=message, effective_chat=SimpleNamespace(id=1, type="private"))


class VoiceHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def run_handler(self, update, *, audio=b"ogg", heard=None, error=None, download_error=None):
        conversation = AsyncMock()
        download = AsyncMock(return_value=audio, side_effect=download_error)
        transcribe = AsyncMock(return_value=heard, side_effect=error)
        with patch.object(bot, "_is_allowed_user", return_value=True), \
                patch.object(bot, "_download_telegram_file", download), \
                patch.object(bot.stt, "transcribe", transcribe), \
                patch.object(bot, "_conversation_reply", conversation), \
                patch.dict(bot.session, {"web_search": False}):
            await bot.handle_voice(update, SimpleNamespace(bot=object()))
        replies = [c.args[0] for c in update.message.reply_text.await_args_list]
        return replies, conversation, download

    async def test_transcript_is_echoed_and_answered_like_text(self) -> None:
        update = _update(caption="lunch")
        replies, conversation, _ = await self.run_handler(
            update, heard=stt.Transcript(text=" Had 125 grams of chicken. ", language="en", seconds=4.0))
        self.assertEqual(replies, ["🎙️ Had 125 grams of chicken."])
        kwargs = conversation.await_args.kwargs
        self.assertEqual(kwargs["memory_user_text"], "(Voice message, transcribed) Had 125 grams of chicken.\nCaption: lunch")
        self.assertTrue(kwargs["user_content"].startswith("[Sent at "))
        self.assertTrue(kwargs["user_content"].endswith("Had 125 grams of chicken.\nCaption: lunch"))

    async def test_silence_gets_a_reply(self) -> None:
        replies, conversation, _ = await self.run_handler(_update(), heard=stt.Transcript("", "en", 3.0))
        self.assertEqual(replies, ["I couldn't make out any speech in that voice message."])
        conversation.assert_not_awaited()

    async def test_every_failure_gets_a_reply(self) -> None:
        cases = (
            ({"error": stt.NotAvailable("faster-whisper is not installed")}, "aren't working"),
            ({"error": asyncio.TimeoutError()}, "took too long"),
            ({"error": RuntimeError("decoder blew up")}, "Couldn't transcribe"),
            ({"download_error": RuntimeError("network")}, "Couldn't download"),
            ({"download_error": ValueError("File too large")}, "too large"),
        )
        for kwargs, expected in cases:
            replies, conversation, _ = await self.run_handler(_update(), **kwargs)
            self.assertEqual(len(replies), 1, kwargs)
            self.assertIn(expected, replies[0])
            conversation.assert_not_awaited()

    async def test_too_long_is_refused_before_downloading(self) -> None:
        replies, conversation, download = await self.run_handler(_update(duration=config.MAX_VOICE_SECONDS + 60))
        self.assertIn("I can transcribe up to", replies[0])
        download.assert_not_awaited()

    async def test_unreadable_messages_get_an_answer(self) -> None:
        update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))
        with patch.object(bot, "_is_allowed_user", return_value=True):
            await bot.handle_unsupported(update, SimpleNamespace())
        self.assertIn("Send text, photos, PDFs or voice messages", update.message.reply_text.await_args.args[0])


class SttTests(unittest.TestCase):
    def test_segments_are_joined(self) -> None:
        model = MagicMock()
        model.transcribe.return_value = (iter([SimpleNamespace(text=" Had rice."), SimpleNamespace(text=" And tea. ")]),
                                         SimpleNamespace(language="en", duration=5.0))
        with patch.object(stt, "_get_model", return_value=model), patch.object(stt, "decode", return_value="samples"):
            heard = stt.transcribe_sync(b"ogg")
        self.assertEqual(model.transcribe.call_args.args[0], "samples")
        self.assertEqual((heard.text, heard.language, heard.seconds), ("Had rice. And tea.", "en", 5.0))
        self.assertTrue(model.transcribe.call_args.kwargs["vad_filter"])

    def test_decode_gives_16khz_mono_samples(self) -> None:
        import av
        import numpy as np
        from io import BytesIO
        buf = BytesIO()
        with av.open(buf, "w", format="ogg") as out:
            stream = out.add_stream("libopus", rate=48000)
            stream.layout = "mono"
            tone = (np.sin(np.arange(48000) * 2 * np.pi * 440 / 48000) * 8000).astype(np.int16).reshape(1, -1)
            frame = av.AudioFrame.from_ndarray(tone, format="s16", layout="mono")
            frame.sample_rate = 48000
            for packet in stream.encode(frame):
                out.mux(packet)
            for packet in stream.encode(None):
                out.mux(packet)
        samples = stt.decode(buf.getvalue())
        self.assertEqual(samples.dtype, np.float32)
        self.assertAlmostEqual(len(samples) / stt.SAMPLE_RATE, 1.0, delta=0.1)
        with self.assertRaises(Exception):
            stt.decode(b"not audio")

    def test_switched_off(self) -> None:
        with patch.object(config, "VOICE_ENABLED", False), patch.object(stt, "_model", None):
            with self.assertRaises(stt.NotAvailable):
                stt.transcribe_sync(b"ogg")


if __name__ == "__main__":
    unittest.main()
