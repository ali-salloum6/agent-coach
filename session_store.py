"""Persist the in-memory chat session across process restarts.

One file per agent: ``data/<slug>.session.json``. Written after each successful
turn (and session-setting commands). Deleted on ``/new`` (and any other call
that clears the conversation). Loaded at startup so a restart does not wipe
chat history or the system prompt.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

import config

log = logging.getLogger(__name__)

_SESSION_KEYS = ("model", "history", "web_search", "tts")


def session_path(agent_slug: str) -> Path:
    return config.DATA_DIR / f"{agent_slug}.session.json"


def clear(agent_slug: str) -> None:
    """Delete the on-disk session if it exists."""
    path = session_path(agent_slug)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        log.exception("Failed to delete session file %s", path)


def save(agent_slug: str, session: dict[str, Any]) -> None:
    """Atomically write the current session to disk."""
    path = session_path(agent_slug)
    payload = {key: session.get(key) for key in _SESSION_KEYS}
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{agent_slug}.session.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def load(agent_slug: str) -> dict[str, Any] | None:
    """Return a validated session dict, or None if missing/invalid."""
    path = session_path(agent_slug)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log.exception("Corrupt session file %s; ignoring", path)
        return None
    if not isinstance(raw, dict):
        log.warning("Session file %s is not an object; ignoring", path)
        return None

    model = raw.get("model")
    history = raw.get("history")
    web_search = raw.get("web_search", False)
    tts = raw.get("tts", False)
    if not isinstance(model, str) or not model.strip():
        log.warning("Session file %s missing model; ignoring", path)
        return None
    if not isinstance(history, list) or not history:
        log.warning("Session file %s has empty history; ignoring", path)
        return None
    if not all(isinstance(msg, dict) and "role" in msg for msg in history):
        log.warning("Session file %s has invalid history entries; ignoring", path)
        return None
    if not isinstance(web_search, bool) or not isinstance(tts, bool):
        log.warning("Session file %s has invalid flags; ignoring", path)
        return None

    return {
        "model": model.strip(),
        "history": history,
        "web_search": web_search,
        "tts": tts,
    }
