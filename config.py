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

DEFAULT_MODEL = "z-ai/glm-5.2"
VISION_MODEL = "google/gemini-3.1-pro-preview"
EXTRACTION_MODEL = "google/gemini-3.1-flash-lite-preview"
CHEAP_MODEL = "google/gemini-3.1-flash-lite-preview"
# "structured" routes through memory_writer.propose_and_apply (the new path).
# "legacy" keeps the old free-form extractor for emergency rollback.
MEMORY_WRITER_MODE = (
    os.environ.get("MEMORY_WRITER_MODE", "structured").strip().lower() or "structured"
)

# On 429 rate limit, retry once with this model instead
RATE_LIMIT_FALLBACK: dict[str, str] = {
    "z-ai/glm-5.2": "google/gemini-3.1-pro-preview",
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
