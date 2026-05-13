"""Strict JSON memory-operation schema and validator.

The LLM proposes JSON operations; this module decides which operations are
safe to apply. Anything that does not match the schema, lacks an exact
evidence quote in the source it claims, references an unknown category, or
violates source/op pairing rules is rejected. Accepted operations are also
normalized (e.g. assistant plans referencing "today"/"this evening" get
downgraded from `long_term` to `recent` retention).

The model never writes Markdown. Code owns formatting and placement.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date as date_cls, datetime
from typing import Any

from memory_template import ALLOWED_CATEGORIES

log = logging.getLogger(__name__)

ALLOWED_OPS: frozenset[str] = frozenset(
    {
        "noop",
        "add_recent_note",
        "add_measurement",
        "add_current_fact",
        "add_event",
        "add_plan",
        "mark_adopted",
        "supersede",
    }
)
ALLOWED_SOURCES: frozenset[str] = frozenset({"user", "assistant"})
ALLOWED_RETENTIONS: frozenset[str] = frozenset({"recent", "long_term", "historical"})
ALLOWED_STATUSES: frozenset[str] = frozenset(
    {"proposed", "active", "adopted", "superseded"}
)
ALLOWED_TTL_DAYS: frozenset[int] = frozenset({1, 2, 7, 14, 30})

# Hard limit per exchange to keep memory growth bounded.
MAX_ACCEPTED_OPS = 5

# Summary length cap (also used as a soft anti-bundle signal).
MAX_SUMMARY_CHARS = 320
MAX_EVIDENCE_CHARS = 600

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# When an assistant plan summary contains any of these phrases, it is forced
# to recent retention so we don't pile single-day prescriptions onto the
# long-term plan list. The validation findings showed Gemini still wanted to
# save same-day plans as long-term unless we enforce this in code.
_TRANSIENT_PLAN_MARKERS: tuple[str, ...] = (
    "today",
    "tonight",
    "this evening",
    "this morning",
    "this afternoon",
    "right now",
    "for this session",
    "for this workout",
    "this workout",
    "this session",
)


class MemoryOpsError(Exception):
    """Raised when the model output cannot be parsed at all."""


@dataclass
class ValidatedOp:
    op: str
    source: str
    retention: str
    summary: str
    evidence_quote: str
    date: str | None = None
    category: str | None = None
    value: Any = None
    unit: str | None = None
    ttl_days: int | None = None
    status: str | None = None
    target_ref: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class RejectedOp:
    raw: dict[str, Any]
    reason: str


@dataclass
class ValidationResult:
    accepted: list[ValidatedOp] = field(default_factory=list)
    rejected: list[RejectedOp] = field(default_factory=list)
    parse_error: str | None = None
    raw_payload: str = ""

    @property
    def any_accepted(self) -> bool:
        return bool(self.accepted)


def parse_llm_payload(text: str) -> dict[str, Any]:
    """Parse a model response that should contain `{"ops": [...]}`.

    Accepts fenced JSON (```json ... ```) and tolerates leading text only
    when JSON starts with `{`. Raises MemoryOpsError on failure.
    """
    if not text or not text.strip():
        raise MemoryOpsError("empty model output")
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    if not stripped.startswith("{"):
        # Find the first JSON object boundary.
        idx = stripped.find("{")
        if idx < 0:
            raise MemoryOpsError("no JSON object in model output")
        stripped = stripped[idx:]
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as e:
        raise MemoryOpsError(f"invalid JSON: {e}") from e
    if not isinstance(data, dict):
        raise MemoryOpsError("top-level JSON must be an object")
    return data


def _normalize_date(value: Any) -> tuple[str | None, str | None]:
    if value is None or value == "":
        return None, None
    if isinstance(value, str):
        if _DATE_RE.match(value):
            try:
                datetime.strptime(value, "%Y-%m-%d")
                return value, None
            except ValueError as e:
                return None, f"invalid date: {e}"
        return None, f"date must be YYYY-MM-DD, got '{value}'"
    if isinstance(value, date_cls):
        return value.isoformat(), None
    return None, f"date must be a string, got {type(value).__name__}"


def _evidence_in_source(evidence: str, source_text: str) -> bool:
    """Return True if `evidence` appears in `source_text` (after light normalization).

    Models occasionally add/remove smart quotes or normalize whitespace, so
    we compare with collapsed whitespace and unicode-quote substitution.
    """
    def _norm(s: str) -> str:
        s = s.replace("\u2018", "'").replace("\u2019", "'")
        s = s.replace("\u201c", '"').replace("\u201d", '"')
        s = re.sub(r"\s+", " ", s)
        return s.strip().lower()

    needle = _norm(evidence)
    haystack = _norm(source_text)
    return bool(needle) and needle in haystack


def _is_transient_plan(summary: str) -> bool:
    lowered = summary.lower()
    return any(marker in lowered for marker in _TRANSIENT_PLAN_MARKERS)


def _validate_one(
    raw: dict[str, Any],
    *,
    user_message: str,
    assistant_response: str,
) -> ValidatedOp | RejectedOp:
    if not isinstance(raw, dict):
        return RejectedOp(raw={}, reason="op must be an object")

    op_name = raw.get("op")
    if not isinstance(op_name, str) or op_name not in ALLOWED_OPS:
        return RejectedOp(raw=raw, reason=f"unknown op: {op_name!r}")

    if op_name == "noop":
        # `noop` is allowed but produces no write. Keep raw for the audit log.
        return ValidatedOp(
            op="noop",
            source="user",
            retention="recent",
            summary=(raw.get("reason") or "").strip()[:MAX_SUMMARY_CHARS] or "no-op",
            evidence_quote="",
            raw=raw,
        )

    source = raw.get("source")
    if source not in ALLOWED_SOURCES:
        return RejectedOp(raw=raw, reason=f"source must be user/assistant, got {source!r}")

    retention = raw.get("retention")
    if retention not in ALLOWED_RETENTIONS:
        return RejectedOp(
            raw=raw, reason=f"retention must be recent/long_term/historical, got {retention!r}"
        )

    summary = raw.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return RejectedOp(raw=raw, reason="summary must be a non-empty string")
    summary = summary.strip()
    if len(summary) > MAX_SUMMARY_CHARS:
        return RejectedOp(
            raw=raw,
            reason=f"summary too long ({len(summary)} > {MAX_SUMMARY_CHARS})",
        )

    evidence_quote = raw.get("evidence_quote")
    if not isinstance(evidence_quote, str) or not evidence_quote.strip():
        return RejectedOp(raw=raw, reason="evidence_quote must be a non-empty string")
    if len(evidence_quote) > MAX_EVIDENCE_CHARS:
        return RejectedOp(
            raw=raw,
            reason=f"evidence_quote too long ({len(evidence_quote)} > {MAX_EVIDENCE_CHARS})",
        )
    source_text = user_message if source == "user" else assistant_response
    if not _evidence_in_source(evidence_quote, source_text):
        return RejectedOp(raw=raw, reason="evidence_quote not found in claimed source")

    date_value, date_error = _normalize_date(raw.get("date"))
    if date_error:
        return RejectedOp(raw=raw, reason=date_error)

    category = raw.get("category")
    if category is not None:
        if not isinstance(category, str):
            return RejectedOp(raw=raw, reason="category must be a string")
        category = category.strip().lower()
        if category not in ALLOWED_CATEGORIES:
            return RejectedOp(raw=raw, reason=f"category not allowed: {category!r}")
    # Category requirements per op.
    if op_name in {"add_recent_note", "add_measurement", "add_current_fact", "add_plan"} and not category:
        return RejectedOp(raw=raw, reason=f"{op_name} requires a category")

    status = raw.get("status")
    if status is not None:
        if not isinstance(status, str) or status.lower() not in ALLOWED_STATUSES:
            return RejectedOp(raw=raw, reason=f"invalid status: {status!r}")
        status = status.lower()

    ttl_days = raw.get("ttl_days")
    if op_name == "add_recent_note":
        if ttl_days is None:
            ttl_days = 2
        if not isinstance(ttl_days, int) or ttl_days not in ALLOWED_TTL_DAYS:
            return RejectedOp(
                raw=raw,
                reason=f"ttl_days must be one of {sorted(ALLOWED_TTL_DAYS)}, got {ttl_days!r}",
            )
        retention = "recent"
    else:
        if ttl_days is not None and ttl_days not in ALLOWED_TTL_DAYS:
            return RejectedOp(
                raw=raw,
                reason=f"ttl_days must be one of {sorted(ALLOWED_TTL_DAYS)}, got {ttl_days!r}",
            )

    # Op-specific structural rules.
    if op_name == "add_current_fact" and source != "user":
        return RejectedOp(raw=raw, reason="add_current_fact must be source=user")
    if op_name == "mark_adopted" and source != "user":
        return RejectedOp(raw=raw, reason="mark_adopted must be source=user")
    if op_name == "supersede" and source != "user":
        return RejectedOp(raw=raw, reason="supersede must be source=user")

    if op_name == "add_plan":
        # Downgrade transient assistant plans to recent retention.
        if retention == "long_term" and _is_transient_plan(summary):
            retention = "recent"
            ttl_days = ttl_days or 7
        if retention == "recent" and ttl_days is None:
            ttl_days = 7

    if op_name in {"mark_adopted", "supersede"}:
        target_ref = raw.get("target_ref") or raw.get("target")
        if not isinstance(target_ref, str) or not target_ref.strip():
            return RejectedOp(
                raw=raw, reason=f"{op_name} requires target_ref text from existing memory"
            )
        target_ref = target_ref.strip()
    else:
        target_ref = None

    return ValidatedOp(
        op=op_name,
        source=source,
        retention=retention,
        summary=summary,
        evidence_quote=evidence_quote.strip(),
        date=date_value,
        category=category,
        value=raw.get("value"),
        unit=raw.get("unit") if isinstance(raw.get("unit"), str) else None,
        ttl_days=ttl_days,
        status=status,
        target_ref=target_ref,
        raw=raw,
    )


def validate(
    payload_text: str,
    *,
    user_message: str,
    assistant_response: str,
) -> ValidationResult:
    """Top-level validator: parse, validate, cap, and report.

    Always returns a `ValidationResult`. Never raises.
    """
    result = ValidationResult(raw_payload=payload_text or "")

    try:
        data = parse_llm_payload(payload_text)
    except MemoryOpsError as e:
        result.parse_error = str(e)
        return result

    ops = data.get("ops")
    if not isinstance(ops, list):
        result.parse_error = "payload missing list 'ops'"
        return result

    for raw_op in ops:
        outcome = _validate_one(
            raw_op if isinstance(raw_op, dict) else {"raw": raw_op},
            user_message=user_message,
            assistant_response=assistant_response,
        )
        if isinstance(outcome, ValidatedOp):
            if outcome.op == "noop":
                result.accepted.append(outcome)
                continue
            if (
                sum(1 for op in result.accepted if op.op != "noop")
                >= MAX_ACCEPTED_OPS
            ):
                result.rejected.append(
                    RejectedOp(raw=outcome.raw, reason=f"exceeds MAX_ACCEPTED_OPS={MAX_ACCEPTED_OPS}")
                )
                continue
            result.accepted.append(outcome)
        else:
            result.rejected.append(outcome)

    return result
