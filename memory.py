from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

import config
import llm


def _memory_path(agent_slug: str) -> Path:
    return config.DATA_DIR / f"{agent_slug}.md"


def load(agent_slug: str) -> str:
    """Return the full memory content, or empty string if none yet."""
    path = _memory_path(agent_slug)
    if path.exists():
        return path.read_text()
    return ""


def append(agent_slug: str, text: str) -> None:
    """Append a timestamped entry to the memory file."""
    path = _memory_path(agent_slug)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = f"\n- [{timestamp}] {text}\n"
    with open(path, "a") as f:
        f.write(entry)


def forget(agent_slug: str) -> str | None:
    """Backup the memory file and start fresh. Returns backup path or None."""
    path = _memory_path(agent_slug)
    if not path.exists():
        return None
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = path.with_suffix(f".backup_{timestamp}.md")
    shutil.move(path, backup)
    return str(backup)


def _backup_memory(agent_slug: str) -> Path | None:
    """Copy memory file to a timestamped backup. Returns backup path or None."""
    path = _memory_path(agent_slug)
    if not path.exists():
        return None
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = path.with_suffix(f".backup_{timestamp}.md")
    shutil.copy2(path, backup)
    return backup


async def summarize(agent_slug: str) -> tuple[bool, str]:
    """Backup current memory, then replace it with a summarized version. Uses 3.1 flash lite.
    Returns (success, message)."""
    path = _memory_path(agent_slug)
    if not path.exists():
        return False, "Memory is empty; nothing to summarize."
    content = path.read_text()
    if not content.strip():
        return False, "Memory is empty; nothing to summarize."
    backup = _backup_memory(agent_slug)
    if not backup:
        return False, "Could not create backup."
    summary = await llm.summarize_memory(content)
    path.write_text(summary)
    return True, f"Memory summarized. Backup: {backup.name}"


async def extract_and_save(
    agent_slug: str,
    user_message: str,
    assistant_response: str,
) -> None:
    """Run memory extraction and append any new facts."""
    existing = load(agent_slug)
    new_facts = await llm.extract_memories(user_message, assistant_response, existing)
    if new_facts:
        path = _memory_path(agent_slug)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        entry = f"\n### Auto-extracted [{timestamp}]\n{new_facts}\n"
        with open(path, "a") as f:
            f.write(entry)
