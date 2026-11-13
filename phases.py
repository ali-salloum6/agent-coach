"""Changing phase in memory v2 (a cut, a build, maintenance, a diet break).

A phase change is the user's decision, saved by the memory step
(`change_phase` in writer_v2.py). It can start today or on a date up to 30
days ahead; a future start is stored in phase.json as "next" and applied by
`apply_due` on that day.

Starting a phase:
- closes the open chapter of the story: its heading gets the date range,
  its "So far" line becomes a "Summary" line computed from the logs, and
  its unprotected week lines fold into month lines;
- opens the next chapter with the starting numbers from the logs and the
  agreed plan;
- rewrites the playbook's Phase section (the previous version stays in
  history) and leaves a short-lived note so the coach agrees new targets
  and pivot rules with the user instead of assuming the old ones;
- records the old phase in phase.json "history" and restarts the weekly
  review's week count.
"""

from __future__ import annotations

import logging
import re
from datetime import date as date_cls, timedelta
from typing import Any

import brief
import cards
import config
import ledger
import memory_template
import memory_v2
import review

log = logging.getLogger(__name__)

START_METRICS = (("weight", "weight", "kg"), ("belly", "navel", "cm"), ("waist", "waist", "cm"), ("shoulders", "shoulders", "cm"))
START_LOOKBACK_DAYS = 21


def long_day(iso: str) -> str:
    d = date_cls.fromisoformat(iso)
    return f"{d.day} {d.strftime('%B')} {d.year}"


def title_for(name: str) -> str:
    return name.strip().capitalize()


# ---------------------------------------------------------------------------
# Story lines computed from the logs.


def start_line(agent_slug: str, start: str, *, today: str) -> str:
    """The latest standard readings on or before the start, within 3 weeks."""
    metrics = ledger.read_log(agent_slug, ledger.METRICS)
    earliest = (date_cls.fromisoformat(start) - timedelta(days=START_LOOKBACK_DAYS)).isoformat()
    parts = []
    for metric, label, unit in START_METRICS:
        series = [(d, v) for d, v in ledger.metric_series(metrics, metric) if earliest <= d <= start]
        if series:
            d, v = series[-1]
            when = "" if d == start else f" ({brief.fmt_day(d, today=today)})"
            parts.append(f"{label} {brief.fmt_num(v)} {unit}{when}")
    facts = "; ".join(parts) if parts else "no recent readings"
    return f"- Start, {brief.fmt_day(start, today=today)}: {facts}."


def summary_line(agent_slug: str, start: str, end: str, *, today: str) -> str:
    weeks = ((date_cls.fromisoformat(end) - date_cls.fromisoformat(start)).days + 1) / 7
    parts = review.period_parts(agent_slug, start, end, today=today, compact=True)
    return (f"- Summary, {brief.fmt_day(start, today=today)} to {brief.fmt_day(end, today=today)} "
            f"(about {brief.fmt_num(weeks, 0)} weeks): " + "; ".join(parts) + ".")


# ---------------------------------------------------------------------------
# Story and playbook editing.


def _chapter_numbers(lines: list[str]) -> list[int]:
    return [int(m.group(1)) for line in lines if (m := re.match(r"^## Chapter (\d+):", line))]


def close_and_open(story: str, *, agent_slug: str, old: dict[str, Any], end: str,
                   new_title: str, new_start: str, plan: str, today: str) -> str:
    """Close the open chapter (if any) and append the next one."""
    story = review.fold_all_weeks(agent_slug, story, phase_start=old["start"],
                                  protect_through=int(old.get("protect_through_week", 0)), today=today)
    lines = story.rstrip().splitlines()
    bounds = review._open_chapter(lines)
    if bounds is not None:
        start, stop = bounds
        heading = lines[start]
        span = f"{long_day(old['start'])} to {long_day(end)} (closed)"
        m = re.match(r"^(## Chapter \d+: .*?),? since .*\(open\)\s*$", heading)
        lines[start] = f"{m.group(1)}, {span}" if m else heading.replace("(open)", span)
        summary = summary_line(agent_slug, old["start"], end, today=today)
        so_far = [i for i in range(start, stop) if lines[i].startswith("- So far:")]
        if so_far:
            lines[so_far[0]] = summary
        else:
            last = max((i for i in range(start, stop) if lines[i].startswith("- ")), default=start)
            lines.insert(last + 1, summary)
    number = max(_chapter_numbers(lines), default=0) + 1
    chapter = ["", f"## Chapter {number}: {new_title}, since {long_day(new_start)} (open)", "",
               start_line(agent_slug, new_start, today=today)]
    if plan:
        chapter.append(f"- Plan: {plan.rstrip('.')}.")
    chapter.append("- So far: no readings yet.")
    return "\n".join(lines + chapter) + "\n"


def replace_section_bullets(card: str, section: str, bullets: list[str]) -> str:
    """Replace a section's bullets, or add the section at the top when it is missing."""
    lines = card.rstrip().splitlines()
    wanted = " ".join(section.lower().split())
    heading = next((i for i, line in enumerate(lines)
                    if line.startswith("## ") and " ".join(line[3:].lower().split()) == wanted), None)
    body = [f"- {b.removeprefix('- ')}" for b in bullets]
    if heading is None:
        insert_at = next((i for i, line in enumerate(lines) if line.startswith("## ")), len(lines))
        new = lines[:insert_at] + [f"## {section}"] + body + [""] + lines[insert_at:]
        return "\n".join(new).rstrip() + "\n"
    end = next((j for j in range(heading + 1, len(lines)) if lines[j].startswith("## ")), len(lines))
    tail = [""] if end < len(lines) else []
    return "\n".join(lines[: heading + 1] + body + tail + lines[end:]).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Starting, scheduling and applying.


def start_phase(agent_slug: str, *, name: str, start: str, plan: str = "", today: str) -> str:
    """Switch to a new phase starting `start` (today or earlier)."""
    old = memory_v2.read_phase(agent_slug) or {}
    title = title_for(name)
    if old.get("start") and start <= old["start"]:
        raise ValueError(f"the new phase must start after the current one ({old['start']})")
    end = (date_cls.fromisoformat(start) - timedelta(days=1)).isoformat() if old.get("start") else start

    story = cards.read_card(agent_slug, cards.STORY)
    if old.get("start"):
        story = close_and_open(story, agent_slug=agent_slug, old=old, end=end,
                               new_title=title, new_start=start, plan=plan, today=today)
    else:
        story = close_and_open(story, agent_slug=agent_slug, old={"start": start}, end=start,
                               new_title=title, new_start=start, plan=plan, today=today)
    story = review.condense_story(agent_slug, story, today=today, limit=cards.BUDGETS[cards.STORY],
                                  phase_start=start)
    cards.write_card(agent_slug, cards.STORY, story)

    playbook = cards.read_card(agent_slug, cards.PLAYBOOK)
    if playbook.strip():
        bullets = [f"{title}, since {brief.fmt_day(start, today=today)} {start[:4]}."]
        if plan:
            bullets.append(f"Plan: {plan.rstrip('.')}.")
        if old.get("start"):
            old_title = old.get("title") or title_for(old.get("name", "previous phase"))
            bullets.append(f"Previous phase: {old_title}, {brief.fmt_day(old['start'], today=today)} to "
                           f"{brief.fmt_day(end, today=today)}; the story card has how it went.")
        cards.write_card(agent_slug, cards.PLAYBOOK, replace_section_bullets(playbook, "Phase", bullets))

    history = list(old.get("history", []))
    if old.get("start"):
        history.append({"name": old.get("name"), "title": old.get("title") or title_for(old.get("name", "")),
                        "start": old["start"], "end": end})
    memory_v2.write_phase(agent_slug, {"name": name, "title": title, "start": start,
                                       "protect_through_week": 0, "history": history})
    if review.last_reviewed_week(agent_slug) is not None:
        review.mark_reviewed(agent_slug, 0, today)

    note = (f"Phase changed to {title.lower()} on {brief.fmt_day(start, today=today)}. Targets and pivot rules "
            "in the playbook may still be the previous phase's; agree new ones with the user.")
    entry = memory_template.build_recent_entry(summary=note, category="goal", source="assistant",
                                               ttl_days=14, now=config.local_now())
    memory_template.append_recent_entry(memory_v2.recent_path(agent_slug), entry)
    log.info("Phase changed to %s from %s", name, start)
    return f"phase {name} from {start}"


def schedule_phase(agent_slug: str, *, name: str, start: str, plan: str = "", today: str) -> str:
    phase = memory_v2.read_phase(agent_slug) or {}
    phase["next"] = {"name": name, "start": start, "plan": plan, "agreed": today}
    memory_v2.write_phase(agent_slug, phase)
    return f"phase {name} scheduled for {start}"


def apply_due(agent_slug: str, *, today: str) -> str | None:
    """Start a scheduled phase once its day has come. Safe to call often."""
    phase = memory_v2.read_phase(agent_slug) or {}
    nxt = phase.get("next")
    if not nxt or nxt.get("start", "9999") > today:
        return None
    phase.pop("next")
    memory_v2.write_phase(agent_slug, phase)
    try:
        return start_phase(agent_slug, name=nxt["name"], start=nxt["start"], plan=nxt.get("plan", ""), today=today)
    except Exception:
        log.exception("Scheduled phase change failed; keeping it for the next try")
        phase["next"] = nxt
        memory_v2.write_phase(agent_slug, phase)
        return None
