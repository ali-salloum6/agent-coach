import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]

# Optional: bot only responds to this user (username without @, or set ALLOWED_USER_ID for numeric id)
ALLOWED_USERNAME = os.environ.get("TELEGRAM_ALLOWED_USERNAME", "").strip() or None
_allowed_id = os.environ.get("TELEGRAM_ALLOWED_USER_ID", "").strip()
ALLOWED_USER_ID = int(_allowed_id) if _allowed_id else None

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1/chat/completions"

DEFAULT_MODEL = "google/gemini-3.1-pro-preview"
EXTRACTION_MODEL = "google/gemini-3.1-flash-lite-preview"

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

ACTIVE_AGENT = "gym_coach"
