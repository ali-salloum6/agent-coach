#!/usr/bin/env python3
"""Convert the old single-file memory (`data/<slug>.md`) into the v2 stores.

The old file is one long list of sentences. This script handles the parts
that can be converted by rule, and prepares the rest for sorting:

  split    Number every entry and write one text file per section, ready
           to be sorted into profile, threads, foodbook, playbook, story or
           archive.
  logs     Convert the "Time Series" and "Timeline" sections into the
           structured logs (`data/<slug>/logs/*.jsonl`): body metrics, daily
           food totals and events. Entries the rules cannot place are
           listed for review and stay in the archive.
  merge    Check rows that were extracted from free text (lifts, readings
           found in short-lived notes, food reference entries) against the
           lines they cite, then merge them into the stores.
  verify   Check that every number in drafted cards appears in the lines
           they cite, or in the logs for lines citing "(weight log)" and the
           like.
  delta    List entries and short-lived notes the live bot added after the
           freeze, matched by text, to carry over when switching.
  report   Summarise where every entry of the old file went.

Nothing is ever deleted: the source file is only read. Run it on a frozen
copy, for example:

  python scripts/migrate_memory_v2.py split  --source data/freeze/gym_coach.md --work data/freeze/work
  python scripts/migrate_memory_v2.py logs   --source data/freeze/gym_coach.md --work data/freeze/work --slug gym_coach
  python scripts/migrate_memory_v2.py report --source data/freeze/gym_coach.md --work data/freeze/work

`logs` accepts hand-made corrections in `<work>/overrides.jsonl`, one JSON
object per line:
  {"n": 123, "skip": true, "why": "restates another day"}
  {"n": 123, "metric": "weight", "date": "2026-03-17", "value": 95.4, "unit": "kg", "cond": ""}
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date as date_cls, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import ledger  # noqa: E402

DESTINATIONS = ("profile", "threads", "foodbook", "playbook", "story", "archive")


# ---------------------------------------------------------------------------
# Parsing the old file.


@dataclass
class Entry:
    n: int  # line number of the entry in the source file (its id)
    section: str
    sub: str | None
    line: str
    notes: list[str] = field(default_factory=list)

    @property
    def date(self) -> str | None:
        m = re.match(r"- \[(\d{4}-\d{2}-\d{2})", self.line)
        return m.group(1) if m else None

    @property
    def text(self) -> str:
        """The sentence without the date prefix and the trailing tag."""
        body = re.sub(r"^- \[[^\]]*\]\s*", "", self.line)
        return re.sub(r"\s*_\([^)]*\)_\s*$", "", body).strip()

    @property
    def category(self) -> str | None:
        m = re.search(r"_\(\w+ · ([\w_]+)", self.line)
        return m.group(1) if m else None


def parse_entries(markdown: str) -> list[Entry]:
    entries: list[Entry] = []
    section: str | None = None
    sub: str | None = None
    for i, line in enumerate(markdown.splitlines(), 1):
        if line.startswith("## "):
            section, sub = line[3:].strip(), None
        elif line.startswith("### "):
            sub = line[4:].strip()
        elif line.startswith("- ") and section:
            entries.append(Entry(i, section, sub, line))
        elif line.startswith("  ") and line.strip() and entries and entries[-1].section == section:
            entries[-1].notes.append(line)
    return entries


# ---------------------------------------------------------------------------
# split


def cmd_split(args: argparse.Namespace) -> None:
    entries = parse_entries(Path(args.source).read_text(encoding="utf-8"))
    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)

    def dump(name: str, items: list[Entry]) -> None:
        lines: list[str] = []
        for e in items:
            lines.append(f"L{e.n}| {e.line}")
            lines += ["      " + note for note in e.notes]
        (work / name).write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"{len(items):>5} entries  {name}")

    foodish = {"nutrition", "supplement", "preference"}
    snapshot = [e for e in entries if e.section == "Current Snapshot"]
    dump("snapshot_food.txt", [e for e in snapshot if e.category in foodish])
    dump("snapshot_rest.txt", [e for e in snapshot if e.category not in foodish])
    plans = [e for e in entries if e.section == "Plans, Advice, And Rationale"]
    month = lambda e: (e.date or "")[:7]  # noqa: E731
    dump("plans_1_mar_apr.txt", [e for e in plans if month(e) <= "2026-04"])
    dump("plans_2_may_jun.txt", [e for e in plans if "2026-05" <= month(e) <= "2026-06"])
    dump("plans_3_jul_nov.txt", [e for e in plans if month(e) >= "2026-07"])
    dump("superseded.txt", [e for e in entries if e.section == "Superseded / Historical"])
    dump("manual_notes.txt", [e for e in entries if e.section == "Manual Notes"])
    dump("timeline.txt", [e for e in entries if e.section == "Timeline"])
    dump("workouts.txt", [e for e in entries if e.sub == "Workouts"])
    # Corrections are applied to the entries they correct, so they need no
    # destination of their own.
    corrections = [e for e in entries if e.section in ("Superseded / Historical", "Manual Notes")]
    (work / "labels.corrections.jsonl").write_text(
        "".join(json.dumps({"n": e.n, "to": "archive", "why": "correction, applied where it belongs"}) + "\n" for e in corrections),
        encoding="utf-8",
    )
    print(f"{len(corrections):>5} entries  labels.corrections.jsonl (archive)")


# ---------------------------------------------------------------------------
# logs: shared helpers


NUM = r"\d[\d,]*(?:\.\d+)?"
MONTH_NAMES = "jan feb mar apr may jun jul aug sep oct nov dec".split()
MONTH_RE = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
COMPARISON_RE = re.compile(
    r"(?:down|up)?\s*\b(?:from|vs\.?|versus|compared (?:to|with)|matching|replacing)\b(?:[^.;()]|\.(?=\d))*",
    re.I,
)
APPROX_RE = re.compile(r"\b(?:approximately|approx\.?|roughly|about|around|nearly|just below|just under)\b|~", re.I)


def to_float(text: str) -> float:
    return float(text.replace(",", ""))


def suffix_value(line: str) -> tuple[float | None, float | None, str | None]:
    """Value in the trailing "(80.5 kg)" tag: (value, upper bound of a range, unit)."""
    m = re.search(rf"\(\s*(-?{NUM})(?:\s*[–-]\s*({NUM}))?\s*([^()]*)\)\s*$", line)
    if not m:
        return None, None, None
    try:
        return to_float(m.group(1)), (to_float(m.group(2)) if m.group(2) else None), m.group(3).strip() or None
    except ValueError:
        return None, None, None


def other_date_mentioned(text: str, entry_date: str) -> str | None:
    """A date in the sentence that differs from the entry's own date.

    Comparisons ("down from 84.45 kg on July 14") are ignored: they describe
    another reading, not this one.
    """
    stripped = COMPARISON_RE.sub(" ", text)
    year, month, day = (int(x) for x in entry_date.split("-"))
    mentions = [
        (MONTH_NAMES.index(m.group(1)[:3].lower()) + 1, int(m.group(2)), m.group(0))
        for m in re.finditer(rf"\b({MONTH_RE})\s+(\d{{1,2}})\b(?!\s*(?:kg|cm|g\b|reps|sets|%))", text, re.I)
    ]
    if any((mon, d) == (month, day) for mon, d, _ in mentions):
        return None
    for m in re.finditer(rf"\b({MONTH_RE})\s+(\d{{1,2}})\b(?!\s*(?:kg|cm|g\b|reps|sets|%))", stripped, re.I):
        mon = MONTH_NAMES.index(m.group(1)[:3].lower()) + 1
        if (mon, int(m.group(2))) != (month, day):
            return m.group(0)
    for m in re.finditer(rf"\b(?:in|during|since|of)\s+(?:late |early |mid[- ]?)?({MONTH_RE})\b(?!\s+\d{{1,2}}\b)", stripped, re.I):
        if MONTH_NAMES.index(m.group(1)[:3].lower()) + 1 != month:
            return m.group(0)
    m = re.search(r"\b(20\d\d)\b", stripped)
    if m and int(m.group(1)) != year:
        return m.group(1)
    return None


@dataclass
class Decision:
    n: int
    to: str  # "logs:metrics", "logs:food", "logs:events" or "archive"
    why: str = ""
    review: bool = False


class Collector:
    """Gathers log rows and the per-entry decisions."""

    def __init__(self) -> None:
        self.metrics: dict[tuple[str, str, str], dict] = {}
        self.metric_sources: dict[tuple[str, str, str], list[int]] = collections.defaultdict(list)
        self.conflicts: list[str] = []
        self.decisions: dict[int, Decision] = {}

    def decide(self, n: int, to: str, why: str = "", review: bool = False) -> None:
        self.decisions[n] = Decision(n, to, why, review)

    def add_metric(self, e: Entry, *, date: str, metric: str, value: float, unit: str, cond: str = "", note: str = "") -> None:
        key = (date, metric, cond)
        old = self.metrics.get(key)
        if old and abs(old["value"] - value) > 1e-9:
            self.conflicts.append(
                f"{date} {metric}{' [' + cond + ']' if cond else ''}: {old['value']} (L{self.metric_sources[key][-1]}) replaced by {value} (L{e.n})"
            )
        if old and note and not old.get("note"):
            pass
        self.metrics[key] = {
            "date": date,
            "metric": metric,
            "value": value,
            "unit": unit,
            "cond": cond,
            "note": note or (old or {}).get("note", ""),
        }
        self.metric_sources[key].append(e.n)
        self.decide(e.n, "logs:metrics")


# ---------------------------------------------------------------------------
# logs: weight


WEIGHT_NONSTANDARD = re.compile(r"gym scale|with clothes|clothes on|inbody|after eating|post-food|after food", re.I)
WEIGHT_RESTATEMENT = re.compile(r"\b(?:before|when|while) (?:starting|their|the)\b|\blowest\b.*\bwas\b|\breports? their\b", re.I)


def convert_weight(entries: list[Entry], out: Collector) -> None:
    for e in entries:
        value, upper, unit = suffix_value(e.line)
        text = e.text
        if value is None or upper is not None or (unit or "").lower() != "kg":
            out.decide(e.n, "archive", "no single kg value", review=True)
            continue
        first = re.search(rf"({NUM})\s*(?:kg|kilos?|kilograms?)\b", text, re.I)
        if not first or abs(to_float(first.group(1)) - value) > 1e-9:
            out.decide(e.n, "archive", "value is a change or comparison", review=True)
            continue
        other = other_date_mentioned(text, e.date)
        if other:
            out.decide(e.n, "archive", f"refers to another date ({other})", review=True)
            continue
        if WEIGHT_RESTATEMENT.search(text):
            out.decide(e.n, "archive", "restates an earlier reading", review=True)
            continue
        cond = "non-standard scale or conditions" if WEIGHT_NONSTANDARD.search(text) else ""
        before_value = text[: first.start()]
        note = "approximate" if APPROX_RE.search(before_value[-40:]) else ""
        out.add_metric(e, date=e.date, metric="weight", value=value, unit="kg", cond=cond, note=note)


# ---------------------------------------------------------------------------
# logs: tape measurements (found in the old "Measurements" subsection)


TAPE_SITES: tuple[tuple[str, str], ...] = (
    # (metric, pattern); checked in the order the words appear in the sentence
    ("belly", r"navel|belly|love handles?|widest (?:part|point)"),
    ("waist", r"narrowest|upper waist|waist"),
    ("pantline", r"pant[- ]?line|pants sit|lower abdomen"),
    ("hips", r"\bhips?\b"),
    ("neck", r"\bneck\b"),
    ("chest", r"\bchest\b"),
    ("shoulders", r"\bshoulders?\b"),
)
TAPE_SKIP = re.compile(r"\bratio\b|differential|\bgap\b|\blost\b|\bdrop\b|unit not stated|baseline measurements|run \d|readings run", re.I)
TAPE_COND = (
    (re.compile(r"post-workout|pumped|pump-inflated", re.I), "post-workout"),
    (re.compile(r"self-estimate|\bestimates\b", re.I), "self-estimate"),
)
INTAKE_WORDS = re.compile(r"kcal|calor|protein|intake|\bcarbs?\b", re.I)
LIFT_WORDS = re.compile(r"\breps?\b|\bsets?\b|bench|leg press|\brows?\b|raises?\b|extensions?\b|\bhang\b", re.I)


def first_site(text: str) -> str | None:
    best: tuple[int, str] | None = None
    for metric, pattern in TAPE_SITES:
        m = re.search(pattern, text, re.I)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), metric)
    return best[1] if best else None


def convert_tape(entries: list[Entry], out: Collector) -> list[Entry]:
    """Convert tape readings. Returns the entries that are daily intake totals."""
    intake: list[Entry] = []
    for e in entries:
        text = e.text
        if INTAKE_WORDS.search(text):
            intake.append(e)
            continue
        if LIFT_WORDS.search(text) and not re.search(r"\bcm\b|\d\s*cm", text):
            out.decide(e.n, "archive", "lift result filed under measurements", review=True)
            continue
        if re.search(r"\bkg\b|\d\s*kg", text) and not re.search(r"\d\s*cm", text):
            out.decide(e.n, "archive", "weight filed under measurements (weight log has it)")
            continue
        if not re.search(r"\d\s*cm", e.line):
            out.decide(e.n, "archive", "not a tape reading", review=True)
            continue
        other = other_date_mentioned(text, e.date)
        if other:
            out.decide(e.n, "archive", f"refers to another date ({other})", review=True)
            continue
        cond = next((label for pattern, label in TAPE_COND if pattern.search(text)), "")
        value, upper, unit = suffix_value(e.line)
        has_suffix = value is not None and upper is None and (unit or "").lower() == "cm" and value > 0
        in_text = [to_float(x) for x in re.findall(rf"({NUM})\s*cm", text)]
        pairs: list[tuple[str, float]] = []
        if has_suffix and in_text and abs(in_text[0] - value) < 1e-9:
            site = first_site(text)
            if site:
                pairs.append((site, value))
        elif TAPE_SKIP.search(text):
            out.decide(e.n, "archive", "derived from other readings")
            continue
        elif has_suffix:
            site = first_site(text)
            if site and any(abs(x - value) < 1e-9 for x in in_text):
                pairs.append((site, value))
        else:
            # Several readings in one sentence: "waist 83cm, belly 95.5cm, hips 103.5cm".
            for clause in re.split(r",|;|\band\b", text):
                m = re.search(rf"({NUM})\s*cm", clause)
                site = first_site(clause)
                if m and site:
                    pairs.append((site, to_float(m.group(1))))
        if not pairs:
            out.decide(e.n, "archive", "could not tell which site was measured", review=True)
            continue
        note = "approximate" if APPROX_RE.search(text) else ""
        for site, reading in pairs:
            out.add_metric(e, date=e.date, metric=site, value=reading, unit="cm", cond=cond, note=note)
    return intake


# ---------------------------------------------------------------------------
# logs: daily food totals


KCAL_RE = re.compile(rf"({NUM})(?:\s*[–-]\s*({NUM}))?\s*(?:kcal|calories|cals?\b)", re.I)
PROTEIN_RE = re.compile(rf"({NUM})(?:\s*[–-]\s*({NUM}))?\s*(?:g|grams?)\s*(?:of\s+)?(?:protein\b)", re.I)
PROTEIN_ALT_RE = re.compile(rf"protein[^.;]*?({NUM})(?:\s*[–-]\s*({NUM}))?\s*(?:g|grams?)\b", re.I)
INTAKE_SKIP = re.compile(
    r"\bremaining\b|\btarget\b|\bdeficit\b|\bmaintenance\b|\bburn(?:ed|s)?\b|\bbudget\b|\bleft for\b|\bper (?:100|serving)\b|\bprovides?\b"
    r"|\bproposed\b|\bplanned\b|\bwould\b",  # projections, not food eaten
    re.I,
)
INTAKE_RUNNING = re.compile(
    r"running|so far|subtotal|partial|through (?:lunch|breakfast|dinner)|after (?:logging|adding|the|breakfast|lunch)|"
    r"before dinner|not (?:a )?(?:confirmed )?final|not including|excluding|conditional|assuming",
    re.I,
)
INTAKE_TOTALISH = re.compile(r"total|intake|consumed|so far|running|today|daily|day's|for the day|yesterday", re.I)


def _mid(lo: str, hi: str | None) -> tuple[float, str]:
    low = to_float(lo)
    if hi:
        high = to_float(hi)
        return round((low + high) / 2, 1), f"range {lo}–{hi}"
    return low, ""


def convert_intake(entries: list[Entry], out: Collector) -> dict[str, dict]:
    """Keep the best reported total of each day.

    A total stated as final (or as "yesterday's") beats the others. Otherwise
    the later line wins, because later lines are more complete; the day is
    marked as not closed when that line is a running total.
    """
    days: dict[str, dict] = {}
    for e in entries:
        text = e.text
        if INTAKE_SKIP.search(text) or not INTAKE_TOTALISH.search(text):
            out.decide(e.n, "archive", "not a day total", review=not INTAKE_SKIP.search(text))
            continue
        kcal_m = KCAL_RE.search(text)
        protein_m = PROTEIN_RE.search(text) or PROTEIN_ALT_RE.search(text)
        if not kcal_m and not protein_m:
            out.decide(e.n, "archive", "no calories or protein value", review=True)
            continue
        day = e.date
        if re.search(r"\byesterday", text, re.I):
            day = (date_cls.fromisoformat(day) - timedelta(days=1)).isoformat()
        stated_final = bool(
            re.search(r"\byesterday|(?<!not a )(?<!not )\bfinal (?:daily |day )?(?:intake|total)\b", text, re.I)
        )
        running = bool(INTAKE_RUNNING.search(text)) and not stated_final
        rank = 2 if stated_final else 1
        slot = days.setdefault(day, {"date": day, "kcal": None, "protein": None, "kcal_note": "", "protein_note": "", "src": [], "kcal_running": None, "protein_running": None, "kcal_rank": -1, "protein_rank": -1})
        if kcal_m and rank >= slot["kcal_rank"]:
            slot["kcal"], note = _mid(kcal_m.group(1), kcal_m.group(2))
            slot["kcal_running"], slot["kcal_rank"] = running, rank
            slot["kcal_note"] = f"calories {note}" if note else ""
        if protein_m and rank >= slot["protein_rank"]:
            slot["protein"], note = _mid(protein_m.group(1), protein_m.group(2))
            slot["protein_running"], slot["protein_rank"] = running, rank
            slot["protein_note"] = f"protein {note}" if note else ""
        slot["src"].append(e.n)
        out.decide(e.n, "logs:food")
    return days


# ---------------------------------------------------------------------------
# logs: sleep and events


def convert_sleep(entries: list[Entry], out: Collector) -> None:
    for e in entries:
        value, upper, unit = suffix_value(e.line)
        if value is not None and upper is None and (unit or "").lower().startswith("hour"):
            out.add_metric(e, date=e.date, metric="sleep", value=value, unit="hours")
        else:
            out.decide(e.n, "archive", "no hours value")


def convert_events(entries: list[Entry]) -> list[dict]:
    rows = []
    for e in entries:
        rows.append({"date": e.date, "kind": e.category or "event", "text": e.text, "src": f"migration:L{e.n}"})
    return rows


# ---------------------------------------------------------------------------
# logs: command


def _read_overrides(work: Path) -> dict[int, dict]:
    path = work / "overrides.jsonl"
    if not path.exists():
        return {}
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return {row["n"]: row for row in rows}


def _apply_overrides(overrides: dict[int, dict], out: Collector, by_n: dict[int, Entry]) -> None:
    """Hand corrections, for entries the rules were not allowed to touch."""
    for n, o in overrides.items():
        e = by_n[n]
        if o.get("skip"):
            out.decide(e.n, "archive", o.get("why", "override"))
        else:
            out.add_metric(
                e,
                date=o.get("date", e.date),
                metric=o["metric"],
                value=o["value"],
                unit=o["unit"],
                cond=o.get("cond", ""),
                note=o.get("note", ""),
            )


def cmd_logs(args: argparse.Namespace) -> None:
    entries = parse_entries(Path(args.source).read_text(encoding="utf-8"))
    by_n = {e.n: e for e in entries}
    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)
    out = Collector()

    overrides = _read_overrides(work)
    by_sub = collections.defaultdict(list)
    for e in entries:
        if e.section == "Time Series" and e.n not in overrides:
            by_sub[e.sub].append(e)

    convert_weight(by_sub["Weight"], out)
    intake_entries = convert_tape(by_sub["Measurements"], out)
    convert_sleep(by_sub["Sleep"], out)
    days = convert_intake(by_sub["Nutrition"] + intake_entries, out)
    for name in by_sub:
        if name not in {"Weight", "Measurements", "Sleep", "Nutrition", "Workouts"}:
            for e in by_sub[name]:
                out.decide(e.n, "archive", f"no rule for the {name} series", review=True)
    _apply_overrides(overrides, out, by_n)

    stamp = config.local_now().isoformat(timespec="seconds")

    def finish(rows: list[dict]) -> list[dict]:
        for i, row in enumerate(rows):
            row.setdefault("id", f"m{i:04d}")
            row["added"] = stamp
        return rows

    metric_rows = finish([
        {**row, "src": "migration:" + ",".join(f"L{n}" for n in sorted(set(out.metric_sources[key])))}
        for key, row in sorted(out.metrics.items())
    ])
    food_rows = finish([
        {
            "date": d["date"],
            "time": "",
            "kind": "day_total",
            "kcal": d["kcal"],
            "protein": d["protein"],
            "final": not (d["kcal_running"] or d["protein_running"]),
            "note": "; ".join(x for x in (d["kcal_note"], d["protein_note"]) if x),
            "src": "migration:" + ",".join(f"L{n}" for n in d["src"]),
        }
        for d in sorted(days.values(), key=lambda d: d["date"])
    ])
    for row in food_rows:
        row["id"] = "d" + row["date"].replace("-", "")
    event_rows = finish(convert_events([e for e in entries if e.section == "Timeline"]))
    for i, row in enumerate(event_rows):
        row["id"] = f"e{i:04d}"

    ledger.write_log(args.slug, ledger.METRICS, metric_rows)
    ledger.write_log(args.slug, ledger.FOOD, food_rows)
    ledger.write_log(args.slug, ledger.EVENTS, event_rows)

    decisions = sorted(out.decisions.values(), key=lambda d: d.n)
    for e in entries:
        if e.section == "Timeline":
            decisions.append(Decision(e.n, "logs:events"))
    (work / "labels.timeseries.jsonl").write_text(
        "".join(json.dumps({"n": d.n, "to": d.to, **({"why": d.why} if d.why else {})}) + "\n" for d in decisions),
        encoding="utf-8",
    )
    review = [d for d in decisions if d.review]
    (work / "review.timeseries.txt").write_text(
        "".join(f"L{d.n} [{d.why}] {by_n[d.n].line}\n" for d in review)
        + "\nConflicts (later line kept):\n"
        + "".join(c + "\n" for c in out.conflicts),
        encoding="utf-8",
    )

    per_metric = collections.Counter(r["metric"] + (" (non-standard)" if r["cond"] else "") for r in metric_rows)
    print(f"logs written under {ledger.agent_dir(args.slug) / 'logs'}")
    print(f"  metrics: {len(metric_rows)} rows  {dict(sorted(per_metric.items()))}")
    final_days = sum(1 for r in food_rows if r["final"])
    print(f"  food:    {len(food_rows)} day totals ({final_days} final, {len(food_rows) - final_days} running)")
    print(f"  events:  {len(event_rows)} rows")
    print(f"  overrides applied: {len(overrides)}; conflicts: {len(out.conflicts)}; entries for review: {len(review)}")
    print(f"  see {work / 'review.timeseries.txt'}")


# ---------------------------------------------------------------------------
# merge: rows extracted from free text (lifts, readings in short-lived notes,
# food reference entries). They are checked against the lines they cite
# before they are merged into the stores.


class Sources:
    """Looks up the text behind a source id.

    `L<n>` is line n of the old memory file, `R<n>` is line n of the expired
    notes file, `C<n>` is line n of the current notes file. Anything else
    (for example "audit 2026-11-10") is a hand-supplied source and is not
    checked.
    """

    def __init__(self, source: Path) -> None:
        self.md = source.read_text(encoding="utf-8").splitlines()
        stem = source.name[: -len(".md")] if source.name.endswith(".md") else source.name
        pruned = source.with_name(f"{stem}.recent.pruned.jsonl")
        recent = source.with_name(f"{stem}.recent.md")
        self.pruned = pruned.read_text(encoding="utf-8").splitlines() if pruned.exists() else []
        self.recent = recent.read_text(encoding="utf-8").splitlines() if recent.exists() else []

    def text(self, source_id: object) -> str | None:
        sid = str(source_id)
        if sid.isdigit():
            # A bare number is a line of the old memory file; 0 marks a row
            # supplied by hand, which has no line to check against.
            if int(sid) == 0:
                return None
            sid = f"L{sid}"
        m = re.fullmatch(r"([LRC])(\d+)", sid)
        if not m:
            return None
        lines = {"L": self.md, "R": self.pruned, "C": self.recent}[m.group(1)]
        n = int(m.group(2))
        if not 0 < n <= len(lines):
            return ""
        text = [lines[n - 1]]
        if m.group(1) == "L":
            # An old-memory entry includes the indented notes under it
            # (adoptions and corrections).
            for follow in lines[n:]:
                if follow.startswith("  ") and follow.strip():
                    text.append(follow)
                elif follow.strip():
                    break
        return "\n".join(text)


def _number_forms(value: float) -> list[str]:
    if float(value).is_integer():
        return sorted({str(int(value)), f"{int(value):,}", f"{int(value)}.0"})
    return sorted({f"{value:g}", f"{value:.2f}".rstrip("0")})


def numbers_missing_from_sources(numbers: list[float], src_ids: list, sources: Sources) -> list[float]:
    """Numbers that do not appear in any cited line (empty when all are backed)."""
    texts = [sources.text(i) for i in src_ids]
    if not src_ids or any(t is None for t in texts):
        return []  # hand-supplied source
    joined = " ".join(t or "" for t in texts)
    missing = []
    for value in numbers:
        if value is None:
            continue
        if value == 0 and re.search(r"\bzero\b|\bno (?:calories|protein)\b", joined, re.I):
            continue
        if not any(re.search(rf"(?<![\d.,]){re.escape(form)}(?![\d]|[.,]\d)", joined) for form in _number_forms(value)):
            missing.append(value)
    return missing


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _src_label(src_ids: list) -> str:
    return "migration:" + ",".join(str(i) if re.fullmatch(r"[LRC]\d+", str(i)) else str(i) for i in src_ids)


def cmd_merge(args: argparse.Namespace) -> None:
    work = Path(args.work)
    sources = Sources(Path(args.source))
    stamp = config.local_now().isoformat(timespec="seconds")
    problems: list[str] = []

    # Readings found outside the old time series. They never replace a
    # reading the time series already has.
    metrics = ledger.read_log(args.slug, ledger.METRICS)
    have = {(r["date"], r["metric"], r.get("cond", "")): r for r in metrics}
    added_metrics = 0
    for path in sorted(work.glob("metrics.*.jsonl")):
        for row in _read_jsonl(path):
            src_ids = row.get("src") or []
            missing = numbers_missing_from_sources([row["value"]], src_ids, sources)
            if missing:
                problems.append(f"{path.name}: {row['date']} {row['metric']} {row['value']} not found in {src_ids}")
                continue
            key = (row["date"], row["metric"], row.get("cond", ""))
            if key in have:
                if abs(have[key]["value"] - row["value"]) > 1e-9:
                    problems.append(
                        f"{path.name}: {row['date']} {row['metric']} {row['value']} differs from the logged {have[key]['value']} ({have[key]['src']}); kept the logged one"
                    )
                continue
            new = {
                "date": row["date"], "metric": row["metric"], "value": row["value"], "unit": row["unit"],
                "cond": row.get("cond", ""), "note": row.get("note", ""),
                "id": f"x{len(have):04d}", "src": _src_label(src_ids), "added": stamp,
            }
            have[key] = new
            added_metrics += 1
    ledger.write_log(args.slug, ledger.METRICS, have.values())

    # Lifts. Same exercise, load and day: the row with more sets wins.
    # `exercise_names.json` maps other spellings to one name per exercise.
    names_path = work / "exercise_names.json"
    names = json.loads(names_path.read_text(encoding="utf-8")) if names_path.exists() else {}
    lifts: dict[tuple, dict] = {}
    for path in sorted(work.glob("lifts*.jsonl")):
        for row in _read_jsonl(path):
            src_ids = row.get("src") or []
            numbers = ([row["load"]] if row.get("load") is not None else []) + list(row.get("reps") or [])
            missing = numbers_missing_from_sources(numbers, src_ids, sources)
            if missing:
                problems.append(f"{path.name}: {row['date']} {row['exercise']} numbers {missing} not found in {src_ids}")
                continue
            exercise = row["exercise"].strip().lower()
            key = (row["date"], names.get(exercise, exercise), row.get("load"))
            old = lifts.get(key)
            if old and old["reps"] != list(row.get("reps") or []):
                keep_new = len(row.get("reps") or []) > len(old["reps"])
                problems.append(
                    f"{path.name}: {row['date']} {row['exercise']} @ {row.get('load')}: reps {row.get('reps')} vs {old['reps']}; kept {'the new' if keep_new else 'the first'}"
                )
                if not keep_new:
                    continue
            elif old:
                continue
            lifts[key] = {
                "date": row["date"], "exercise": key[1], "load": row.get("load"), "unit": row.get("unit"),
                "reps": list(row.get("reps") or []), "note": row.get("note", ""),
                "src": _src_label(src_ids), "added": stamp,
            }
    # A row without a load duplicates a row for the same exercise and day
    # that has one.
    loaded = {(r["date"], r["exercise"]) for r in lifts.values() if r["load"] is not None}
    for key, row in list(lifts.items()):
        if row["load"] is None and (row["date"], row["exercise"]) in loaded:
            problems.append(f"lifts: {row['date']} {row['exercise']} without a load dropped; the day has a row with one")
            del lifts[key]
    lift_rows = sorted(lifts.values(), key=lambda r: (r["date"], r["exercise"]))
    for i, row in enumerate(lift_rows):
        row["id"] = f"l{i:04d}"
    ledger.write_log(args.slug, ledger.LIFTS, lift_rows)

    # Food reference. Same name: the most recently updated entry wins.
    foods: dict[str, dict] = {}
    for path in sorted(work.glob("foodbook.*.jsonl")):
        for row in _read_jsonl(path):
            src_ids = row.get("src") or []
            numbers = [v for v in (row.get("kcal"), row.get("protein_g")) if v is not None]
            missing = numbers_missing_from_sources(numbers, src_ids, sources)
            if missing:
                problems.append(f"{path.name}: {row['name']} numbers {missing} not found in {src_ids}")
                continue
            key = " ".join(row["name"].lower().split())
            old = foods.get(key)
            entry = {
                "name": row["name"].strip(), "aliases": row.get("aliases") or [], "serving": row.get("serving", ""),
                "kcal": row.get("kcal"), "protein_g": row.get("protein_g"), "era": row.get("era", "any"),
                "updated": row.get("updated", ""), "note": row.get("note", ""), "src": _src_label(src_ids),
            }
            if old:
                if (old["kcal"], old["protein_g"]) != (entry["kcal"], entry["protein_g"]):
                    problems.append(
                        f"{path.name}: {row['name']}: {entry['kcal']} kcal / {entry['protein_g']} g vs {old['kcal']} / {old['protein_g']}; kept the later one"
                    )
                if entry["updated"] < old["updated"]:
                    old["aliases"] = sorted(set(old["aliases"]) | set(entry["aliases"]))
                    continue
                entry["aliases"] = sorted(set(old["aliases"]) | set(entry["aliases"]))
            foods[key] = entry
    foodbook_path = ledger.agent_dir(args.slug) / "foodbook.jsonl"
    foodbook_path.parent.mkdir(parents=True, exist_ok=True)
    foodbook_path.write_text(
        "".join(json.dumps(f, ensure_ascii=False) + "\n" for f in sorted(foods.values(), key=lambda f: f["name"].lower())),
        encoding="utf-8",
    )

    (work / "review.merge.txt").write_text("".join(p + "\n" for p in problems), encoding="utf-8")
    exercises = collections.Counter(r["exercise"] for r in lift_rows)
    print(f"metrics: {added_metrics} readings added from notes ({len(have)} rows in all)")
    print(f"lifts:   {len(lift_rows)} rows, {len(exercises)} exercises, {len({r['date'] for r in lift_rows})} training days")
    print(f"foodbook: {len(foods)} entries -> {foodbook_path}")
    print(f"problems: {len(problems)} (see {work / 'review.merge.txt'})")


# ---------------------------------------------------------------------------
# verify: every number in a drafted card must appear in the lines it cites,
# or in the logs when the line cites the logs.

CITE_RE = re.compile(r"\((?:[LRC]?\d+)(?:,\s*[LRC]?\d+)*\)|\[(?:[LRC]\d+)(?:,\s*[LRC]\d+)*\]")
LOG_CITE_RE = re.compile(r"\((?:weight|tape|lift|food) log\)", re.I)
DATE_WORDS_RE = re.compile(
    rf"\b\d{{4}}-\d{{2}}-\d{{2}}\b"
    rf"|\b\d{{1,2}}(?:\s+{MONTH_RE})?\s+(?:to|and|–|-)\s+\d{{1,2}}\s+{MONTH_RE}"  # "5 to 11 Oct", "26 Oct to 1 Nov"
    rf"|\b\d{{1,2}}\s+{MONTH_RE}(?:\s+\d{{4}})?|\b{MONTH_RE}\s+\d{{1,2}}\b(?:,?\s+\d{{4}})?"
    rf"|\b\d{{1,2}}:\d{{2}}\b|\b20\d\d\b",
    re.I,
)
NUMBER_RE = re.compile(rf"(?<![\w.])(-?{NUM})(?![\d])")


def numbers_in(text: str) -> list[float]:
    cleaned = DATE_WORDS_RE.sub(" ", CITE_RE.sub(" ", text))
    cleaned = re.sub(r"\b(?:week|day|month|chapter|phase|set)\s+\d+\b", " ", cleaned, flags=re.I)
    out = []
    for m in NUMBER_RE.finditer(cleaned):
        try:
            out.append(to_float(m.group(1).lstrip("-")))
        except ValueError:
            pass
    return out


def cmd_verify(args: argparse.Namespace) -> None:
    sources = Sources(Path(args.source))
    metrics = ledger.read_log(args.slug, ledger.METRICS)
    lifts = ledger.read_log(args.slug, ledger.LIFTS)
    log_numbers = {float(r["value"]) for r in metrics}
    for row in lifts:
        if row.get("load") is not None:
            log_numbers.add(float(row["load"]))
        log_numbers.update(float(x) for x in row.get("reps") or [])
    problems = 0
    checked = 0
    for path in args.files:
        for lineno, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
            ids = [i for group in CITE_RE.findall(line) for i in re.findall(r"[LRC]?\d+", group)]
            uses_logs = bool(LOG_CITE_RE.search(line))
            nums = numbers_in(LOG_CITE_RE.sub(" ", line))
            if not nums:
                continue
            if not ids and not uses_logs:
                if args.strict:
                    print(f"{path}:{lineno}: numbers without a source: {line.strip()[:140]}")
                    problems += 1
                continue
            checked += 1
            missing = numbers_missing_from_sources(nums, ids, sources) if ids else nums
            if uses_logs:
                missing = [n for n in missing if not any(abs(n - v) < 1e-9 for v in log_numbers)]
            if missing:
                problems += 1
                shown = ", ".join(f"{n:g}" for n in missing)
                print(f"{path}:{lineno}: not found in its sources: {shown}\n    {line.strip()[:220]}")
    print(f"checked {checked} cited lines; {problems} with problems")


# ---------------------------------------------------------------------------
# delta: what the live bot added after the freeze, to carry over at switch time.


def cmd_delta(args: argparse.Namespace) -> None:
    """List entries and short-lived notes that are new since the freeze.

    Line numbers shift as the old file grows, so entries are matched by
    their text, not their ids.
    """
    old = {e.line for e in parse_entries(Path(args.source).read_text(encoding="utf-8"))}
    new_entries = [e for e in parse_entries(Path(args.current).read_text(encoding="utf-8")) if e.line not in old]
    out: list[str] = [f"# New since the freeze: {len(new_entries)} entries", ""]
    for e in new_entries:
        where = e.section if not e.sub else f"{e.section} / {e.sub}"
        out.append(f"- [{where}] {e.line[2:]}")
    current_notes = Path(args.current).with_name(Path(args.current).name.replace(".md", ".recent.md"))
    frozen_notes = Path(args.source).with_name(Path(args.source).name.replace(".md", ".recent.md"))
    if current_notes.exists():
        frozen = set(frozen_notes.read_text(encoding="utf-8").splitlines()) if frozen_notes.exists() else set()
        notes = [line for line in current_notes.read_text(encoding="utf-8").splitlines()
                 if line.startswith("- [") and line not in frozen]
        out += ["", f"# New short-lived notes: {len(notes)}", ""] + notes
    print("\n".join(out))


# ---------------------------------------------------------------------------
# report


def cmd_report(args: argparse.Namespace) -> None:
    entries = parse_entries(Path(args.source).read_text(encoding="utf-8"))
    work = Path(args.work)
    labels: dict[int, str] = {}
    duplicates: list[int] = []
    paths = sorted(work.glob("labels.*.jsonl"), key=lambda p: (p.name == "labels.overrides.jsonl", p.name))
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            # labels.overrides.jsonl holds hand corrections and is read last.
            if row["n"] in labels and path.name != "labels.overrides.jsonl":
                duplicates.append(row["n"])
            labels[row["n"]] = "logs:lifts" if row["to"] == "lifts" else row["to"]
    missing = [e for e in entries if e.n not in labels]
    unknown = sorted(set(labels) - {e.n for e in entries})
    table: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for e in entries:
        section = e.section if not e.sub else f"{e.section} / {e.sub}"
        table[section][labels.get(e.n, "(unsorted)")] += 1
    columns = sorted({to for counts in table.values() for to in counts})
    lines = ["# Where every old memory entry went", "", f"Source: `{args.source}` ({len(entries)} entries).", ""]
    lines.append("| Section | " + " | ".join(columns) + " | Total |")
    lines.append("| --- | " + " | ".join("---:" for _ in columns) + " | ---: |")
    for section, counts in table.items():
        lines.append(f"| {section} | " + " | ".join(str(counts.get(c, 0)) for c in columns) + f" | {sum(counts.values())} |")
    totals = collections.Counter()
    for counts in table.values():
        totals.update(counts)
    lines.append("| **All** | " + " | ".join(str(totals.get(c, 0)) for c in columns) + f" | {sum(totals.values())} |")
    lines += ["", f"Unsorted entries: {len(missing)}. Labels for unknown ids: {len(unknown)}. Entries labelled twice: {len(set(duplicates))}."]
    if missing:
        lines += ["", "## Unsorted", ""] + [f"- L{e.n} {e.line[:160]}" for e in missing[:200]]
    out_path = work / "coverage.md"
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[: len(table) + 9]))
    print(f"\nwritten to {out_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name, fn in (("split", cmd_split), ("logs", cmd_logs), ("merge", cmd_merge), ("verify", cmd_verify),
                     ("delta", cmd_delta), ("report", cmd_report)):
        p = sub.add_parser(name)
        p.add_argument("--source", required=True, help="frozen copy of the old memory file")
        if name not in ("verify", "delta"):
            p.add_argument("--work", required=True, help="directory for working files")
        if name == "delta":
            p.add_argument("--current", required=True, help="the old memory file as it is now")
        if name in ("logs", "merge", "verify"):
            p.add_argument("--slug", required=True, help="agent slug; logs go to data/<slug>/logs/")
        if name == "verify":
            p.add_argument("files", nargs="+", help="drafted cards to check")
            p.add_argument("--strict", action="store_true", help="also flag numbers with no source at all")
        p.set_defaults(fn=fn)
    args = parser.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
