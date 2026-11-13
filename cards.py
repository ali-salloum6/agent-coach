"""State cards and tripwires for the v2 memory.

Cards are small Markdown files the coach sees in full: the profile, the open
threads, the playbook and the story. They are rewritten in place, never
appended to without limit: each has a size budget, and every rewrite first
saves the previous version under `history/`, so nothing is lost.

Tripwires are the playbook's pivot rules in a form code can check against
the logs, for example "weight gain above 0.25 kg a week for 3 weeks".
"""

from __future__ import annotations

import json
import operator
import os
from pathlib import Path
from typing import Any, Callable, Iterable

import brief
import config
import ledger

PROFILE = "profile"
THREADS = "threads"
PLAYBOOK = "playbook"
STORY = "story"

# Maximum size of each card, in characters.
BUDGETS: dict[str, int] = {
    PROFILE: 5_000,
    THREADS: 4_000,
    PLAYBOOK: 7_000,
    STORY: 12_000,
}

_OPS: dict[str, Callable[[float, float], bool]] = {
    ">": operator.gt,
    ">=": operator.ge,
    "<": operator.lt,
    "<=": operator.le,
}
_OP_WORDS = {">": "above", ">=": "at or above", "<": "below", "<=": "at or below"}


class CardTooLong(ValueError):
    """The card would exceed its budget; it must be condensed, not truncated."""


def card_path(agent_slug: str, name: str) -> Path:
    if name not in BUDGETS:
        raise ValueError(f"unknown card {name!r}")
    return ledger.agent_dir(agent_slug) / "cards" / f"{name}.md"


def read_card(agent_slug: str, name: str) -> str:
    path = card_path(agent_slug, name)
    return path.read_text(encoding="utf-8") if path.exists() else ""


def write_card(agent_slug: str, name: str, text: str) -> None:
    """Replace a card, keeping the previous version in `history/`."""
    text = text.rstrip() + "\n"
    if len(text) > BUDGETS[name]:
        raise CardTooLong(f"{name} card is {len(text):,} characters; the limit is {BUDGETS[name]:,}")
    path = card_path(agent_slug, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        previous = path.read_text(encoding="utf-8")
        if previous == text:
            return
        history = ledger.agent_dir(agent_slug) / "history"
        history.mkdir(parents=True, exist_ok=True)
        stamp = config.local_now().strftime("%Y%m%dT%H%M%S")
        target = history / f"{name}.{stamp}.md"
        n = 1
        while target.exists():  # several edits in one second each keep their version
            target = history / f"{name}.{stamp}-{n}.md"
            n += 1
        target.write_text(previous, encoding="utf-8")
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Tripwires.


def tripwires_path(agent_slug: str) -> Path:
    return ledger.agent_dir(agent_slug) / "cards" / "tripwires.json"


def read_tripwires(agent_slug: str) -> list[dict[str, Any]]:
    path = tripwires_path(agent_slug)
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def check_tripwire(wire: dict[str, Any], metrics: Iterable[dict[str, Any]], *, today: str) -> dict[str, Any]:
    """Evaluate one rule against the logs.

    Two kinds of rule:
    - "rate": the metric's trend over `window_weeks`, in units per week,
      compared with `threshold`.
    - "level": the last `consecutive` standard readings, each compared with
      `threshold`.

    Returns the rule with `status` ("tripped", "clear" or "unknown") and a
    `detail` sentence showing the numbers used.
    """
    series = ledger.metric_series(metrics, wire["metric"])
    compare = _OPS[wire["op"]]
    unit = wire.get("unit", "")
    limit = f"{_OP_WORDS[wire['op']]} {brief.fmt_num(wire['threshold'])} {unit}".strip()
    status = "unknown"
    if wire["kind"] == "rate":
        weeks = int(wire["window_weeks"])
        rate = ledger.rate_per_week(series, weeks=weeks, today=today)
        if rate is None:
            detail = f"too few readings in the last {weeks} weeks to compute the trend (trips {limit})"
        else:
            status = "tripped" if compare(rate, wire["threshold"]) else "clear"
            detail = f"trend is {brief.fmt_signed(rate)} {unit} over the last {weeks} weeks (trips {limit})"
    elif wire["kind"] == "level":
        needed = int(wire.get("consecutive", 1))
        last = series[-needed:]
        shown = ", ".join(f"{brief.fmt_num(v)} on {brief.fmt_day(d, today=today)}" for d, v in last)
        if len(last) < needed:
            detail = f"needs {needed} readings; has {len(last)}" + (f" ({shown})" if shown else "")
        else:
            status = "tripped" if all(compare(v, wire["threshold"]) for _, v in last) else "clear"
            times = "reading" if needed == 1 else f"{needed} readings in a row"
            detail = f"last {times}: {shown} (trips {limit})"
    else:
        raise ValueError(f"unknown tripwire kind {wire['kind']!r}")
    return {**wire, "status": status, "detail": detail}


def tripwire_lines(agent_slug: str, *, today: str) -> str:
    """The tripwire status block shown to the coach."""
    wires = read_tripwires(agent_slug)
    if not wires:
        return ""
    metrics = ledger.read_log(agent_slug, ledger.METRICS)
    lines = []
    for wire in wires:
        result = check_tripwire(wire, metrics, today=today)
        label = {"tripped": "TRIPPED", "clear": "clear", "unknown": "cannot tell yet"}[result["status"]]
        line = f"- {wire['name']}: {label}; {result['detail']}."
        if result["status"] == "tripped":
            line += f" Rule: {wire['then']}"
        lines.append(line)
    return "Pivot rules checked against the logs:\n" + "\n".join(lines)
