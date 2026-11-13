"""Memory v2 writer: turns each exchange into checked changes to logs and cards.

After every reply a model call reads the exchange and proposes operations as
JSON. Code checks each one before applying it:

- its evidence must be an exact quote from the text it claims (the user's
  message or the coach's reply);
- every number it saves must be written in the exchange, so nothing is
  computed or invented by the model;
- facts about the user, playbook changes and pivot rules need the user's own
  words: the coach can only propose them;
- dates must be recent and not in the future.

Applying an operation writes to the logs (`ledger.py`), the cards
(`cards.py`), the food reference, the short-lived notes, the pending
proposals or the pivot rules. Every accepted and rejected operation goes to
an audit log. Nothing is ever deleted: card rewrites keep the previous
version, and closed threads move to the archive.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from dataclasses import dataclass, field
from datetime import date as date_cls, datetime, timedelta
from pathlib import Path
from typing import Any

import cards
import config
import ledger
import llm
import memory_ops
import memory_template
import memory_v2
import numcheck
import recall
from agents.registry import get_agent

log = logging.getLogger(__name__)

MAX_OPS = 15
BACKDATE_DAYS = 14
MAX_TEXT = 400
METRIC_UNITS: dict[str, frozenset[str]] = {
    "weight": frozenset({"kg"}),
    "belly": frozenset({"cm"}),
    "waist": frozenset({"cm"}),
    "hips": frozenset({"cm"}),
    "chest": frozenset({"cm"}),
    "shoulders": frozenset({"cm"}),
    "neck": frozenset({"cm"}),
    "arm": frozenset({"cm"}),
    "thigh": frozenset({"cm"}),
    "sleep": frozenset({"hours"}),
    "steps": frozenset({"steps"}),
    "body_fat": frozenset({"%"}),
}
EVENT_KINDS = frozenset({"milestone", "injury", "decision", "move", "health", "other"})
NOTE_DAYS = frozenset({2, 7, 14, 30})
TRIPWIRE_OPS = frozenset({">", ">=", "<", "<="})
LIFT_UNITS = frozenset({"kg", "lb"})

# Operations that change what the coach believes or follows: only the user's
# own words can make them.
USER_ONLY = frozenset(
    {"set_profile", "set_thread", "close_thread", "remove_food", "apply_change",
     "change_playbook", "set_tripwire", "remove_tripwire"}
)
# A quote this short ("I", "ok") proves nothing unless it is the whole message.
MIN_EVIDENCE_CHARS = 12
KNOWN_OPS = frozenset(
    {"log_food", "correct_food", "remove_food", "day_total", "log_metric", "log_lift",
     "set_profile", "set_thread", "close_thread", "event", "food_reference", "note",
     "propose_change", "apply_change", "change_playbook", "set_tripwire", "remove_tripwire"}
)


@dataclass
class Exchange:
    user: str
    assistant: str
    has_attachment: bool = False

    @property
    def both(self) -> str:
        return f"{self.user}\n{self.assistant}"

    def measured_text(self) -> str:
        """Where a reading or a set may be stated.

        Normally the user's message. When the user sent a photo (a scale, a
        workout app), the numbers are only in the coach's description of it.
        """
        return self.both if self.has_attachment else self.user


@dataclass
class Result:
    applied: list[dict] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)
    error: str | None = None


class Rejected(ValueError):
    """An operation failed a check; the message says which."""


# ---------------------------------------------------------------------------
# Field checks.


def _text(op: dict, key: str, *, required: bool = True, limit: int = MAX_TEXT) -> str:
    value = op.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise Rejected(f"{key} is required")
        return ""
    if not isinstance(value, str):
        raise Rejected(f"{key} must be text")
    value = " ".join(value.split())
    if len(value) > limit:
        raise Rejected(f"{key} is longer than {limit} characters")
    return value


def _number(op: dict, key: str, *, required: bool = True) -> float | None:
    value = op.get(key)
    if value is None:
        if required:
            raise Rejected(f"{key} is required")
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise Rejected(f"{key} must be a number")
    if not math.isfinite(value):
        raise Rejected(f"{key} must be a finite number")
    if value < 0:
        raise Rejected(f"{key} cannot be negative")
    return float(value)


def _date(op: dict, today: str) -> str:
    value = op.get("date") or today
    try:
        day = date_cls.fromisoformat(value)
    except (TypeError, ValueError):
        raise Rejected(f"date must be YYYY-MM-DD, got {value!r}") from None
    now = date_cls.fromisoformat(today)
    if day > now:
        raise Rejected("date is in the future")
    if day < now - timedelta(days=BACKDATE_DAYS):
        raise Rejected(f"date is more than {BACKDATE_DAYS} days ago")
    return day.isoformat()


def _require_numbers(values: list[float | None], text: str) -> None:
    absent = numcheck.missing(values, text)
    if absent:
        shown = ", ".join(f"{v:g}" for v in absent)
        raise Rejected(f"numbers not stated in the exchange: {shown}")


def _check_evidence(op: dict, exchange: Exchange) -> str:
    source = op.get("source")
    if source not in ("user", "assistant"):
        raise Rejected("source must be user or assistant")
    if op["op"] in USER_ONLY and source != "user":
        raise Rejected(f"{op['op']} needs the user's own words")
    evidence = op.get("evidence")
    if not isinstance(evidence, str) or not evidence.strip():
        raise Rejected("evidence is required")
    text = exchange.user if source == "user" else exchange.assistant
    if _loose(evidence) not in _loose(text):
        raise Rejected("evidence is not a quote from the claimed text")
    if len(_norm(evidence)) < MIN_EVIDENCE_CHARS and _norm(evidence) != _norm(text):
        raise Rejected(f"evidence must be at least {MIN_EVIDENCE_CHARS} characters or the whole message")
    return source


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


_QUOTES = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2013": "-", "\u2014": "-"})


def _loose(text: str) -> str:
    """Text for quote matching: no Markdown symbols, plain quotes and dashes, single spaces.

    Models often start a quote inside bold text, so a quote may carry half
    of a "**" pair; dropping every Markdown symbol on both sides fixes that.
    """
    text = text.translate(_QUOTES)
    text = re.sub(r"[*_`#>]+", "", text)
    return " ".join(text.lower().split())


# ---------------------------------------------------------------------------
# Card editing. Pure functions over Markdown text.


def _section_bounds(lines: list[str], section: str, level: str = "## ") -> tuple[int, int] | None:
    """(heading index, end index) of a section, matched case-insensitively."""
    wanted = _norm(section)
    for i, line in enumerate(lines):
        if line.startswith(level) and _norm(line[len(level):]) == wanted:
            end = len(lines)
            for j in range(i + 1, len(lines)):
                if lines[j].startswith("## ") or (level == "### " and lines[j].startswith("### ")):
                    end = j
                    break
            return i, end
    return None


def _join(lines: list[str]) -> str:
    return "\n".join(lines).rstrip() + "\n"


def set_profile_line(card: str, section: str, key: str, value: str) -> str:
    """Replace the "- **Key**: value" bullet in a section, or add it."""
    lines = card.splitlines()
    bounds = _section_bounds(lines, section)
    if bounds is None:
        raise Rejected(f"profile has no section {section!r}")
    start, end = bounds
    bullet = f"- **{key}**: {value}"
    pattern = re.compile(rf"^- \*\*{re.escape(key)}\*\*:", re.I)
    for i in range(start + 1, end):
        if pattern.match(lines[i]):
            lines[i] = bullet
            return _join(lines)
    last = max((i for i in range(start + 1, end) if lines[i].startswith("- ")), default=start)
    lines.insert(last + 1, bullet)
    return _join(lines)


def set_thread_lines(card: str, topic: str, new_lines: list[str]) -> str:
    """Replace a topic's lines, or add the topic at the end."""
    lines = card.splitlines()
    body = [f"- {line.removeprefix('- ').strip()}" for line in new_lines]
    bounds = _section_bounds(lines, topic)
    if bounds is None:
        return _join(lines + ["", f"## {topic}"] + body)
    start, end = bounds
    return _join(lines[: start + 1] + body + ([""] if end < len(lines) else []) + lines[end:])


def remove_thread(card: str, topic: str) -> tuple[str, str]:
    """Remove a topic. Returns the new card and the removed block."""
    lines = card.splitlines()
    bounds = _section_bounds(lines, topic)
    if bounds is None:
        raise Rejected(f"no open thread {topic!r}")
    start, end = bounds
    removed = "\n".join(lines[start:end]).strip()
    return _join(lines[:start] + lines[end:]), removed


def _bullet_hits(lines: list[str], section: str | None, old: str) -> list[int]:
    """Indexes of bullet lines containing `old`, in `section` when it exists.

    Headings and prose never match, so a change cannot remove a section.
    """
    wanted = _norm(old.removeprefix("- "))
    bounds = _section_bounds(lines, section) if section else None
    scope = range(bounds[0] + 1, bounds[1]) if bounds else range(len(lines))
    hits = [i for i in scope if lines[i].startswith("- ") and wanted in _norm(lines[i])]
    if not hits and bounds:
        hits = [i for i, line in enumerate(lines) if line.startswith("- ") and wanted in _norm(line)]
    return hits


def edit_playbook(card: str, section: str, old: str | None, new: str | None) -> str:
    """Replace, remove or add one playbook bullet.

    `old` must match exactly one bullet (as a substring, ignoring case and
    spacing), looked up in `section` first. With `old` empty, `new` is added
    at the end of `section`.
    """
    lines = card.splitlines()
    new_line = f"- {new.removeprefix('- ').strip()}" if new else None
    if old:
        hits = _bullet_hits(lines, section, old)
        if len(hits) != 1:
            raise Rejected(f"old text matches {len(hits)} playbook lines; it must match exactly one")
        if new_line:
            lines[hits[0]] = new_line
        else:
            del lines[hits[0]]
        return _join(lines)
    if not new_line:
        raise Rejected("a playbook change needs old text, new text, or both")
    bounds = _section_bounds(lines, section)
    if bounds is None:
        raise Rejected(f"playbook has no section {section!r}")
    start, end = bounds
    last = max((i for i in range(start + 1, end) if lines[i].startswith("- ")), default=start)
    lines.insert(last + 1, new_line)
    return _join(lines)


def matching_line(card: str, old: str, section: str | None = None) -> str:
    """The one bullet that `old` refers to."""
    lines = card.splitlines()
    hits = _bullet_hits(lines, section, old)
    if len(hits) != 1:
        raise Rejected(f"old text matches {len(hits)} playbook lines; it must match exactly one")
    return lines[hits[0]]


def profile_line(card: str, section: str, key: str) -> str:
    """The current "- **Key**: value" line, or "" when the key is new."""
    lines = card.splitlines()
    bounds = _section_bounds(lines, section)
    if bounds is None:
        return ""
    pattern = re.compile(rf"^- \*\*{re.escape(key)}\*\*:", re.I)
    return next((lines[i] for i in range(bounds[0] + 1, bounds[1]) if pattern.match(lines[i])), "")


def headings(card: str) -> list[str]:
    return [line[3:].strip() for line in card.splitlines() if line.startswith("## ")]


# ---------------------------------------------------------------------------
# Applying operations.


def _food_reference(slug: str, entry: dict) -> None:
    rows = recall.load_foodbook(slug)
    names = {_norm(entry["name"]), *(_norm(a) for a in entry["aliases"])}
    kept = []
    for row in rows:
        row_names = {_norm(row["name"]), *(_norm(a) for a in row.get("aliases", []))}
        if row_names & names:
            entry["aliases"] = sorted({*entry["aliases"], *row.get("aliases", [])} - {entry["name"]})
            _append_line(ledger.agent_dir(slug) / "archive" / "foodbook_replaced.jsonl", json.dumps(row, ensure_ascii=False))
            continue
        kept.append(row)
    kept.append(entry)
    _atomic_write(
        recall.foodbook_path(slug),
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in sorted(kept, key=lambda r: r["name"].lower())),
    )


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _append_line(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(line.rstrip("\n") + "\n")


def _archive_closed_thread(slug: str, block: str, today: str) -> None:
    path = ledger.agent_dir(slug) / "archive" / "closed_threads.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(f"\n<!-- closed {today} -->\n{block}\n")


def _tripwire(op: dict, exchange: Exchange, pending_text: str) -> dict:
    wire = {
        "id": re.sub(r"[^a-z0-9-]+", "-", _text(op, "id", limit=40).lower()).strip("-"),
        "name": _text(op, "name", limit=80),
        "metric": _text(op, "metric", limit=20).lower(),
        "kind": _text(op, "kind", limit=10).lower(),
        "op": _text(op, "op_symbol", required=False, limit=2) or str(op.get("compare", "")),
        "threshold": _number(op, "threshold"),
        "unit": _text(op, "unit", limit=20),
        "then": _text(op, "then", limit=200),
    }
    if wire["metric"] not in METRIC_UNITS:
        raise Rejected(f"unknown metric {wire['metric']!r}")
    if wire["kind"] not in ("rate", "level"):
        raise Rejected("kind must be rate or level")
    if wire["op"] not in TRIPWIRE_OPS:
        raise Rejected("compare must be one of > >= < <=")
    if wire["kind"] == "rate":
        weeks = op.get("window_weeks")
        if not isinstance(weeks, int) or not 1 <= weeks <= 12:
            raise Rejected("window_weeks must be 1 to 12")
        wire["window_weeks"] = weeks
    else:
        count = op.get("consecutive")
        if not isinstance(count, int) or not 1 <= count <= 8:
            raise Rejected("consecutive must be 1 to 8")
        wire["consecutive"] = count
    _require_numbers([wire["threshold"]], f"{exchange.user}\n{pending_text}")
    return wire


def apply_op(slug: str, op: dict, exchange: Exchange, *, now: datetime) -> str:
    """Check and apply one operation. Returns a short description.

    Raises `Rejected` with the reason when a check fails; nothing is written
    in that case.
    """
    name = op.get("op")
    if name not in KNOWN_OPS:
        raise Rejected(f"unknown op {name!r}")
    source = _check_evidence(op, exchange)
    today = ledger.today_iso(now)
    stamp = f"chat:{now.astimezone(config.MEMORY_TIMEZONE).isoformat(timespec='minutes')}"

    if name == "log_food":
        kcal, protein = _number(op, "kcal", required=False), _number(op, "protein", required=False)
        _require_numbers([kcal, protein], exchange.both)
        time = _text(op, "time", required=False, limit=5)
        if time and not re.fullmatch(r"\d{2}:\d{2}", time):
            raise Rejected("time must be HH:MM")
        row = ledger.add_food(
            slug, date=_date(op, today), time=time, name=_text(op, "name", limit=120),
            qty=_text(op, "qty", required=False, limit=80), kcal=kcal, protein=protein,
            note=_text(op, "note", required=False, limit=200), src=stamp,
        )
        return f"food {row['id']}: {row['name']}"

    if name == "correct_food":
        row_id = _text(op, "id", limit=20)
        changes = {k: op[k] for k in ("name", "qty", "time", "note") if isinstance(op.get(k), str) and op[k].strip()}
        for key in ("kcal", "protein"):
            if op.get(key) is not None:
                changes[key] = _number(op, key)
        if isinstance(op.get("date"), str) and op["date"].strip():
            changes["date"] = _date(op, today)
        if not changes:
            raise Rejected("correct_food changes nothing")
        _require_numbers([changes.get("kcal"), changes.get("protein")], exchange.both)
        if ledger.update_food(slug, row_id, **changes) is None:
            raise Rejected(f"no food row {row_id!r}")
        return f"food {row_id} corrected"

    if name == "remove_food":
        row_id = _text(op, "id", limit=20)
        if not ledger.remove_food(slug, row_id):
            raise Rejected(f"no food row {row_id!r}")
        return f"food {row_id} removed"

    if name == "day_total":
        kcal, protein = _number(op, "kcal", required=False), _number(op, "protein", required=False)
        if kcal is None and protein is None:
            raise Rejected("day_total needs kcal or protein")
        _require_numbers([kcal, protein], exchange.both)
        day = _date(op, today)
        ledger.set_day_total(slug, date=day, kcal=kcal, protein=protein,
                             final=bool(op.get("final", True)), src=stamp)
        return f"day total {day}"

    if name == "log_metric":
        metric = _text(op, "metric", limit=20).lower()
        unit = _text(op, "unit", limit=10).lower()
        if metric not in METRIC_UNITS:
            raise Rejected(f"unknown metric {metric!r}")
        if unit not in METRIC_UNITS[metric]:
            raise Rejected(f"{metric} must be in {', '.join(sorted(METRIC_UNITS[metric]))}")
        value = _number(op, "value")
        _require_numbers([value], exchange.measured_text())
        row, replaced = ledger.add_metric(
            slug, date=_date(op, today), metric=metric, value=value, unit=unit,
            cond=_text(op, "cond", required=False, limit=80),
            note=_text(op, "note", required=False, limit=200), src=stamp,
        )
        return f"{metric} {value:g} {unit} on {row['date']}" + (" (replaced)" if replaced else "")

    if name == "log_lift":
        load = _number(op, "load", required=False)
        unit = _text(op, "unit", required=False, limit=5).lower() or None
        if load is not None and unit not in LIFT_UNITS:
            raise Rejected("unit must be kg or lb")
        reps = op.get("reps") or []
        if not isinstance(reps, list) or not all(isinstance(r, int) and 0 < r < 200 for r in reps):
            raise Rejected("reps must be a list of whole numbers")
        _require_numbers([load, *reps], exchange.measured_text())
        row, replaced = ledger.add_lift(
            slug, date=_date(op, today), exercise=_text(op, "exercise", limit=60),
            load=load, unit=unit, reps=reps, note=_text(op, "note", required=False, limit=200), src=stamp,
        )
        return f"lift {row['exercise']} on {row['date']}" + (" (replaced)" if replaced else "")

    if name == "event":
        kind = _text(op, "kind", limit=20).lower()
        if kind not in EVENT_KINDS:
            raise Rejected(f"kind must be one of {', '.join(sorted(EVENT_KINDS))}")
        row, new = ledger.add_event(slug, date=_date(op, today), kind=kind, text=_text(op, "text", limit=300), src=stamp)
        return f"event {row['date']}" + ("" if new else " (already recorded)")

    if name == "set_profile":
        section, key = _text(op, "section", limit=60), _text(op, "key", limit=40)
        value = _text(op, "value", limit=300)
        profile = cards.read_card(slug, cards.PROFILE)
        _require_numbers(numbers_in(value), f"{exchange.user}\n{profile_line(profile, section, key)}")
        _write_card(slug, cards.PROFILE, set_profile_line(profile, section, key, value), what=f"{key}: {value}")
        return f"profile {key}"

    if name == "set_thread":
        topic = _text(op, "topic", limit=40)
        new_lines = op.get("lines")
        if not isinstance(new_lines, list) or not 1 <= len(new_lines) <= 5:
            raise Rejected("lines must be a list of 1 to 5 lines")
        new_lines = [" ".join(str(line).split())[:250] for line in new_lines]
        _write_card(slug, cards.THREADS, set_thread_lines(cards.read_card(slug, cards.THREADS), topic, new_lines),
                    what=f"{topic}: {' '.join(new_lines)}")
        return f"thread {topic}"

    if name == "close_thread":
        topic = _text(op, "topic", limit=40)
        card, removed = remove_thread(cards.read_card(slug, cards.THREADS), topic)
        _write_card(slug, cards.THREADS, card)
        _archive_closed_thread(slug, removed, today)
        return f"thread {topic} closed"

    if name == "food_reference":
        kcal, protein = _number(op, "kcal", required=False), _number(op, "protein", required=False)
        if kcal is None and protein is None:
            raise Rejected("food_reference needs kcal or protein")
        _require_numbers([kcal, protein], exchange.both)
        aliases = [a for a in op.get("aliases") or [] if isinstance(a, str) and a.strip()][:5]
        entry = {
            "name": _text(op, "name", limit=120), "aliases": aliases,
            "serving": _text(op, "serving", required=False, limit=80),
            "kcal": kcal, "protein_g": protein, "era": "any", "updated": today,
            "note": _text(op, "note", required=False, limit=200), "src": stamp,
        }
        _food_reference(slug, entry)
        return f"food reference {entry['name']}"

    if name == "note":
        days = op.get("days", 2)
        if days not in NOTE_DAYS:
            raise Rejected(f"days must be one of {sorted(NOTE_DAYS)}")
        category = _text(op, "category", required=False, limit=20).lower() or "other"
        entry = memory_template.build_recent_entry(
            summary=_text(op, "text", limit=280), category=category, source=source, ttl_days=days, now=now,
        )
        memory_template.append_recent_entry(memory_v2.recent_path(slug), entry)
        return "note"

    if name == "propose_change":
        playbook = cards.read_card(slug, cards.PLAYBOOK)
        section = _text(op, "section", limit=60)
        old, new = _text(op, "old", required=False), _text(op, "new", required=False)
        edit_playbook(playbook, section, old or None, new or None)  # must apply cleanly now
        _check_new_numbers(new, old and matching_line(playbook, old, section), exchange.both)
        pid = memory_v2.add_pending(slug, {
            "date": today, "section": section, "old": old or None, "new": new or None,
            "why": _text(op, "why", required=False, limit=200),
        })
        return f"proposal {pid}"

    if name == "apply_change":
        pid = _text(op, "proposal", limit=10)
        item = next((p for p in memory_v2.read_pending(slug) if p["id"] == pid), None)
        if item is None:
            raise Rejected(f"no waiting proposal {pid!r}")
        card = edit_playbook(cards.read_card(slug, cards.PLAYBOOK), item["section"], item["old"], item["new"])
        _write_card(slug, cards.PLAYBOOK, card, what=item["new"] or f"remove {item['old']}")
        memory_v2.pop_pending(slug, pid)
        return f"playbook: applied {pid}"

    if name == "change_playbook":
        playbook = cards.read_card(slug, cards.PLAYBOOK)
        section = _text(op, "section", limit=60)
        old, new = _text(op, "old", required=False), _text(op, "new", required=False)
        card = edit_playbook(playbook, section, old or None, new or None)
        _check_new_numbers(new, old and matching_line(playbook, old, section), exchange.user)
        _write_card(slug, cards.PLAYBOOK, card, what=new or f"remove {old}")
        return "playbook changed"

    if name == "set_tripwire":
        pending_text = memory_v2.pending_lines(memory_v2.read_pending(slug))
        wire = _tripwire(op, exchange, pending_text)
        wire["set"], wire["src"] = today, stamp
        wires = [w for w in cards.read_tripwires(slug) if w["id"] != wire["id"]] + [wire]
        _write_tripwires(slug, wires)
        return f"tripwire {wire['id']}"

    if name == "remove_tripwire":
        wire_id = _text(op, "id", limit=40)
        wires = cards.read_tripwires(slug)
        kept = [w for w in wires if w["id"] != wire_id]
        if len(kept) == len(wires):
            raise Rejected(f"no tripwire {wire_id!r}")
        _write_tripwires(slug, kept)
        return f"tripwire {wire_id} removed"

    raise Rejected(f"unhandled op {name!r}")


_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
_DATES = re.compile(
    rf"\b\d{{4}}-\d{{2}}-\d{{2}}\b|\b\d{{1,2}}\s+{_MONTH}|\b{_MONTH}\s+\d{{1,2}}\b|\b\d{{1,2}}:\d{{2}}\b|\b20\d\d\b",
    re.I,
)


def numbers_in(text: str) -> list[float]:
    """Numbers written in a line of text, ignoring dates and times."""
    cleaned = _DATES.sub(" ", text)
    return [float(m.replace(",", "")) for m in re.findall(r"(?<![\w.])\d[\d,]*(?:\.\d+)?", cleaned)]


def _check_new_numbers(new: str | None, old_line: str | None, text: str) -> None:
    """Numbers in a new playbook line must come from `text` or the line it replaces."""
    if new:
        _require_numbers(numbers_in(new), f"{text}\n{old_line or ''}")


def _write_card(slug: str, name: str, text: str, *, what: str = "") -> None:
    try:
        cards.write_card(slug, name, text)
    except cards.CardTooLong as e:
        # Never silent: the coach sees this note in the live block and can
        # tell the user the card needs condensing.
        note = f"Memory: the {name} card is full, so this was not saved: {what}"[:280]
        entry = memory_template.build_recent_entry(
            summary=note, category="logistics", source="assistant", ttl_days=7, now=config.local_now(),
        )
        memory_template.append_recent_entry(memory_v2.recent_path(slug), entry)
        raise Rejected(f"{e}; condense the card first") from None


def _write_tripwires(slug: str, wires: list[dict]) -> None:
    _atomic_write(cards.tripwires_path(slug), json.dumps(wires, ensure_ascii=False, indent=2) + "\n")


# ---------------------------------------------------------------------------
# The writer prompt.


def _food_lines(slug: str, today: str) -> str:
    yesterday = (date_cls.fromisoformat(today) - timedelta(days=1)).isoformat()
    rows = [r for r in ledger.read_log(slug, ledger.FOOD) if r["date"] in (today, yesterday)]
    lines = []
    for r in rows:
        if r["kind"] == "item":
            qty = f" ({r['qty']})" if r.get("qty") else ""
            lines.append(f"- [{r['id']}] {r['date']} {r.get('time') or ''} {r['name']}{qty}: {r.get('kcal')} kcal, {r.get('protein')} g protein")
        else:
            lines.append(f"- {r['date']} reported day total: {r.get('kcal')} kcal, {r.get('protein')} g protein")
    return "\n".join(lines) or "(nothing logged today or yesterday)"


def _lift_lines(slug: str, today: str) -> str:
    yesterday = (date_cls.fromisoformat(today) - timedelta(days=1)).isoformat()
    rows = [r for r in ledger.read_log(slug, ledger.LIFTS) if r["date"] in (today, yesterday)]
    lines = [
        f"- {r['date']} {r['exercise']} {r.get('load') if r.get('load') is not None else ''} {r.get('unit') or ''}: "
        f"{', '.join(str(x) for x in r.get('reps') or [])}"
        for r in rows
    ]
    return "\n".join(lines) or "(nothing logged today or yesterday)"


def build_prompt(slug: str, exchange: Exchange, *, now: datetime) -> str:
    today = ledger.today_iso(now)
    profile = cards.read_card(slug, cards.PROFILE)
    playbook = cards.read_card(slug, cards.PLAYBOOK)
    pending = memory_v2.pending_lines(memory_v2.read_pending(slug)) or "(none)"
    wires = json.dumps(cards.read_tripwires(slug), ensure_ascii=False) if cards.read_tripwires(slug) else "(none)"
    exercises = sorted({r["exercise"] for r in ledger.read_log(slug, ledger.LIFTS)}) or ["(none yet)"]
    foods = recall.match_foods(recall.load_foodbook(slug), exchange.both, limit=8)
    food_ref = "\n".join(recall.food_line(f) for f in foods) or "(none)"
    context = get_agent(slug).memory_writer_context
    local = now.astimezone(config.MEMORY_TIMEZONE)
    return f"""You save memory for {context}. Read one exchange between the user and the coach and return JSON only, in the form {{"ops": [...]}}. An empty list means nothing to save.

Current time: {local.strftime('%Y-%m-%d %H:%M')} ({config.MEMORY_TIMEZONE_LABEL}), {local.strftime('%A')}.

Rules for every op:
- "source" is "user" or "assistant": the text "evidence" is quoted from.
- "evidence" is an exact quote from that text supporting the op: a full phrase of at least 12 characters, or the whole message when it is shorter.
- Every number you save must be written in the exchange exactly as you give it. Never add up or work out new numbers. For a range, use the upper bound for calories and the lower bound for protein.
- "date" is the day the thing happened, YYYY-MM-DD: today unless the text says otherwise.
- Save only what happened or what the user stated. Plans, suggestions and examples are not food eaten, readings taken or sets done.
- Return at most {MAX_OPS} ops.

Operations:
Food the user ate:
  {{"op": "log_food", "date", "time": "HH:MM" or null, "name", "qty", "kcal", "protein", "source", "evidence"}}: one op per item, with the calories and protein the coach gave for that item; use null for a value the coach did not give as a number. Never log an item already in the food log below.
  {{"op": "correct_food", "id", plus any of "name", "qty", "kcal", "protein", "source", "evidence"}}: fix a logged item; ids are in the food log below.
  {{"op": "remove_food", "id", "source": "user", "evidence"}}: the user says an item was not eaten or was logged twice.
  {{"op": "day_total", "date", "kcal", "protein", "final": true or false, "source", "evidence"}}: only when the user gives a whole day's total and that day has no items.
Body readings the user took:
  {{"op": "log_metric", "date", "metric", "value", "unit", "cond", "source", "evidence"}}: metric is one of {', '.join(METRIC_UNITS)}. "belly" is the tape at the navel, "waist" the narrowest point. Units: kg for weight, cm for tape, hours for sleep, steps, % for body_fat. "cond" is "" for the standard protocol (weight: home scale, morning, fasted; tape: cold, fasted); otherwise say what differs, for example "gym scale" or "after breakfast".
Sets the user did:
  {{"op": "log_lift", "date", "exercise", "load", "unit": "kg" or "lb", "reps": [reps of each set], "note", "source", "evidence"}}: one op per exercise and load. The op replaces what is logged for that exercise, load and day, so when the user adds sets to an exercise already logged below, send all the sets of that day. Use these exercise names when they fit: {', '.join(exercises)}.
Facts and topics:
  {{"op": "set_profile", "section", "key", "value", "source": "user", "evidence"}}: a durable fact about the user is new or changed. Section is one of: {', '.join(headings(profile)) or '(no profile yet)'}. Reuse an existing key when it fits; the value replaces the old one, so keep what is still true.
  {{"op": "set_thread", "topic", "lines": [1 to 5 short lines], "source": "user", "evidence"}}: the user says what changed in an open side topic (skin, bloodwork, an injury, a habit being changed). The lines replace the topic's old lines, so keep the old lines that are still true.
  {{"op": "close_thread", "topic", "source": "user", "evidence"}}: the topic is resolved.
  {{"op": "event", "date", "kind": "milestone" | "injury" | "decision" | "move" | "health" | "other", "text", "source", "evidence"}}: something that belongs in the story of this phase.
  {{"op": "food_reference", "name", "aliases": [...], "serving", "kcal", "protein", "note", "source", "evidence"}}: calories and protein for a specific food, product or dish likely to come up again (a label, a menu, a corrected estimate).
  {{"op": "note", "text", "days": 2 | 7 | 14 | 30, "category", "source", "evidence"}}: useful for the next few days only, such as advice not yet taken or a temporary situation.
The playbook (targets, weekly protocol, pivot rules, checkpoints):
  {{"op": "propose_change", "section", "old": an existing line or null, "new": the new line or null, "why", "source": "assistant", "evidence"}}: the coach suggested a playbook change the user has not agreed to.
  {{"op": "apply_change", "proposal": "p1", "source": "user", "evidence"}}: the user agreed to a waiting proposal.
  {{"op": "change_playbook", "section", "old": an existing line or null, "new": the new line or null, "source": "user", "evidence"}}: the user decided a change themselves.
  {{"op": "set_tripwire", "id", "name", "metric", "kind": "rate" | "level", "op_symbol": ">" | ">=" | "<" | "<=", "threshold", "unit", "window_weeks" (rate) or "consecutive" (level), "then", "source": "user", "evidence"}}: when the user agrees to a new or changed pivot rule, send this too so code can check it.
  {{"op": "remove_tripwire", "id", "source": "user", "evidence"}}

=== Food log, today and yesterday ===
{_food_lines(slug, today)}

=== Lifts logged today and yesterday ===
{_lift_lines(slug, today)}

=== Profile ===
{profile.strip() or '(empty)'}

=== Threads ===
{cards.read_card(slug, cards.THREADS).strip() or '(empty)'}

=== Playbook ===
{playbook.strip() or '(empty)'}

=== Waiting proposals ===
{pending}

=== Pivot rules checked by code ===
{wires}

=== Food reference entries matching this exchange ===
{food_ref}

USER MESSAGE:
{exchange.user}

COACH REPLY:
{exchange.assistant}
"""


# ---------------------------------------------------------------------------
# Entry point.


def _audit(slug: str, payload: dict) -> None:
    path = ledger.agent_dir(slug) / "memory_ops.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def apply_payload(slug: str, payload_text: str, exchange: Exchange, *, now: datetime) -> Result:
    """Parse the writer's JSON and apply each operation that passes its checks."""
    result = Result()
    try:
        data = memory_ops.parse_llm_payload(payload_text)
    except memory_ops.MemoryOpsError as e:
        result.error = str(e)
        return result
    ops = data.get("ops")
    if not isinstance(ops, list):
        result.error = "payload has no list 'ops'"
        return result
    for op in ops[:MAX_OPS]:
        if not isinstance(op, dict):
            result.rejected.append({"op": op, "reason": "op must be an object"})
            continue
        try:
            done = apply_op(slug, op, exchange, now=now)
            result.applied.append({"op": op.get("op"), "result": done})
        except Rejected as e:
            result.rejected.append({"op": op.get("op"), "reason": str(e), "raw": op})
        except Exception as e:  # noqa: BLE001 - one bad op must not stop the rest
            log.exception("writer_v2: applying %s failed", op.get("op"))
            result.rejected.append({"op": op.get("op"), "reason": f"error: {e}", "raw": op})
    for op in ops[MAX_OPS:]:
        result.rejected.append({"op": op.get("op") if isinstance(op, dict) else op, "reason": f"over {MAX_OPS} ops"})
    return result


async def propose_and_apply(
    slug: str,
    user_message: str,
    assistant_response: str,
    *,
    writer_model: str,
    has_attachment: bool = False,
    now: datetime | None = None,
) -> Result:
    now = now or config.local_now()
    exchange = Exchange(user=user_message, assistant=assistant_response, has_attachment=has_attachment)
    try:
        raw = await llm.chat(
            messages=[{"role": "user", "content": build_prompt(slug, exchange, now=now)}],
            model=writer_model,
            # A failed memory step loses the meal or reading, so at peak load
            # it may use the configured fallback model like a reply does.
            allow_fallback=True,
            timeout_seconds=300.0,
            reasoning_effort_override=config.MEMORY_WRITER_EFFORT,
        )
    except Exception as e:  # noqa: BLE001 - never let memory crash the bot
        log.exception("writer_v2: model call failed")
        result = Result(error=f"model call failed: {e}")
        _audit(slug, {"time": now.isoformat(), "error": result.error, "user": user_message[:1200]})
        return result
    result = apply_payload(slug, raw, exchange, now=now)
    _audit(slug, {
        "time": now.isoformat(),
        "user": user_message[:1200],
        "assistant": assistant_response[:1200],
        "raw": raw[:6000],
        "applied": result.applied,
        "rejected": result.rejected,
        "error": result.error,
    })
    log.info("writer_v2: applied=%d rejected=%d%s", len(result.applied), len(result.rejected),
             f" error={result.error}" if result.error else "")
    return result
