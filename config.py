import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
# Optional HTTP(S) proxy (e.g. http://10.8.0.1:8888 via WireGuard + tinyproxy).
# Used for Telegram Bot API; also defaults OpenRouter egress unless overridden below.
TELEGRAM_HTTP_PROXY = os.environ.get("TELEGRAM_HTTP_PROXY", "").strip() or None
_openrouter_proxy = os.environ.get("OPENROUTER_HTTP_PROXY", "").strip()
OPENROUTER_HTTP_PROXY = _openrouter_proxy or TELEGRAM_HTTP_PROXY
OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]
ACTIVE_AGENT = os.environ.get("ACTIVE_AGENT", "gym_coach").strip() or "gym_coach"

# Optional: bot only responds to this user (username without @, or set ALLOWED_USER_ID for numeric id)
ALLOWED_USERNAME = os.environ.get("TELEGRAM_ALLOWED_USERNAME", "").strip() or None
_allowed_id = os.environ.get("TELEGRAM_ALLOWED_USER_ID", "").strip()
ALLOWED_USER_ID = int(_allowed_id) if _allowed_id else None

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_CREDITS_URL = "https://openrouter.ai/api/v1/credits"
OPENROUTER_KEY_INFO_URL = "https://openrouter.ai/api/v1/key"

# Cap per response (reasoning tokens count toward it). xhigh reasoning on
# GPT-6.1 Sol used ~6k reasoning tokens on a hard planning question, so leave
# room for that; this only stops a degenerate stream from running for minutes.
MAX_OUTPUT_TOKENS = int(os.environ.get("MAX_OUTPUT_TOKENS", "32000"))
# OpenRouter providers to never route to (Modal served garbage output on 2026-09-29).
OPENROUTER_IGNORE_PROVIDERS = ["modal"]

DEFAULT_MODEL = "openai/gpt-6.1-sol"
# Reasoning effort sent for OpenAI GPT-6 models (low|medium|high|xhigh|max).
OPENAI_REASONING_EFFORT = (
    os.environ.get("OPENAI_REASONING_EFFORT", "xhigh").strip().lower() or "xhigh"
)
# Optional OpenAI answer length (low|medium|high); unset keeps the provider default (medium).
OPENAI_VERBOSITY = os.environ.get("OPENAI_VERBOSITY", "").strip().lower() or None
# GPT-6.1 Sol accepts text, image and PDF input, so attachment turns stay on the chat model.
VISION_MODEL = DEFAULT_MODEL
EXTRACTION_MODEL = "google/gemini-3.1-flash-lite-preview"
CHEAP_MODEL = "google/gemini-3.1-flash-lite-preview"
# "structured" routes through memory_writer.propose_and_apply (the new path).
# "legacy" keeps the old free-form extractor for emergency rollback.
MEMORY_WRITER_MODE = (
    os.environ.get("MEMORY_WRITER_MODE", "structured").strip().lower() or "structured"
)
# Reasoning effort for the background memory step (memory v2), lower than the
# reply's so the next reply does not wait long for it. Only models that take
# an effort setting use it.
MEMORY_WRITER_EFFORT = os.environ.get("MEMORY_WRITER_EFFORT", "low").strip().lower() or "low"
# Voice messages: transcribed locally with faster-whisper (stt.py).
VOICE_ENABLED = os.environ.get("VOICE_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "small").strip() or "small"
# Spoken language, English by default. "auto" detects it (multilingual models
# only), but detection can take accented English for another language.
_whisper_language = os.environ.get("WHISPER_LANGUAGE", "en").strip().lower() or "en"
WHISPER_LANGUAGE = None if _whisper_language == "auto" else _whisper_language
WHISPER_THREADS = int(os.environ.get("WHISPER_THREADS", "0") or 0)
# 1 is greedy decoding: about twice as fast as 5 on a 4-core server, with the
# same transcript in tests; raise it if words come out wrong.
WHISPER_BEAM_SIZE = int(os.environ.get("WHISPER_BEAM_SIZE", "1") or 1)
MAX_VOICE_SECONDS = int(os.environ.get("MAX_VOICE_SECONDS", "600"))
MAX_VOICE_BYTES = 20 * 1024 * 1024  # Telegram bots can download at most 20 MB
VOICE_TIMEOUT_SECONDS = float(os.environ.get("VOICE_TIMEOUT_SECONDS", "300"))
# Which memory the bot uses. "1" is the single Markdown file per agent;
# "2" is logs, cards, story and archive (docs/memory-v2.md). Set per bot in
# its env file, so each agent can switch on its own.
MEMORY_VERSION = os.environ.get("MEMORY_VERSION", "1").strip() or "1"
# Weekly review (memory v2 only): local weekday and time it runs.
WEEKLY_REVIEW_DAY = os.environ.get("WEEKLY_REVIEW_DAY", "monday").strip().lower() or "monday"
WEEKLY_REVIEW_TIME = os.environ.get("WEEKLY_REVIEW_TIME", "12:00").strip() or "12:00"

# On a 429 rate limit, or a provider at capacity, retry once with this model.
# Every fallback must be allowed by the OpenRouter account's zero-data-retention
# setting; Gemini 3.8 Flash is not (checked 2026-11-13), so it is only a source.
RATE_LIMIT_FALLBACK: dict[str, str] = {
    "openai/gpt-6.1-sol": "moonshotai/kimi-k3.1-pro",
    "openai/gpt-6-luna": "moonshotai/kimi-k3.1-pro",
    "z-ai/glm-5.3-flash": "z-ai/glm-5",
    "z-ai/glm-5.2": "z-ai/glm-5",
    "google/gemini-3.8-flash": "z-ai/glm-5",
    "google/gemini-3.1-pro-preview": "z-ai/glm-5",
    "google/gemini-3-flash-preview": "z-ai/glm-5",
    "google/gemini-3.1-flash-lite-preview": "z-ai/glm-5",
}

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

# Local timezone for memory timestamps, message context, and writer prompts.
# IANA name, e.g. Europe/Moscow, Asia/Shanghai, UTC.
_memory_tz_name = (
    os.environ.get("MEMORY_TIMEZONE", "Europe/Moscow").strip() or "Europe/Moscow"
)
MEMORY_TIMEZONE = ZoneInfo(_memory_tz_name)
MEMORY_TIMEZONE_LABEL = _memory_tz_name.rsplit("/", 1)[-1].replace("_", " ")


def local_now() -> datetime:
    return datetime.now(MEMORY_TIMEZONE)


def format_local_time(dt: datetime | None = None, *, with_label: bool = True) -> str:
    """Format a datetime in MEMORY_TIMEZONE for user-facing strings."""
    when = (dt or local_now()).astimezone(MEMORY_TIMEZONE)
    base = when.strftime("%Y-%m-%d %H:%M")
    if with_label:
        return f"{base} {MEMORY_TIMEZONE_LABEL}"
    return base

# Vision / Telegram media (per user turn)
MAX_VISION_IMAGES = int(os.environ.get("MAX_VISION_IMAGES", "10"))
MAX_IMAGE_BYTES = int(os.environ.get("MAX_IMAGE_BYTES", str(5 * 1024 * 1024)))
# Telegram bots can download at most 20 MB per file.
MAX_PDF_BYTES = int(os.environ.get("MAX_PDF_BYTES", str(20 * 1024 * 1024)))
MAX_VISION_PAYLOAD_BYTES = int(
    os.environ.get("MAX_VISION_PAYLOAD_BYTES", str(20 * 1024 * 1024))
)
MEDIA_GROUP_DEBOUNCE_SEC = float(os.environ.get("MEDIA_GROUP_DEBOUNCE_SEC", "1.2"))

# Text-to-speech (/read) — Kokoro-82M, CPU-only torch on the VPS
TTS_VOICE = os.environ.get("TTS_VOICE", "bm_george").strip() or "bm_george"
_tts_enabled = os.environ.get("TTS_ENABLED", "true").strip().lower()
TTS_ENABLED = _tts_enabled not in ("0", "false", "no", "off")
_tts_auto = os.environ.get("TTS_AUTO_READ", "false").strip().lower()
TTS_AUTO_READ = _tts_auto in ("1", "true", "yes", "on")
