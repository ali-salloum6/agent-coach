"""Structured memory writer.

Orchestration after every assistant reply:

1. Build a NARROW input for the writer model (current snapshot, active
   issues, active plans, non-expired recent memory, and the new exchange).
2. Ask the writer model for JSON memory operations (`llm.propose_memory_ops`).
3. Validate the JSON deterministically (`memory_ops.validate`).
4. Apply accepted operations to the canonical and/or recent Markdown
   files via `memory_template` helpers.
5. Record every accepted/rejected operation to a JSONL audit log so we
   can debug or replay later.
6. Prune expired recent notes opportunistically.

Anything that does not look safe is rejected and logged but not written.
The model never writes Markdown directly — code owns shape.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import config
import llm
import memory_ops
import memory_template

log = logging.getLogger(__name__)

MOSCOW = ZoneInfo("Europe/Moscow")


def _canonical_path(agent_slug: str) -> Path:
    return config.DATA_DIR / f"{agent_slug}.md"


def _recent_path(agent_slug: str) -> Path:
    return config.DATA_DIR / f"{agent_slug}.recent.md"


def _audit_path(agent_slug: str) -> Path:
    return config.DATA_DIR / f"{agent_slug}.memory_ops.jsonl"


# ---------------------------------------------------------------------------
# Reading helpers.


_SECTION_RE_TMPL = r"^##\s+{name}\s*$"


def _read_section(markdown: str, name: str) -> str:
    pattern = re.compile(_SECTION_RE_TMPL.format(name=re.escape(name)), re.MULTILINE)
    match = pattern.search(markdown)
    if not match:
        return ""
    start = match.end()
    next_match = re.search(r"^##\s+\S", markdown[start:], re.MULTILINE)
    end = start + next_match.start() if next_match else len(markdown)
    return markdown[start:end].strip()


def _load_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Public API used by bot.py.


@dataclass
class ApplyResult:
    accepted: int = 0
    rejected: int = 0
    pruned_recent: int = 0
    parse_error: str | None = None
    details: list[dict] = field(default_factory=list)

    @property
    def did_anything(self) -> bool:
        return self.accepted > 0 or self.rejected > 0 or self.pruned_recent > 0


def ensure_files(agent_slug: str) -> None:
    """Create canonical and recent files with their templates if missing."""
    memory_template.ensure_canonical(_canonical_path(agent_slug), agent_slug)
    memory_template.ensure_recent(_recent_path(agent_slug), agent_slug)


def prune_recent(agent_slug: str, *, now: datetime | None = None) -> int:
    """Drop expired recent notes from disk. Idempotent. Returns count pruned."""
    return memory_template.prune_recent_file(_recent_path(agent_slug), now=now)


def load_combined_memory(agent_slug: str, *, now: datetime | None = None) -> str:
    """Return canonical + non-expired recent memory, joined for the prompt.

    Expired entries are pruned as a side effect so the file stays small.
    The recent section is rendered without HTML comment metadata so the
    model only sees human-readable bullets.
    """
    ensure_files(agent_slug)
    prune_recent(agent_slug, now=now)
    canonical = _load_text(_canonical_path(agent_slug)).strip()
    recent_raw = _load_text(_recent_path(agent_slug))
    recent_visible = memory_template.visible_recent(recent_raw, now=now).strip()
    if not recent_visible:
        return canonical
    return (
        canonical
        + "\n\n"
        + "## Recent Notes (auto-expiring)\n\n"
        + recent_visible
        + "\n"
    )


async def propose_and_apply(
    agent_slug: str,
    user_message: str,
    assistant_response: str,
    *,
    timestamp: datetime | None = None,
    writer_model: str | None = None,
) -> ApplyResult:
    """Full pipeline: prompt model, validate, apply, audit, prune.

    Failure-tolerant: returns an `ApplyResult` either way and never raises
    so the background task can swallow errors safely.
    """
    ensure_files(agent_slug)
    now_dt = (timestamp or datetime.now(MOSCOW)).astimezone(MOSCOW)

    pruned = prune_recent(agent_slug, now=now_dt)
    result = ApplyResult(pruned_recent=pruned)

    canonical = _load_text(_canonical_path(agent_slug))
    recent_raw = _load_text(_recent_path(agent_slug))

    current_snapshot = _read_section(canonical, "Current Snapshot")
    active_issues = _read_section(canonical, "Active Issues")
    active_plans = _read_section(canonical, "Plans, Advice, And Rationale")
    recent_visible = memory_template.visible_recent(recent_raw, now=now_dt)

    try:
        raw_response = await llm.propose_memory_ops(
            user_message=user_message,
            assistant_response=assistant_response,
            current_snapshot=current_snapshot,
            active_issues=active_issues,
            active_plans=active_plans,
            recent_memory_visible=recent_visible,
            timestamp_iso=now_dt.isoformat(),
            model=writer_model,
        )
    except Exception as e:  # noqa: BLE001 - never let extraction crash the bot.
        log.exception("memory_writer: model call failed")
        result.parse_error = f"model call failed: {e}"
        _append_audit(
            agent_slug,
            {
                "timestamp": now_dt.isoformat(),
                "phase": "propose",
                "error": str(e),
                "exchange": {
                    "user_message": _truncate(user_message),
                    "assistant_response": _truncate(assistant_response),
                },
            },
        )
        return result

    validation = memory_ops.validate(
        raw_response,
        user_message=user_message,
        assistant_response=assistant_response,
    )

    # Apply accepted ops in order.
    for op in validation.accepted:
        if op.op == "noop":
            result.details.append({"op": "noop", "summary": op.summary})
            continue
        try:
            _apply_op(agent_slug, op, now=now_dt)
            result.accepted += 1
            result.details.append(_op_to_audit(op, status="accepted"))
        except Exception as e:  # noqa: BLE001 - audit and move on.
            log.exception("memory_writer: failed to apply op")
            result.rejected += 1
            result.details.append(
                _op_to_audit(op, status="apply_failed", error=str(e))
            )

    for rej in validation.rejected:
        result.rejected += 1
        result.details.append(
            {
                "op": rej.raw.get("op"),
                "source": rej.raw.get("source"),
                "summary": rej.raw.get("summary"),
                "status": "rejected",
                "reason": rej.reason,
            }
        )

    if validation.parse_error:
        result.parse_error = validation.parse_error

    _append_audit(
        agent_slug,
        {
            "timestamp": now_dt.isoformat(),
            "phase": "propose_and_apply",
            "raw_payload": _truncate(raw_response, limit=4000),
            "exchange": {
                "user_message": _truncate(user_message),
                "assistant_response": _truncate(assistant_response),
            },
            "result": {
                "accepted": result.accepted,
                "rejected": result.rejected,
                "pruned_recent": result.pruned_recent,
                "parse_error": result.parse_error,
            },
            "ops": result.details,
        },
    )

    return result


# ---------------------------------------------------------------------------
# Op application.


def _format_date_prefix(op: memory_ops.ValidatedOp, now: datetime) -> str:
    if op.date:
        return op.date
    return now.astimezone(MOSCOW).strftime("%Y-%m-%d")


def _render_value_unit(op: memory_ops.ValidatedOp) -> str:
    if op.value is None and not op.unit:
        return ""
    value = op.value if op.value is not None else ""
    unit = f" {op.unit}" if op.unit else ""
    return f" ({value}{unit})" if str(value) or unit else ""


def _bullet(op: memory_ops.ValidatedOp, *, prefix: str = "") -> str:
    src = "user" if op.source == "user" else "assistant"
    cat = op.category or "other"
    bits = [
        f"- [{prefix}]" if prefix else "-",
        f"_{src} · {cat}_",
        op.summary.strip(),
    ]
    # Compact join while skipping empty leading bullet token.
    return " ".join(b for b in bits if b)


def _apply_op(
    agent_slug: str,
    op: memory_ops.ValidatedOp,
    *,
    now: datetime,
) -> None:
    if op.retention == "recent" or op.op == "add_recent_note":
        ttl_days = op.ttl_days or 2
        category = op.category or "other"
        entry = memory_template.build_recent_entry(
            summary=op.summary,
            category=category,
            source=op.source,
            ttl_days=ttl_days,
            now=now,
        )
        memory_template.append_recent_entry(_recent_path(agent_slug), entry)
        return

    canonical_path = _canonical_path(agent_slug)
    markdown = _load_text(canonical_path)

    if op.op == "add_measurement":
        subsection = memory_template.time_series_subsection(op.category)
        prefix = _format_date_prefix(op, now)
        line = f"- [{prefix}] {op.summary.strip()}{_render_value_unit(op)}"
        new_md = memory_template.append_under_heading(
            markdown,
            section="Time Series",
            subsection=subsection,
            body=line + "\n",
        )
    elif op.op == "add_current_fact":
        prefix = _format_date_prefix(op, now)
        line = f"- [{prefix}] {op.summary.strip()} _(user · {op.category})_"
        new_md = memory_template.append_under_heading(
            markdown,
            section="Current Snapshot",
            subsection=None,
            body=line + "\n",
        )
    elif op.op == "add_event":
        prefix = _format_date_prefix(op, now)
        line = (
            f"- [{prefix}] {op.summary.strip()} "
            f"_({op.source} · {op.category or 'event'})_"
        )
        new_md = memory_template.append_under_heading(
            markdown,
            section="Timeline",
            subsection=None,
            body=line + "\n",
        )
    elif op.op == "add_plan":
        prefix = _format_date_prefix(op, now)
        status = op.status or "proposed"
        line = (
            f"- [{prefix}] {op.summary.strip()} "
            f"_({op.source} · {op.category or 'plan'} · status={status})_"
        )
        new_md = memory_template.append_under_heading(
            markdown,
            section="Plans, Advice, And Rationale",
            subsection=None,
            body=line + "\n",
        )
    elif op.op == "mark_adopted":
        new_md = _supersede_or_adopt(
            markdown,
            target_ref=op.target_ref or "",
            new_status="adopted",
            note=op.summary,
            now=now,
            move_to_historical=False,
        )
    elif op.op == "supersede":
        new_md = _supersede_or_adopt(
            markdown,
            target_ref=op.target_ref or "",
            new_status="superseded",
            note=op.summary,
            now=now,
            move_to_historical=True,
        )
    else:
        # Defensive default: append a Manual Notes entry.
        prefix = _format_date_prefix(op, now)
        line = f"- [{prefix}] {op.summary.strip()} _(uncategorized: {op.op})_"
        new_md = memory_template.append_under_heading(
            markdown,
            section="Manual Notes",
            subsection=None,
            body=line + "\n",
        )

    canonical_path.write_text(new_md, encoding="utf-8")


def _supersede_or_adopt(
    markdown: str,
    *,
    target_ref: str,
    new_status: str,
    note: str,
    now: datetime,
    move_to_historical: bool,
) -> str:
    """Annotate or move a referenced line.

    First-pass behavior: we never silently delete prior content. If the
    target line is found, we append an annotation right after it; if move
    is requested, the matched line plus annotation are also appended under
    `## Superseded / Historical` for traceability. The original line stays
    in place too so we don't lose context — `## Superseded / Historical`
    accumulates the audit trail.
    """
    timestamp = now.astimezone(MOSCOW).strftime("%Y-%m-%d %H:%M")
    annotation = (
        f"  - [{timestamp}] status={new_status} — {note.strip()}"
    )
    if not target_ref.strip():
        return memory_template.append_under_heading(
            markdown,
            section="Manual Notes",
            subsection=None,
            body=f"- [{timestamp}] {note.strip()} _(supersede with no target)_ \n",
        )

    needle = target_ref.strip()
    lines = markdown.splitlines()
    found_idx = None
    for idx, line in enumerate(lines):
        if needle in line:
            found_idx = idx
            break

    if found_idx is None:
        # Could not find the referenced line; append to Manual Notes for review.
        return memory_template.append_under_heading(
            markdown,
            section="Manual Notes",
            subsection=None,
            body=(
                f"- [{timestamp}] {note.strip()} "
                f"_(supersede target not found: {target_ref!r})_\n"
            ),
        )

    new_lines = list(lines)
    new_lines.insert(found_idx + 1, annotation)
    new_md = "\n".join(new_lines)
    if markdown.endswith("\n") and not new_md.endswith("\n"):
        new_md += "\n"

    if move_to_historical:
        archive_line = (
            f"- [{timestamp}] {needle} → status={new_status}; {note.strip()}"
        )
        new_md = memory_template.append_under_heading(
            new_md,
            section="Superseded / Historical",
            subsection=None,
            body=archive_line + "\n",
        )

    return new_md


# ---------------------------------------------------------------------------
# Audit log helpers.


def _truncate(text: str, *, limit: int = 1200) -> str:
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _op_to_audit(
    op: memory_ops.ValidatedOp,
    *,
    status: str,
    error: str | None = None,
) -> dict:
    return {
        "op": op.op,
        "source": op.source,
        "retention": op.retention,
        "category": op.category,
        "date": op.date,
        "ttl_days": op.ttl_days,
        "status": status,
        "summary": op.summary,
        "evidence_quote": _truncate(op.evidence_quote, limit=400),
        "error": error,
    }


def _append_audit(agent_slug: str, payload: dict) -> None:
    path = _audit_path(agent_slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError as e:
        log.warning("memory_writer: could not write audit log: %s", e)


__all__ = [
    "ApplyResult",
    "ensure_files",
    "load_combined_memory",
    "propose_and_apply",
    "prune_recent",
]
