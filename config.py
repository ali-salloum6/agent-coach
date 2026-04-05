import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]
ACTIVE_AGENT = os.environ.get("ACTIVE_AGENT", "gym_coach").strip() or "gym_coach"

# Optional: bot only responds to this user (username without @, or set ALLOWED_USER_ID for numeric id)
ALLOWED_USERNAME = os.environ.get("TELEGRAM_ALLOWED_USERNAME", "").strip() or None
_allowed_id = os.environ.get("TELEGRAM_ALLOWED_USER_ID", "").strip()
ALLOWED_USER_ID = int(_allowed_id) if _allowed_id else None

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_CREDITS_URL = "https://openrouter.ai/api/v1/credits"
OPENROUTER_KEY_INFO_URL = "https://openrouter.ai/api/v1/key"

DEFAULT_MODEL = "google/gemini-3.1-pro-preview"
SUMMARIZATION_MODEL = DEFAULT_MODEL
EXTRACTION_MODEL = "google/gemini-3.1-flash-lite-preview"
CHEAP_MODEL = "google/gemini-3.1-flash-lite-preview"

# On 429 rate limit, retry once with this model instead
RATE_LIMIT_FALLBACK: dict[str, str] = {
    "google/gemini-3.1-pro-preview": "z-ai/glm-5",
    "google/gemini-3-flash-preview": "z-ai/glm-5",
    "google/gemini-3.1-flash-lite-preview": "z-ai/glm-5",
}

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

# Vision / Telegram media (per user turn)
MAX_VISION_IMAGES = int(os.environ.get("MAX_VISION_IMAGES", "10"))
MAX_IMAGE_BYTES = int(os.environ.get("MAX_IMAGE_BYTES", str(5 * 1024 * 1024)))
MAX_VISION_PAYLOAD_BYTES = int(
    os.environ.get("MAX_VISION_PAYLOAD_BYTES", str(20 * 1024 * 1024))
)
MEDIA_GROUP_DEBOUNCE_SEC = float(os.environ.get("MEDIA_GROUP_DEBOUNCE_SEC", "1.2"))
