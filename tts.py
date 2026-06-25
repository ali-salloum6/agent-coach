"""Text cleaning and Kokoro TTS synthesis for /read."""

from __future__ import annotations

import logging
import re
from io import BytesIO

import config

log = logging.getLogger(__name__)

SAMPLE_RATE = 24000
DEFAULT_VOICE = "bm_george"
MAX_TTS_CHARS = 10_000

_pipelines: dict[str, object] = {}


def _lang_code_for_voice(voice: str) -> str:
    """Map Kokoro voice id to pipeline lang_code (American vs British English)."""
    prefix = voice.split("_", 1)[0]
    if prefix in ("bm", "bf"):
        return "b"
    return "a"


def _get_pipeline(voice: str):
    lang = _lang_code_for_voice(voice)
    if lang not in _pipelines:
        from kokoro import KPipeline

        log.info("Initializing Kokoro TTS pipeline (lang_code=%s, first /read use)", lang)
        _pipelines[lang] = KPipeline(lang_code=lang)
    return _pipelines[lang]


def reply_to_speech_text(text: str) -> str:
    """Strip markdown/HTML/formatting so TTS reads plain words, not markup."""
    if not text:
        return ""

    t = text
    # HTML tags (e.g. if content ever carried Telegram HTML)
    t = re.sub(r"<[^>]+>", "", t)
    # Fenced code blocks — omit entirely
    t = re.sub(r"```[^\n]*\n.*?```", " ", t, flags=re.DOTALL)
    t = re.sub(r"```.*?```", " ", t, flags=re.DOTALL)
    # Inline code — keep spoken content
    t = re.sub(r"`([^`]+)`", r"\1", t)
    # Images ![alt](url) → alt text
    t = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", t)
    # Links [text](url) → link label
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)
    # Bold / italic
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", t, flags=re.DOTALL)
    t = re.sub(r"\*(.+?)\*", r"\1", t, flags=re.DOTALL)
    t = re.sub(r"__(.+?)__", r"\1", t, flags=re.DOTALL)
    t = re.sub(r"_(.+?)_", r"\1", t, flags=re.DOTALL)
    # ATX headers
    t = re.sub(r"^#{1,6}\s+", "", t, flags=re.MULTILINE)
    # List markers
    t = re.sub(r"^[\*\-\+]\s+", "", t, flags=re.MULTILINE)
    t = re.sub(r"^\d+\.\s+", "", t, flags=re.MULTILINE)
    # Horizontal rules
    t = re.sub(r"^[-*_]{3,}\s*$", " ", t, flags=re.MULTILINE)
    # Collapse whitespace
    t = re.sub(r"\n{3,}", "\n\n", t)
    t = re.sub(r"[ \t]+", " ", t)
    return t.strip()


def is_speakable(text: str) -> bool:
    return bool(reply_to_speech_text(text).strip())


def synthesize_to_wav_bytes(text: str, *, voice: str | None = None) -> bytes:
    """Synthesize *text* to in-memory WAV bytes using Kokoro."""
    voice = voice or config.TTS_VOICE
    clean = reply_to_speech_text(text)
    if not clean.strip():
        raise ValueError("No speakable text after cleaning")

    if len(clean) > MAX_TTS_CHARS:
        raise ValueError(
            f"Text too long ({len(clean):,} chars; max {MAX_TTS_CHARS:,})"
        )

    import numpy as np
    import soundfile as sf

    pipeline = _get_pipeline(voice)
    chunks: list = []
    generator = pipeline(
        clean,
        voice=voice,
        speed=1,
        split_pattern=r"\n+",
    )
    for _, _, audio in generator:
        chunks.append(audio)

    if not chunks:
        raise ValueError("Synthesis produced no audio")

    combined = np.concatenate(chunks)
    buf = BytesIO()
    sf.write(buf, combined, SAMPLE_RATE, format="WAV")
    return buf.getvalue()
