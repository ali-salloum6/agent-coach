"""Lookups for the v2 memory: the food reference and the archive.

Neither is pasted into the prompt wholesale. For each message the bot adds
only what matches it:

- food reference entries whose name (or an alias) is mentioned;
- archive entries that share the message's distinctive words.

The archive is everything the cards and logs do not hold: the frozen old
memory file, expired short-lived notes and the chat itself. Matching is
plain word overlap weighted by how rare a word is, which is enough for a
few megabytes of text and needs no external service.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import config
import ledger

STOPWORDS = frozenset(
    """a an and are as at be been but by can did do does for from had has have how i if in is it its
    just me my no not of on or so some than that the their them then there these they this to today
    too was we were what when where which who why will with you your about add added again also am
    any ate got had have having like more much one same that's two user's user yesterday
    could day far get go going hello hi know let make need now ok okay please really should still
    take thank thanks think want week would yes""".split()
)
STEM_LENGTH = 7  # words are compared by their first letters: "photographer" finds "photography"
RARE_SHARE = 0.05  # a "distinctive" word appears in at most this share of archive entries
SOLO_SHARE = 0.01  # a single shared word is enough only if it is this rare


def tokens(text: str) -> list[str]:
    """Lowercase words without stopwords; plurals folded ("strips" -> "strip")."""
    out = []
    for word in re.findall(r"[a-z0-9]+(?:'[a-z]+)?", text.lower()):
        word = word.split("'")[0]
        if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        if len(word) > 1 and word not in STOPWORDS:
            out.append(word)
    return out


def stems(text: str) -> set[str]:
    """Words cut to their first letters, for matching across word forms."""
    return {word[:STEM_LENGTH] for word in tokens(text)}


# ---------------------------------------------------------------------------
# Food reference.


def foodbook_path(agent_slug: str) -> Path:
    return ledger.agent_dir(agent_slug) / "foodbook.jsonl"


def load_foodbook(agent_slug: str) -> list[dict[str, Any]]:
    path = foodbook_path(agent_slug)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def match_foods(foodbook: Iterable[dict[str, Any]], message: str, *, limit: int = 6) -> list[dict[str, Any]]:
    """Entries named in the message, best match first.

    An entry matches when every word of its name or of one alias appears in
    the message, or when at least two of its words do and they cover most of
    the name ("corner cafe waffle" finds "Corner Cafe medium protein waffle").
    """
    said = set(tokens(message))
    scored = []
    for entry in foodbook:
        best = (0.0, 0)
        for candidate in [entry["name"], *entry.get("aliases", [])]:
            words = set(tokens(candidate))
            if not words:
                continue
            hit = len(words & said)
            cover = hit / len(words)
            if cover == 1.0 or (hit >= 2 and cover >= 0.5):
                best = max(best, (cover, hit))
        if best[1]:
            scored.append((best, entry.get("updated", ""), entry))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [entry for _, _, entry in scored[:limit]]


def food_line(entry: dict[str, Any]) -> str:
    parts = []
    if entry.get("kcal") is not None:
        parts.append(f"{entry['kcal']:g} kcal")
    if entry.get("protein_g") is not None:
        parts.append(f"{entry['protein_g']:g} g protein")
    serving = f" ({entry['serving']})" if entry.get("serving") else ""
    note = f" Note: {entry['note']}" if entry.get("note") else ""
    values = ", ".join(parts) if parts else "no values recorded"
    return f"- {entry['name']}{serving}: {values}.{note}"


# ---------------------------------------------------------------------------
# Archive.


@dataclass(frozen=True)
class Doc:
    date: str  # YYYY-MM-DD, or "" when unknown
    text: str
    origin: str  # which archive file it came from


def archive_dir(agent_slug: str) -> Path:
    return ledger.agent_dir(agent_slug) / "archive"


def _docs_from_file(path: Path) -> list[Doc]:
    docs: list[Doc] = []
    if path.suffix == ".jsonl":
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            text = row.get("bullet") or row.get("text") or ""
            if not text and "user" in row:
                text = f"User: {row.get('user', '')}\nCoach: {row.get('assistant', '')}"
            date = str(row.get("date") or row.get("created") or "")[:10]
            if text:
                docs.append(Doc(date, text.strip(), path.name))
    else:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("- "):
                m = re.match(r"- \[(\d{4}-\d{2}-\d{2})", line)
                docs.append(Doc(m.group(1) if m else "", line[2:], path.name))
    return docs


_cache: dict[str, tuple[tuple, list[tuple[Doc, frozenset[str]]], dict[str, int]]] = {}


def _load_archive(agent_slug: str) -> tuple[list[tuple[Doc, frozenset[str]]], dict[str, int]]:
    """All archive entries with their words, and how many entries use each word.

    Cached until an archive file changes.
    """
    folder = archive_dir(agent_slug)
    files = sorted(p for p in folder.glob("*") if p.suffix in (".md", ".jsonl")) if folder.exists() else []
    # Short-lived notes that expire are moved here by memory_template.
    pruned = config.DATA_DIR / f"{agent_slug}.recent.pruned.jsonl"
    if pruned.exists():
        files.append(pruned)
    state = tuple((p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in files)
    cached = _cache.get(agent_slug)
    if cached and cached[0] == state:
        return cached[1], cached[2]
    docs = [(doc, frozenset(stems(doc.text))) for path in files for doc in _docs_from_file(path)]
    counts: dict[str, int] = {}
    for _, words in docs:
        for word in words:
            counts[word] = counts.get(word, 0) + 1
    _cache[agent_slug] = (state, docs, counts)
    return docs, counts


def search_archive(
    agent_slug: str, query: str, *, limit: int = 5, min_ratio: float = 0.6, strict: bool = False
) -> list[Doc]:
    """Archive entries sharing the query's distinctive words, best first.

    An entry must share a distinctive word with the query: one that few
    archive entries use. Sharing a single word is enough only when that word
    is very rare (a name such as "pumpuccino"). Entries scoring well below
    the best match are dropped, and ties go to the newer entry.

    `strict` is for lookups nobody asked for (adding context to a message):
    it always requires a very rare shared word, so ordinary sentences match
    nothing.
    """
    docs, counts = _load_archive(agent_slug)
    wanted = stems(query)
    if not wanted or not docs:
        return []
    total = len(docs)
    rare_max = max(2, int(total * RARE_SHARE))
    solo_max = max(1, int(total * SOLO_SHARE))
    scored = []
    for doc, words in docs:
        shared = wanted & words
        if not shared:
            continue
        rarest = min(counts[word] for word in shared)
        if rarest > rare_max or ((strict or len(shared) == 1) and rarest > solo_max):
            continue
        score = sum(math.log(1 + total / counts[word]) for word in shared)
        scored.append((score, doc.date, doc))
    if not scored:
        return []
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    best = scored[0][0]
    seen: set[frozenset[str]] = set()
    results = []
    for score, _, doc in scored:
        if score < best * min_ratio:
            break
        key = frozenset(stems(doc.text))
        if key in seen:
            continue
        seen.add(key)
        results.append(doc)
        if len(results) == limit:
            break
    return results
