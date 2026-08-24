"""Memory facade used by bot.py.

This module is intentionally thin: it owns the legacy free-form append
path (kept for emergency rollback via `MEMORY_WRITER_MODE=legacy`) and
delegates everything else to the structured memory writer
(`memory_writer.py`). Prompt loading combines canonical long-term memory
with non-expired recent notes and prunes the recent file automatically.
"""

from __future__ import annotations

import logging
from pathlib import Path

import config
import llm
import memory_template
import memory_writer

log = logging.getLogger(__name__)


def _canonical_path(agent_slug: str) -> Path:
    return config.DATA_DIR / f"{agent_slug}.md"


def _recent_path(agent_slug: str) -> Path:
    return config.DATA_DIR / f"{agent_slug}.recent.md"


# ---------------------------------------------------------------------------
# Public API used by bot.py / agents.


def load(agent_slug: str) -> str:
    """Return canonical memory plus non-expired recent memory for prompts.

    The recent file is pruned in place as a side effect, so expired
    entries never appear in the system prompt.
    """
    return memory_writer.load_combined_memory(agent_slug)


def append(agent_slug: str, text: str) -> None:
    """Manual `/remember` entry appended to the canonical `Manual Notes` section."""
    memory_writer.ensure_files(agent_slug)
    canonical_path = _canonical_path(agent_slug)
    body = f"- [{config.format_local_time()}] {text.strip()}\n"
    markdown = canonical_path.read_text(encoding="utf-8")
    new_md = memory_template.append_under_heading(
        markdown,
        section="Manual Notes",
        subsection=None,
        body=body,
    )
    canonical_path.write_text(new_md, encoding="utf-8")


async def extract_and_save(
    agent_slug: str,
    user_message: str,
    assistant_response: str,
    *,
    chat_model: str,
) -> None:
    """Run after every successful exchange. Honors `MEMORY_WRITER_MODE`."""
    mode = config.MEMORY_WRITER_MODE
    if mode == "structured":
        result = await memory_writer.propose_and_apply(
            agent_slug,
            user_message,
            assistant_response,
            writer_model=chat_model,
        )
        if result.parse_error:
            log.warning(
                "memory_writer parse_error: %s (accepted=%d rejected=%d)",
                result.parse_error,
                result.accepted,
                result.rejected,
            )
        else:
            log.info(
                "memory_writer: accepted=%d rejected=%d pruned=%d",
                result.accepted,
                result.rejected,
                result.pruned_recent,
            )
        return

    if mode == "legacy":
        await _legacy_extract_and_save(agent_slug, user_message, assistant_response)
        return

    log.warning(
        "MEMORY_WRITER_MODE=%r is unknown; falling back to 'structured'.", mode
    )
    await memory_writer.propose_and_apply(
        agent_slug,
        user_message,
        assistant_response,
        writer_model=chat_model,
    )


def prune_recent(agent_slug: str) -> int:
    """Drop expired recent notes from disk. Idempotent. Returns count pruned."""
    return memory_writer.prune_recent(agent_slug)


# ---------------------------------------------------------------------------
# Legacy free-form append path (kept only for `MEMORY_WRITER_MODE=legacy`).


async def _legacy_extract_and_save(
    agent_slug: str,
    user_message: str,
    assistant_response: str,
) -> None:
    existing = _canonical_path(agent_slug).read_text(encoding="utf-8") if _canonical_path(agent_slug).exists() else ""
    new_facts = await llm.extract_memories(user_message, assistant_response, existing)
    if not new_facts:
        return
    path = _canonical_path(agent_slug)
    memory_writer.ensure_files(agent_slug)
    timestamp = config.format_local_time(with_label=False)
    entry = f"\n### Auto-extracted [{timestamp}]\n{new_facts}\n"
    with path.open("a", encoding="utf-8") as f:
        f.write(entry)
