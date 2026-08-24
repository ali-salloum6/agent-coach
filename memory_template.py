"""Canonical Markdown layout and recent-memory entry format.

The new memory architecture stores long-term, human-readable memory in
`data/<slug>.md` and short-lived TTL-bound notes in `data/<slug>.recent.md`.
This module defines:

- the section template and rendering for canonical memory,
- section-path helpers so the memory writer can route each JSON operation
  to a stable Markdown heading deterministically,
- the recent-memory entry format (one HTML comment with machine fields
  followed by a single human-readable bullet) and pruning logic that
  removes expired entries automatically.

The model never writes Markdown directly. All shape lives here.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import config

# Stable top-level sections (in display order).
CANONICAL_SECTIONS: tuple[str, ...] = (
    "Current Snapshot",
    "Stable Profile",
    "Active Issues",
    "Time Series",
    "Plans, Advice, And Rationale",
    "Timeline",
    "Manual Notes",
    "Superseded / Historical",
)

# Time-series subsections (heading text). The writer creates new ones on
# demand for unknown categories under this section to avoid losing data.
TIME_SERIES_DEFAULT_SUBSECTIONS: tuple[str, ...] = (
    "Weight",
    "Sleep",
    "Nutrition",
    "Workouts",
    "Pain",
    "Measurements",
    "Supplements",
    "Skin",
    "Mental Health",
)

# Categories the writer accepts on ops. Unknown values are rejected.
ALLOWED_CATEGORIES: frozenset[str] = frozenset(
    {
        "profile",
        "goal",
        "weight",
        "sleep",
        "nutrition",
        "workout",
        "injury",
        "pain",
        "supplement",
        "skin",
        "measurement",
        "mental_health",
        "preference",
        "logistics",
        "habit",
        "rationale",
        "milestone",
    }
)

# Map category → time-series subsection title.
_CATEGORY_TO_TIME_SERIES: dict[str, str] = {
    "weight": "Weight",
    "sleep": "Sleep",
    "nutrition": "Nutrition",
    "workout": "Workouts",
    "pain": "Pain",
    "measurement": "Measurements",
    "supplement": "Supplements",
    "skin": "Skin",
    "mental_health": "Mental Health",
}

# Magic prefix used on each recent-memory entry. The format is one HTML
# comment with machine-readable key=value fields followed by exactly one
# human-readable bullet line. Pruning is deterministic.
_RECENT_META_PREFIX = "<!-- recent:"
_RECENT_META_RE = re.compile(
    r"<!--\s*recent:\s*(?P<fields>.*?)\s*-->",
    re.DOTALL,
)


def now_local() -> datetime:
    return config.local_now()


def _agent_title(agent_slug: str) -> str:
    return agent_slug.replace("_", " ").title() + " Memory"


def canonical_template(agent_slug: str) -> str:
    """Render the default canonical Markdown file."""
    lines = [
        f"# {_agent_title(agent_slug)}",
        "",
        (
            "Curated long-term memory for the coach. Items are added by the "
            "structured memory writer (`memory_writer.py`) after every "
            "exchange. The recent, short-lived counterpart lives in "
            f"`data/{agent_slug}.recent.md` and prunes itself automatically."
        ),
        "",
    ]
    for section in CANONICAL_SECTIONS:
        lines.extend([f"## {section}", "", ""])
    if "Time Series" in CANONICAL_SECTIONS:
        # Pre-seed common time-series subsections so the file reads well.
        time_series_idx = lines.index("## Time Series") + 2
        sub_lines: list[str] = []
        for sub in TIME_SERIES_DEFAULT_SUBSECTIONS:
            sub_lines.extend([f"### {sub}", "", ""])
        lines[time_series_idx:time_series_idx] = sub_lines
    return "\n".join(lines).rstrip() + "\n"


def recent_template(agent_slug: str) -> str:
    """Render the default recent-memory Markdown file."""
    return (
        f"# {_agent_title(agent_slug).replace('Memory', 'Recent Memory')}\n"
        "\n"
        "Short-lived notes that matter today/this week and prune themselves "
        "automatically once their `expires` timestamp passes. Each entry is "
        "two lines: one HTML comment with machine fields and one bullet for "
        "humans.\n"
    )


def ensure_canonical(path: Path, agent_slug: str) -> None:
    if not path.exists() or not path.read_text(encoding="utf-8").strip():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(canonical_template(agent_slug), encoding="utf-8")


def ensure_recent(path: Path, agent_slug: str) -> None:
    if not path.exists() or not path.read_text(encoding="utf-8").strip():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(recent_template(agent_slug), encoding="utf-8")


# ---------------------------------------------------------------------------
# Section routing for canonical memory.


def time_series_subsection(category: str | None) -> str:
    """Return the time-series subsection title for a category."""
    if category and category in _CATEGORY_TO_TIME_SERIES:
        return _CATEGORY_TO_TIME_SERIES[category]
    # Auto-create a subsection from the category name (capitalized).
    return (category or "Other").replace("_", " ").title()


def append_under_heading(
    markdown: str,
    *,
    section: str,
    subsection: str | None,
    body: str,
) -> str:
    """Return new Markdown with `body` appended under section/subsection.

    Sections and subsections are H2/H3 respectively. Missing headings are
    created on demand. `body` should already contain its trailing newline.
    """
    section_re = re.compile(rf"^##\s+{re.escape(section)}\s*$", re.MULTILINE)
    match = section_re.search(markdown)
    if not match:
        # Append the section at the end of the file.
        if not markdown.endswith("\n"):
            markdown += "\n"
        markdown += f"\n## {section}\n\n"
        match = section_re.search(markdown)
        assert match is not None

    section_start = match.end()
    next_section_match = re.search(r"^##\s+\S", markdown[section_start:], re.MULTILINE)
    section_end = (
        section_start + next_section_match.start()
        if next_section_match
        else len(markdown)
    )

    if subsection is None:
        # Append at the end of the section, before any trailing blank lines.
        block = markdown[section_start:section_end].rstrip("\n")
        new_block = block + "\n\n" + body.rstrip("\n") + "\n\n"
        return markdown[:section_start] + new_block + markdown[section_end:]

    sub_re = re.compile(
        rf"^###\s+{re.escape(subsection)}\s*$",
        re.MULTILINE,
    )
    sub_match = sub_re.search(markdown, section_start, section_end)
    if not sub_match:
        # Insert a new subsection at the end of the section.
        block = markdown[section_start:section_end].rstrip("\n")
        new_block = (
            block
            + "\n\n"
            + f"### {subsection}\n\n"
            + body.rstrip("\n")
            + "\n\n"
        )
        return markdown[:section_start] + new_block + markdown[section_end:]

    sub_start = sub_match.end()
    next_sub_match = re.search(
        r"^(?:##\s+\S|###\s+\S)",
        markdown[sub_start:section_end],
        re.MULTILINE,
    )
    sub_end = (
        sub_start + next_sub_match.start()
        if next_sub_match
        else section_end
    )
    sub_block = markdown[sub_start:sub_end].rstrip("\n")
    new_sub_block = sub_block + "\n\n" + body.rstrip("\n") + "\n\n"
    return markdown[:sub_start] + new_sub_block + markdown[sub_end:]


# ---------------------------------------------------------------------------
# Recent-memory entries.


@dataclass(frozen=True)
class RecentEntry:
    created: datetime
    expires: datetime
    ttl_days: int
    category: str
    source: str
    summary: str

    def render(self) -> str:
        meta = (
            f"<!-- recent: created={self.created.isoformat()} "
            f"expires={self.expires.isoformat()} "
            f"ttl_days={self.ttl_days} "
            f"category={self.category} "
            f"source={self.source} -->"
        )
        created_local = self.created.astimezone(config.MEMORY_TIMEZONE).strftime(
            "%Y-%m-%d %H:%M"
        )
        expires_local = self.expires.astimezone(config.MEMORY_TIMEZONE).strftime(
            "%Y-%m-%d %H:%M"
        )
        bullet = (
            f"- [{created_local} {config.MEMORY_TIMEZONE_LABEL} | expires {expires_local} | "
            f"{self.category} · {self.source}] {self.summary.strip()}"
        )
        return f"{meta}\n{bullet}\n"


def build_recent_entry(
    *,
    summary: str,
    category: str,
    source: str,
    ttl_days: int,
    now: datetime | None = None,
) -> RecentEntry:
    created = (now or now_local()).astimezone(config.MEMORY_TIMEZONE)
    expires = created + timedelta(days=ttl_days)
    return RecentEntry(
        created=created,
        expires=expires,
        ttl_days=ttl_days,
        category=category,
        source=source,
        summary=summary,
    )


def append_recent_entry(path: Path, entry: RecentEntry) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or not path.read_text(encoding="utf-8").strip():
        path.write_text(recent_template(path.stem.replace(".recent", "")), encoding="utf-8")
    with path.open("a", encoding="utf-8") as f:
        f.write("\n" + entry.render())


def _parse_recent_meta(line: str) -> dict[str, str] | None:
    match = _RECENT_META_RE.search(line)
    if not match:
        return None
    fields_text = match.group("fields")
    fields: dict[str, str] = {}
    for token in fields_text.split():
        if "=" not in token:
            continue
        key, _, value = token.partition("=")
        fields[key] = value
    return fields


def _parse_iso_aware(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _bullet_index_after_meta(lines: list[str], meta_idx: int) -> int | None:
    """Return the index of the bullet line paired with a recent meta comment."""
    j = meta_idx + 1
    while j < len(lines) and not lines[j].strip():
        j += 1
    if j < len(lines) and lines[j].lstrip().startswith("- "):
        return j
    return None


def _archive_pruned_entries(
    path: Path,
    archived: list[dict[str, str]],
    *,
    now: datetime | None = None,
) -> None:
    if not archived:
        return
    archive_path = path.with_name(f"{path.stem}.pruned.jsonl")
    pruned_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    with archive_path.open("a", encoding="utf-8") as f:
        for entry in archived:
            f.write(json.dumps({"pruned_at": pruned_at, **entry}, ensure_ascii=False) + "\n")


def prune_expired(
    content: str,
    *,
    now: datetime | None = None,
) -> tuple[str, int, list[dict[str, str]]]:
    """Drop expired `<!-- recent: ... -->` + bullet entries.

    Returns (new_content, pruned_count, archived_entries). Entries without a
    `<!-- recent: -->` marker (e.g. manual notes) are preserved. Entries with
    a missing or unparseable `expires` timestamp are also preserved.
    """
    threshold = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    lines = content.splitlines()
    out: list[str] = []
    pruned = 0
    archived: list[dict[str, str]] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        fields = _parse_recent_meta(line)
        if not fields:
            out.append(line)
            i += 1
            continue
        expires_raw = fields.get("expires") or ""
        expires_at = _parse_iso_aware(expires_raw)
        bullet_idx = _bullet_index_after_meta(lines, i)
        if expires_at is not None and expires_at.astimezone(timezone.utc) <= threshold:
            archived.append(
                {
                    "created": fields.get("created", ""),
                    "expires": expires_raw,
                    "ttl_days": fields.get("ttl_days", ""),
                    "category": fields.get("category", ""),
                    "source": fields.get("source", ""),
                    "bullet": lines[bullet_idx] if bullet_idx is not None else "",
                }
            )
            i = (bullet_idx + 1) if bullet_idx is not None else i + 1
            pruned += 1
            continue
        out.append(line)
        if bullet_idx is not None:
            out.append(lines[bullet_idx])
            i = bullet_idx + 1
        else:
            i += 1
    new_content = "\n".join(out)
    if content.endswith("\n") and not new_content.endswith("\n"):
        new_content += "\n"
    return new_content, pruned, archived


def prune_recent_file(path: Path, *, now: datetime | None = None) -> int:
    if not path.exists():
        return 0
    content = path.read_text(encoding="utf-8")
    new_content, pruned, archived = prune_expired(content, now=now)
    if pruned:
        _archive_pruned_entries(path, archived, now=now)
        path.write_text(new_content, encoding="utf-8")
    return pruned


def visible_recent(content: str, *, now: datetime | None = None) -> str:
    """Return the recent file with expired entries dropped and `<!--` lines stripped.

    Used for injecting into the prompt: we don't want machine metadata
    visible to the model.
    """
    new_content, _, _ = prune_expired(content, now=now)
    lines = new_content.splitlines()
    cleaned = [line for line in lines if not line.lstrip().startswith(_RECENT_META_PREFIX)]
    return "\n".join(cleaned).strip() + ("\n" if cleaned else "")


def iter_recent_entries(content: str) -> Iterable[dict[str, str]]:
    """Yield `{fields..., bullet}` dicts for every non-expired recent entry.

    Convenience for tests and debugging.
    """
    lines = content.splitlines()
    i = 0
    while i < len(lines):
        fields = _parse_recent_meta(lines[i])
        if fields:
            bullet = ""
            if i + 1 < len(lines) and lines[i + 1].lstrip().startswith("- "):
                bullet = lines[i + 1]
                i += 2
            else:
                i += 1
            yield {**fields, "bullet": bullet}
        else:
            i += 1
