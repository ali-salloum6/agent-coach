"""Tests for TTS text cleaning (no Kokoro/torch load in CI)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tts  # noqa: E402


class ReplyToSpeechTextTests(unittest.TestCase):
    def test_strips_bold_and_italic(self) -> None:
        out = tts.reply_to_speech_text("Hello **bold** and *italic* world.")
        self.assertEqual(out, "Hello bold and italic world.")

    def test_strips_link_keeps_label(self) -> None:
        out = tts.reply_to_speech_text("See [OpenRouter](https://openrouter.ai/) for details.")
        self.assertEqual(out, "See OpenRouter for details.")

    def test_strips_inline_code_keeps_content(self) -> None:
        out = tts.reply_to_speech_text("Run `pip install kokoro` first.")
        self.assertEqual(out, "Run pip install kokoro first.")

    def test_omits_fenced_code_blocks(self) -> None:
        raw = "Before\n```python\nprint('hi')\n```\nAfter"
        out = tts.reply_to_speech_text(raw)
        self.assertIn("Before", out)
        self.assertIn("After", out)
        self.assertNotIn("print", out)
        self.assertNotIn("```", out)

    def test_strips_headers(self) -> None:
        out = tts.reply_to_speech_text("## Section title\n\nBody text.")
        self.assertEqual(out, "Section title\n\nBody text.")

    def test_strips_html_tags(self) -> None:
        out = tts.reply_to_speech_text("Use <b>bold</b> and <code>slug</code>.")
        self.assertEqual(out, "Use bold and slug.")

    def test_strips_list_markers(self) -> None:
        out = tts.reply_to_speech_text("* first\n- second\n1. third")
        self.assertEqual(out, "first\nsecond\nthird")

    def test_empty_after_cleaning(self) -> None:
        out = tts.reply_to_speech_text("```\nonly code\n```")
        self.assertEqual(out, "")
        self.assertFalse(tts.is_speakable("```\nonly code\n```"))

    def test_is_speakable_true_for_plain_text(self) -> None:
        self.assertTrue(tts.is_speakable("Hello world."))

    def test_normalizes_tilde_and_units(self) -> None:
        out = tts.reply_to_speech_text("Weigh ~95 kg and take 200mg.")
        self.assertIn("about 95", out)
        self.assertIn("kilograms", out)
        self.assertIn("milligrams", out)
        self.assertNotIn("~", out)
        self.assertNotIn(" kg", out.lower())

    def test_normalizes_grams_not_inside_words(self) -> None:
        out = tts.reply_to_speech_text("Eat 30g protein. Good job.")
        self.assertIn("30 grams", out)
        self.assertIn("Good job", out)


class SynthesizeTests(unittest.TestCase):
    def _fake_soundfile_module(self) -> MagicMock:
        fake_sf = MagicMock()

        def _capture_write(buf, data, rate, format="WAV"):
            buf.write(b"RIFFfake")

        fake_sf.write.side_effect = _capture_write
        return fake_sf

    @patch("tts._get_pipeline")
    def test_synthesize_concatenates_chunks(self, mock_get_pipeline: MagicMock) -> None:
        import numpy as np

        chunk_a = np.zeros(100, dtype=np.float32)
        chunk_b = np.ones(50, dtype=np.float32)
        mock_pipeline = MagicMock()
        mock_pipeline.return_value = [("a", "a", chunk_a), ("b", "b", chunk_b)]
        mock_get_pipeline.return_value = mock_pipeline

        fake_sf = self._fake_soundfile_module()
        with patch.dict(sys.modules, {"soundfile": fake_sf}):
            wav = tts.synthesize_to_wav_bytes("Hello world.")

        self.assertIsInstance(wav, bytes)
        self.assertTrue(wav.startswith(b"RIFF"))
        mock_pipeline.assert_called_once()
        call_kwargs = mock_pipeline.call_args
        self.assertEqual(call_kwargs.kwargs.get("split_pattern"), r"\n+")

    @patch("tts._get_pipeline")
    def test_synthesize_raises_on_empty_clean_text(self, mock_get_pipeline: MagicMock) -> None:
        with self.assertRaises(ValueError) as ctx:
            tts.synthesize_to_wav_bytes("```\n```")
        self.assertIn("No speakable text", str(ctx.exception))
        mock_get_pipeline.assert_not_called()

    @patch("tts._get_pipeline")
    def test_synthesize_raises_when_no_audio_chunks(self, mock_get_pipeline: MagicMock) -> None:
        mock_pipeline = MagicMock()
        mock_pipeline.return_value = []
        mock_get_pipeline.return_value = mock_pipeline

        fake_sf = self._fake_soundfile_module()
        with patch.dict(sys.modules, {"soundfile": fake_sf}):
            with self.assertRaises(ValueError) as ctx:
                tts.synthesize_to_wav_bytes("Hello.")
        self.assertIn("no audio", str(ctx.exception).lower())


if __name__ == "__main__":
    unittest.main()
