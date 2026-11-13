"""Memory v2: what the coach sees, and the small state files around the cards.

The coach sees memory in two parts:

- Stable, built at /new: the profile, playbook, story and threads cards.
  These go into the system prompt.
- Live, attached to every message: numbers computed from the logs, the
  pivot rules checked against them, playbook changes waiting for the user's
  OK, short-lived notes, and food reference or archive entries that match
  the message. This block is added to the request only and never saved in
  the conversation history, so it is always current and appears once.

Saving is in `writer_v2.py`; the weekly review is in `review.py`. The
design is in docs/memory-v2.md.
"""

from __future__ import annotations

import json
import os
import re
from datetime import date as date_cls, datetime
from pathlib import Path
from typing import Any

import brief
import cards
import config
import ledger
import memory_template
import recall

STABLE_CARDS: tuple[str, ...] = (cards.PROFILE, cards.PLAYBOOK, cards.STORY, cards.THREADS)
MAX_ARCHIVE_HITS = 3
MAX_ARCHIVE_CHARS = 300


def enabled() -> bool:
    return config.MEMORY_VERSION == "2"


# ---------------------------------------------------------------------------
# Small JSON state files.


def _json_path(agent_slug: str, name: str) -> Path:
    return ledger.agent_dir(agent_slug) / "cards" / name


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def read_phase(agent_slug: str) -> dict[str, Any] | None:
    """The current phase: {"name": "build", "start": "YYYY-MM-DD"}."""
    return _read_json(_json_path(agent_slug, "phase.json"), None)


def write_phase(agent_slug: str, phase: dict[str, Any]) -> None:
    _write_json(_json_path(agent_slug, "phase.json"), phase)


def phase_week(phase: dict[str, Any] | None, today: str) -> int | None:
    """Week number within the phase, counting the start day as week 1."""
    if not phase or not phase.get("start"):
        return None
    days = (date_cls.fromisoformat(today) - date_cls.fromisoformat(phase["start"])).days
    return days // 7 + 1 if days >= 0 else None


def _pending_state(agent_slug: str) -> dict[str, Any]:
    return _read_json(_json_path(agent_slug, "pending.json"), {"next_id": 1, "items": []})


def read_pending(agent_slug: str) -> list[dict[str, Any]]:
    """Playbook changes the coach proposed and the user has not agreed to yet."""
    return _pending_state(agent_slug)["items"]


def add_pending(agent_slug: str, item: dict[str, Any]) -> str:
    """Store a proposal and return its id ("p1", "p2", ...). Ids are never reused."""
    state = _pending_state(agent_slug)
    pid = f"p{state['next_id']}"
    state["items"].append({"id": pid, **item})
    state["next_id"] += 1
    _write_json(_json_path(agent_slug, "pending.json"), state)
    return pid


def pop_pending(agent_slug: str, pid: str) -> dict[str, Any] | None:
    state = _pending_state(agent_slug)
    for item in state["items"]:
        if item["id"] == pid:
            state["items"].remove(item)
            _write_json(_json_path(agent_slug, "pending.json"), state)
            return item
    return None


def remember_chat(agent_slug: str, chat_id: int) -> None:
    """Store the user's chat id so the weekly review can message them."""
    path = _json_path(agent_slug, "chat.json")
    if _read_json(path, {}).get("chat_id") != chat_id:
        _write_json(path, {"chat_id": chat_id})


def chat_id(agent_slug: str) -> int | None:
    return _read_json(_json_path(agent_slug, "chat.json"), {}).get("chat_id")


def recent_path(agent_slug: str) -> Path:
    """Short-lived notes; the same file the v1 memory used."""
    return config.DATA_DIR / f"{agent_slug}.recent.md"


_NOTE_RE = re.compile(
    r"^- \[(?P<date>\d{4}-\d{2}-\d{2}) (?P<time>\d{2}:\d{2})[^|\]]*\|[^|\]]*\|\s*(?P<cat>[\w_]+)[^\]]*\]\s*(?P<text>.*)$"
)


def recent_notes(agent_slug: str, now: datetime | None = None) -> str:
    """Non-expired short-lived notes, one short line each."""
    path = recent_path(agent_slug)
    if not path.exists():
        return ""
    memory_template.prune_recent_file(path, now=now)
    visible = memory_template.visible_recent(path.read_text(encoding="utf-8"), now=now)
    lines = []
    for line in visible.splitlines():
        m = _NOTE_RE.match(line.strip())
        if m:
            lines.append(f"- {brief.fmt_day(m.group('date'))} {m.group('time')}: {m.group('text').strip()}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# What the coach sees.


def stable_memory(agent_slug: str) -> str:
    """The cards, for the system prompt at /new."""
    parts = [cards.read_card(agent_slug, name).strip() for name in STABLE_CARDS]
    return "\n\n".join(p for p in parts if p)


def pending_lines(pending: list[dict[str, Any]]) -> str:
    lines = []
    for p in pending:
        change = f'replace "{p["old"]}" with "{p["new"]}"' if p.get("old") and p.get("new") else (
            f'remove "{p["old"]}"' if p.get("old") else f'add "{p["new"]}"'
        )
        why = f" Why: {p['why']}" if p.get("why") else ""
        lines.append(f"- [{p['id']}] {p['section']}: {change} (proposed {p['date']}).{why}")
    return "\n".join(lines)


def live_context(agent_slug: str, message_text: str, *, now: datetime | None = None) -> str:
    """The block attached to the user's latest message."""
    now = now or config.local_now()
    today = ledger.today_iso(now)
    phase = read_phase(agent_slug)
    blocks = [
        f"[Live data, computed from the logs at {config.format_local_time(now)}. "
        "Use these numbers rather than older messages or your own sums.]"
    ]
    week = phase_week(phase, today)
    if phase and week:
        blocks.append(f"Phase: {phase['name']}, week {week} (since {brief.fmt_day(phase['start'], today=today)}).")
    blocks.append(brief.numbers(agent_slug, today=today, phase_start=(phase or {}).get("start")))
    tripwires = cards.tripwire_lines(agent_slug, today=today)
    if tripwires:
        blocks.append(tripwires)
    pending = read_pending(agent_slug)
    if pending:
        blocks.append("Playbook changes waiting for the user's OK:\n" + pending_lines(pending))
    notes = recent_notes(agent_slug, now)
    if notes:
        blocks.append("Short-lived notes:\n" + notes)
    foods = recall.match_foods(recall.load_foodbook(agent_slug), message_text)
    if foods:
        blocks.append(
            "Food reference entries matching this message:\n"
            + "\n".join(recall.food_line(f) for f in foods)
        )
    hits = recall.search_archive(agent_slug, message_text, limit=MAX_ARCHIVE_HITS, strict=True)
    if hits:
        shown = []
        for doc in hits:
            text = " ".join(doc.text.split())
            if len(text) > MAX_ARCHIVE_CHARS:
                text = text[:MAX_ARCHIVE_CHARS] + "…"
            shown.append(f"- {text}")
        blocks.append("Archive entries matching this message (older; may be outdated):\n" + "\n".join(shown))
    return "\n\n".join(blocks)


def with_live_context(messages: list[dict], live: str) -> list[dict]:
    """A copy of the messages with the live block added to the last user message."""
    if not messages or messages[-1].get("role") != "user":
        return list(messages)
    last = dict(messages[-1])
    content = last.get("content")
    if isinstance(content, list):
        last["content"] = [{"type": "text", "text": live}] + list(content)
    else:
        last["content"] = f"{live}\n\n---\n\n{content}"
    return list(messages[:-1]) + [last]
