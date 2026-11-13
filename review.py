"""Weekly review for memory v2.

Runs once a week (Monday noon by default), on /review, and at startup when a
scheduled run was missed:

- writes the last finished phase week into the open chapter of the story,
  one line computed from the logs, and refreshes the chapter's "So far" line;
- keeps the story within its size limit: old week lines fold a month at a
  time into "Weeks A to B" lines, and when space is still short the two
  oldest folded lines merge into one, so the chapter stays bounded however
  long the phase runs. Every folded line is recomputed from the logs. Week
  lines the user approved (phase.json "protect_through_week") never change;
- returns a short check-in for the user: the week's numbers, the pivot
  rules checked against the readings, cards close to their size limit, and a
  reminder when today's weigh-in or tape is missing.

Everything here is arithmetic over the logs; no model call is involved.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import date as date_cls, datetime, timedelta
from pathlib import Path
from typing import Any

import brief
import cards
import ledger
import memory_v2

log = logging.getLogger(__name__)

KEEP_WEEK_LINES = 4
MAX_LISTED_READINGS = 2
MAX_EVENTS = 3
CARD_WARN_SHARE = 0.9
TAPE_NAMES = (("belly", "navel"), ("waist", "waist"), ("shoulders", "shoulders"), ("chest", "chest"), ("hips", "hips"))
KEY_LIFT = "bench press"


# ---------------------------------------------------------------------------
# Weeks.


def last_finished_week(today: str, phase_start: str) -> tuple[int, str, str] | None:
    """(number, first day, last day) of the latest phase week that has ended.

    Phase weeks run from the phase start in 7-day blocks, whatever the
    weekday, so a review on any day refers to the same week as on Monday.
    """
    start = date_cls.fromisoformat(phase_start)
    number = (date_cls.fromisoformat(today) - start).days // 7
    if number < 1:
        return None
    first = start + timedelta(days=7 * (number - 1))
    return number, first.isoformat(), (first + timedelta(days=6)).isoformat()


def week_dates(number: int, phase_start: str) -> tuple[str, str]:
    first = date_cls.fromisoformat(phase_start) + timedelta(days=7 * (number - 1))
    return first.isoformat(), (first + timedelta(days=6)).isoformat()


# ---------------------------------------------------------------------------
# Numbers of a period, as short phrases.


def _readings_phrase(label: str, readings: list[tuple[str, float]], unit: str, *, today: str, compact: bool) -> str:
    if len(readings) <= MAX_LISTED_READINGS and not compact:
        shown = ", ".join(f"{brief.fmt_num(v)} {unit} on {brief.fmt_day(d, today=today)}" for d, v in readings)
        return f"{label} {shown}"
    first, last = readings[0][1], readings[-1][1]
    if len(readings) == 1:
        return f"{label} {brief.fmt_num(first)} {unit}"
    low = min(v for _, v in readings)
    text = f"{label} {brief.fmt_num(first)} to {brief.fmt_num(last)} {unit} over {len(readings)} readings"
    return text + (f" (low {brief.fmt_num(low)})" if low < min(first, last) else "")


def _new_bests(lifts: list[dict], start: str, end: str) -> list[str]:
    """Exercises whose best set in the period beats every earlier one."""
    found = []
    for name in sorted({r["exercise"] for r in lifts if start <= r["date"] <= end}):
        before = ledger.best_set([r for r in lifts if r["date"] < start], name)
        during = ledger.best_set([r for r in lifts if start <= r["date"] <= end], name)
        if not during or not before:
            continue
        if (during["load"], max(during["reps"])) > (before["load"], max(before["reps"])):
            unit = f" {during['unit']}" if during.get("unit") else ""
            found.append(f"{name} {brief.fmt_num(during['load'])}{unit} × {max(during['reps'])}")
    return found


def period_parts(agent_slug: str, start: str, end: str, *, today: str, compact: bool = False) -> list[str]:
    metrics = ledger.read_log(agent_slug, ledger.METRICS)
    lifts = ledger.read_log(agent_slug, ledger.LIFTS)
    food = ledger.read_log(agent_slug, ledger.FOOD)
    events = ledger.read_log(agent_slug, ledger.EVENTS)
    parts = []
    for metric, label in (("weight", "weight"), *TAPE_NAMES):
        readings = [(d, v) for d, v in ledger.metric_series(metrics, metric) if start <= d <= end]
        if readings:
            parts.append(_readings_phrase(label, readings, "kg" if metric == "weight" else "cm",
                                          today=today, compact=compact))
    if not parts:
        parts.append("no weigh-in or tape")
    bench = [r for r in lifts if r["exercise"] == KEY_LIFT and start <= r["date"] <= end]
    bench_days = sorted({r["date"] for r in bench})
    if bench and (compact or len(bench_days) > 2):
        best = ledger.best_set(bench, KEY_LIFT)
        if best:
            parts.append(f"bench best {brief._sets_text({**best, 'reps': [max(best['reps'])]})} over {len(bench_days)} sessions")
    elif bench:
        parts.append("bench " + "; ".join(
            f"{'; '.join(brief._sets_text(r) for r in bench if r['date'] == d)} on {brief.fmt_day(d, today=today)}"
            for d in bench_days
        ))
    bests = _new_bests(lifts, start, end)
    if bests:
        more = f" and {len(bests) - 3} more" if len(bests) > 3 else ""
        parts.append("new bests: " + ", ".join(bests[:3]) + more)
    days_trained = len({r["date"] for r in lifts if start <= r["date"] <= end})
    if days_trained:
        parts.append(f"{days_trained} training day{'s' if days_trained != 1 else ''} logged")
    intake = ledger.intake_average(food, start=start, end=min(end, today))
    if intake["days_logged"]:
        parts.append(
            f"food about {brief.fmt_num(intake['kcal'], 0)} kcal and {brief.fmt_num(intake['protein'], 0)} g protein a day "
            f"over {intake['days_logged']} logged day{'s' if intake['days_logged'] != 1 else ''}"
        )
    happened = [e for e in events if start <= e["date"] <= end]
    for e in happened[:MAX_EVENTS]:
        text = e["text"].rstrip(".")
        if compact and len(text) > 90:
            text = text[:90].rstrip() + "…"
        parts.append(f"{brief.fmt_day(e['date'], today=today)}: {text}")
    if len(happened) > MAX_EVENTS:
        parts.append(f"{len(happened) - MAX_EVENTS} more events")
    return parts


def week_line(agent_slug: str, number: int, start: str, end: str, *, today: str) -> str:
    parts = period_parts(agent_slug, start, end, today=today)
    return f"- Week {number}, {brief.fmt_day(start, today=today)} to {brief.fmt_day(end, today=today)}: " + "; ".join(parts) + "."


def folded_line(agent_slug: str, first: int, last: int, phase_start: str, *, today: str) -> str:
    """One short line for weeks `first` to `last` folded together."""
    start, end = week_dates(first, phase_start)[0], week_dates(last, phase_start)[1]
    parts = period_parts(agent_slug, start, end, today=today, compact=True)
    return (f"- Weeks {first} to {last}, {brief.fmt_day(start, today=today)} to {brief.fmt_day(end, today=today)}: "
            + "; ".join(parts) + ".")


def so_far_line(agent_slug: str, phase_start: str, *, today: str) -> str:
    metrics = ledger.read_log(agent_slug, ledger.METRICS)
    parts = []
    weight = ledger.metric_summary(metrics, "weight", today=today, since=phase_start)
    if weight and "since" in weight:
        parts.append(
            f"weight {brief.fmt_signed(weight['change_since'])} kg since the start "
            f"({brief.fmt_num(weight['since'][1])} to {brief.fmt_num(weight['latest'])} kg)"
        )
    navel = [v for d, v in ledger.metric_series(metrics, "belly") if d >= phase_start]
    if navel:
        parts.append(f"navel between {brief.fmt_num(min(navel))} and {brief.fmt_num(max(navel))} cm")
    best = ledger.best_set([r for r in ledger.read_log(agent_slug, ledger.LIFTS) if r["date"] >= phase_start], KEY_LIFT)
    if best:
        parts.append(f"best bench {brief._sets_text({**best, 'reps': [max(best['reps'])]})}")
    return "- So far: " + ("; ".join(parts) if parts else "no readings yet") + "."


# ---------------------------------------------------------------------------
# Story editing.


_WEEK_RE = re.compile(r"^- Week (\d+),")
_WEEKS_RE = re.compile(r"^- Weeks (\d+) to (\d+),")


def _span(line: str) -> tuple[int, int] | None:
    """(first week, last week) a week or folded line covers."""
    if m := _WEEK_RE.match(line):
        return int(m.group(1)), int(m.group(1))
    if m := _WEEKS_RE.match(line):
        return int(m.group(1)), int(m.group(2))
    return None


def _open_chapter(lines: list[str]) -> tuple[int, int] | None:
    starts = [i for i, line in enumerate(lines) if line.startswith("## ") and "(open)" in line]
    if not starts:
        return None
    start = starts[-1]
    end = next((j for j in range(start + 1, len(lines)) if lines[j].startswith("## ")), len(lines))
    return start, end


def update_story(story: str, line: str, so_far: str, *, protect_through: int = 0) -> str:
    """Put a week line and the "So far" line into the open chapter.

    A protected week, or a week already folded into a longer line, keeps
    its text; only the "So far" line changes. Raises ValueError when the
    story has no open chapter.
    """
    lines = story.rstrip().splitlines()
    bounds = _open_chapter(lines)
    if bounds is None:
        raise ValueError("the story has no open chapter")
    start, end = bounds
    number = _span(line)[0]
    rows = [(i, _span(lines[i])) for i in range(start, end) if _span(lines[i])]
    covering = [i for i, (a, b) in rows if a <= number <= b]
    if covering:
        i = covering[0]
        if _WEEK_RE.match(lines[i]) and number > protect_through:
            lines[i] = line
    else:
        later = [i for i, (a, _) in rows if a > number]
        at = later[0] if later else (rows[-1][0] + 1 if rows else end)
        lines.insert(at, line)
    start, end = _open_chapter(lines)
    so_far_rows = [i for i in range(start, end) if lines[i].startswith("- So far:")]
    if so_far_rows:
        lines[so_far_rows[0]] = so_far
    else:
        last = max((i for i in range(start, end) if lines[i].startswith("- ")), default=start)
        lines.insert(last + 1, so_far)
    return "\n".join(lines) + "\n"


def condense_story(agent_slug: str, story: str, *, today: str, limit: int, phase_start: str,
                   protect_through: int = 0) -> str:
    """Fold lines of the open chapter until the story fits.

    In order of preference, repeated until it fits:
    1. fold a calendar month (by each week's first day) once all its weeks
       are older than the latest KEEP_WEEK_LINES;
    2. merge the two oldest folded lines, so older history gets coarser;
    3. fold the oldest remaining old week into the folded line before it;
    4. move the oldest closed chapter in full to the archive, leaving its
       summary (see `archive_oldest_chapter`).
    Protected weeks never fold. Every new line is recomputed from the logs.
    """
    while len(story) > limit:
        lines = story.rstrip().splitlines()
        bounds = _open_chapter(lines)
        if bounds is None:
            archived = archive_oldest_chapter(agent_slug, story, today=today)
            if archived is None:
                break
            story = archived
            continue
        start, end = bounds
        singles = [i for i in range(start, end) if _WEEK_RE.match(lines[i])]
        recent = set(singles[-KEEP_WEEK_LINES:])
        eligible = [i for i in singles if i not in recent and _span(lines[i])[0] > protect_through]
        folded = [i for i in range(start, end) if _WEEKS_RE.match(lines[i])]

        def month_of(i: int) -> tuple[int, int]:
            first = date_cls.fromisoformat(week_dates(_span(lines[i])[0], phase_start)[0])
            return first.year, first.month

        recent_months = {month_of(i) for i in recent}
        complete = [i for i in eligible if month_of(i) not in recent_months]
        if complete:
            group = [i for i in complete if month_of(i) == month_of(complete[0])]
        elif len(folded) >= 2:
            group = folded[:2]
        elif eligible:
            before = [i for i in folded if i < eligible[0]]
            group = [before[-1], eligible[0]] if before else [eligible[0]]
        else:
            archived = archive_oldest_chapter(agent_slug, story, today=today)
            if archived is None:
                break
            story = archived
            continue
        first, last = _span(lines[group[0]])[0], _span(lines[group[-1]])[1]
        new_line = folded_line(agent_slug, first, last, phase_start, today=today)
        for i in reversed(group):
            del lines[i]
        lines.insert(group[0], new_line)
        new_story = "\n".join(lines) + "\n"
        if new_story == story:
            break
        story = new_story
    return story


def fold_all_weeks(agent_slug: str, story: str, *, phase_start: str, protect_through: int, today: str) -> str:
    """Fold every unprotected week line of the open chapter, a calendar month at a time.

    Used when a chapter closes: its weeks no longer need weekly detail.
    """
    lines = story.rstrip().splitlines()
    bounds = _open_chapter(lines)
    if bounds is None:
        return story
    start, end = bounds
    singles = [i for i in range(start, end) if _WEEK_RE.match(lines[i]) and _span(lines[i])[0] > protect_through]

    def month_of(i: int) -> tuple[int, int]:
        first = date_cls.fromisoformat(week_dates(_span(lines[i])[0], phase_start)[0])
        return first.year, first.month

    groups: list[list[int]] = []
    for i in singles:
        if groups and month_of(groups[-1][0]) == month_of(i):
            groups[-1].append(i)
        else:
            groups.append([i])
    for group in reversed(groups):
        first, last = _span(lines[group[0]])[0], _span(lines[group[-1]])[1]
        new_line = folded_line(agent_slug, first, last, phase_start, today=today)
        for i in reversed(group):
            del lines[i]
        lines.insert(group[0], new_line)
    return "\n".join(lines) + "\n"


ARCHIVED_NOTE = "- The full chapter is in the archive."
_MONTHS = r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"


def _chapter_range(heading: str) -> tuple[str, str] | None:
    """Dates from a closed chapter heading such as "14 March to 28 September 2026"."""
    m = re.search(rf"(\d{{1,2}}) ({_MONTHS})(?: (\d{{4}}))? to (\d{{1,2}}) ({_MONTHS}) (\d{{4}})", heading)
    if not m:
        return None
    end = datetime.strptime(f"{m.group(4)} {m.group(5)} {m.group(6)}", "%d %B %Y").date()
    year = int(m.group(3)) if m.group(3) else end.year
    start = datetime.strptime(f"{m.group(1)} {m.group(2)} {year}", "%d %B %Y").date()
    if start > end:
        start = start.replace(year=start.year - 1)
    return start.isoformat(), end.isoformat()


def archive_oldest_chapter(agent_slug: str, story: str, *, today: str) -> str | None:
    """Move the oldest closed chapter in full to the archive, keeping its summary.

    Returns the shorter story, or None when every closed chapter is already
    archived. The archive copy is written once per chapter heading.
    """
    lines = story.rstrip().splitlines()
    for i, line in enumerate(lines):
        if not (line.startswith("## ") and "(closed)" in line):
            continue
        end = next((j for j in range(i + 1, len(lines)) if lines[j].startswith("## ")), len(lines))
        body = lines[i + 1:end]
        if ARCHIVED_NOTE in body:
            continue
        path = ledger.agent_dir(agent_slug) / "archive" / "story_chapters.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        if line not in existing:
            with path.open("a", encoding="utf-8") as f:
                f.write(f"\n<!-- archived {today} -->\n" + "\n".join(lines[i:end]).strip() + "\n")
        summary = next((b for b in body if b.startswith("- Summary")), None)
        if summary is None:
            span = _chapter_range(line)
            summary = (f"- Summary: " + "; ".join(period_parts(agent_slug, span[0], span[1], today=today, compact=True)) + "."
                       if span else "- Summary: see the archive.")
        new_body = ["", summary, ARCHIVED_NOTE, ""] if end < len(lines) else ["", summary, ARCHIVED_NOTE]
        return "\n".join(lines[: i + 1] + new_body + lines[end:]).rstrip() + "\n"
    return None


# ---------------------------------------------------------------------------
# Review state: which week was last reviewed, for catching up missed runs.


def _state_path(agent_slug: str) -> Path:
    return ledger.agent_dir(agent_slug) / "cards" / "review_state.json"


def last_reviewed_week(agent_slug: str) -> int | None:
    path = _state_path(agent_slug)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8")).get("last_week")


def mark_reviewed(agent_slug: str, number: int, today: str) -> None:
    path = _state_path(agent_slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"last_week": number, "date": today}) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def is_due(agent_slug: str, *, today: str) -> bool:
    """True when a finished week has not been reviewed yet.

    Without a state file nothing is due, so switching a bot on does not send
    a check-in at once; the first scheduled run or /review starts the record.
    """
    phase = memory_v2.read_phase(agent_slug)
    last = last_reviewed_week(agent_slug)
    if not phase or not phase.get("start") or last is None:
        return False
    finished = last_finished_week(today, phase["start"])
    return bool(finished and finished[0] > last)


# ---------------------------------------------------------------------------
# The weekly run.


def _card_warnings(agent_slug: str) -> list[str]:
    warnings = []
    for name, budget in cards.BUDGETS.items():
        size = len(cards.read_card(agent_slug, name))
        if size >= budget * CARD_WARN_SHARE:
            warnings.append(f"- Memory: the {name} card is {size * 100 // budget}% full; it needs condensing soon.")
    return warnings


def checkin_text(agent_slug: str, *, today: str) -> str:
    """The message sent to the user."""
    phase = memory_v2.read_phase(agent_slug) or {}
    finished = last_finished_week(today, phase["start"]) if phase.get("start") else None
    if finished:
        number, start, end = finished
        title = f"Week {number} check-in ({brief.fmt_day(start, today=today)} to {brief.fmt_day(end, today=today)})"
    else:
        end = (date_cls.fromisoformat(today) - timedelta(days=1)).isoformat()
        start = (date_cls.fromisoformat(today) - timedelta(days=7)).isoformat()
        title = f"Check-in for the last 7 days ({brief.fmt_day(start, today=today)} to {brief.fmt_day(end, today=today)})"
    lines = [title] + [f"- {p[0].upper()}{p[1:]}" for p in period_parts(agent_slug, start, end, today=today)]
    metrics = ledger.read_log(agent_slug, ledger.METRICS)
    summary = ledger.metric_summary(metrics, "weight", today=today, since=phase.get("start"))
    if summary and summary["rate_6w"] is not None:
        lines.append(f"- Weight trend: {brief.fmt_signed(summary['rate_6w'])} kg a week over 6 weeks.")
    for wire in cards.read_tripwires(agent_slug):
        result = cards.check_tripwire(wire, metrics, today=today)
        label = {"tripped": "TRIPPED", "clear": "clear", "unknown": "can't tell yet"}[result["status"]]
        line = f"- {wire['name']}: {label}; {result['detail']}."
        if result["status"] == "tripped":
            line += f" The rule says: {wire['then']}"
        lines.append(line)
    lines += _card_warnings(agent_slug)
    taken_today = [m for m in ("weight", "belly") if any(d == today for d, _ in ledger.metric_series(metrics, m))]
    if len(taken_today) < 2:
        missing = [label for m, label in (("weight", "weigh-in"), ("belly", "navel tape")) if m not in taken_today]
        lines.append(f"No {' or '.join(missing)} logged today yet. Send it when you can: the pivot rules need weekly readings.")
    return "\n".join(lines)


def run(agent_slug: str, *, today: str) -> dict[str, Any]:
    """Update the story for the last finished week and return the check-in text.

    Story problems (no open chapter, no room) are logged and never stop the
    check-in.
    """
    phase = memory_v2.read_phase(agent_slug)
    result: dict[str, Any] = {"story_updated": False}
    finished = last_finished_week(today, phase["start"]) if phase and phase.get("start") else None
    if finished:
        number, start, end = finished
        protect = int(phase.get("protect_through_week", 0))
        story = cards.read_card(agent_slug, cards.STORY)
        if story.strip():
            try:
                updated = update_story(story, week_line(agent_slug, number, start, end, today=today),
                                       so_far_line(agent_slug, phase["start"], today=today), protect_through=protect)
                updated = condense_story(agent_slug, updated, today=today, limit=cards.BUDGETS[cards.STORY],
                                         phase_start=phase["start"], protect_through=protect)
                cards.write_card(agent_slug, cards.STORY, updated)
                result["story_updated"] = True
            except ValueError as e:  # includes cards.CardTooLong
                log.warning("Weekly review: story not updated: %s", e)
        mark_reviewed(agent_slug, number, today)
    result["text"] = checkin_text(agent_slug, today=today)
    return result
