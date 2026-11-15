"""Speech to text for voice messages, run locally with faster-whisper.

Voice notes are transcribed on the bot's own server, so the audio never
leaves it. The model loads once (at startup, in the background) and stays in
memory; transcriptions run one at a time in a worker thread so the bot keeps
answering other updates meanwhile.

Settings (env): VOICE_ENABLED (default on), WHISPER_MODEL (default "small";
"large-v3-turbo" is more accurate on hard audio and about three times as
slow), WHISPER_LANGUAGE (default "en"; "auto" detects it, but can take
accented English for another language and garble the text), WHISPER_THREADS
(default: all CPU cores), WHISPER_BEAM_SIZE (default 1, greedy; 5 is slower
and occasionally more accurate).
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from dataclasses import dataclass
from io import BytesIO

import config

log = logging.getLogger(__name__)

# Biases spelling towards how the coach writes numbers and units.
INITIAL_PROMPT = (
    "Gym coach chat about meals, calories, protein in grams, weight in kilos, "
    "tape in centimeters, sets and reps, for example 125 grams, 76.9 kilos, 60 kilos for 8, 8 and 6."
)


class NotAvailable(RuntimeError):
    """Transcription is switched off or faster-whisper is not installed."""


@dataclass
class Transcript:
    text: str
    language: str
    seconds: float


_model = None
_load_lock = threading.Lock()
_run_lock = threading.Lock()


def _get_model():
    global _model
    if not config.VOICE_ENABLED:
        raise NotAvailable("voice messages are switched off (VOICE_ENABLED)")
    with _load_lock:
        if _model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as e:
                raise NotAvailable("faster-whisper is not installed") from e
            threads = config.WHISPER_THREADS or os.cpu_count() or 1
            log.info("Loading Whisper model %s (%d threads)", config.WHISPER_MODEL, threads)
            _model = WhisperModel(config.WHISPER_MODEL, device="cpu", compute_type="int8", cpu_threads=threads)
    return _model


SAMPLE_RATE = 16000


def decode(audio: bytes):
    """Decode any audio file (e.g. OGG/Opus) to 16 kHz mono float32 samples.

    Done here rather than inside faster-whisper, whose decoder passes an
    option that PyAV 19 no longer accepts.
    """
    import av
    import numpy as np

    chunks = []
    with av.open(BytesIO(audio), mode="r") as container:
        if not container.streams.audio:
            raise ValueError("the file has no audio")
        resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
        for frame in container.decode(container.streams.audio[0]):
            chunks.extend(f.to_ndarray() for f in resampler.resample(frame))
        chunks.extend(f.to_ndarray() for f in resampler.resample(None))
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(chunks, axis=1).reshape(-1).astype(np.float32) / 32768.0


def transcribe_sync(audio: bytes) -> Transcript:
    """Transcribe one audio file (any format ffmpeg reads, e.g. OGG/Opus)."""
    model = _get_model()
    samples = decode(audio)
    with _run_lock:
        segments, info = model.transcribe(
            samples,
            language=config.WHISPER_LANGUAGE,
            beam_size=config.WHISPER_BEAM_SIZE,
            vad_filter=True,
            initial_prompt=INITIAL_PROMPT,
        )
        text = " ".join(segment.text.strip() for segment in segments).strip()
    return Transcript(text=text, language=info.language, seconds=info.duration)


async def transcribe(audio: bytes, *, timeout: float) -> Transcript:
    """Transcribe in a worker thread. Raises asyncio.TimeoutError after `timeout` seconds."""
    return await asyncio.wait_for(asyncio.to_thread(transcribe_sync, audio), timeout=timeout)


async def preload() -> None:
    """Load the model in the background at startup so the first voice note is quick."""
    if not config.VOICE_ENABLED:
        return
    try:
        await asyncio.to_thread(_get_model)
        log.info("Whisper model %s ready", config.WHISPER_MODEL)
    except NotAvailable as e:
        log.warning("Voice messages unavailable: %s", e)
    except Exception:
        log.exception("Loading the Whisper model failed; voice messages will retry on first use")
