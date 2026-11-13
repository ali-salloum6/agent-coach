"""Text the coach sees, computed from the v2 logs.

The logs hold every reading, set and meal. The coach never sees them raw:
this module turns them into a short "numbers" block (latest values, trend,
records, the day's meals and totals), so the arithmetic is done by code and
is the same in every conversation.
"""

from __future__ import annotations

from datetime import date as date_cls, timedelta
from typing import Any, Iterable

import ledger

Row = dict[str, Any]

# Tape sites in display order, with the name shown to the coach.
TAPE_SITES: tuple[tuple[str, str], ...] = (
    ("belly", "Belly at the navel"),
    ("waist", "Waist at the narrowest point"),
    ("shoulders", "Shoulders"),
    ("chest", "Chest"),
    ("hips", "Hips"),
)
MAX_LIFT_LINES = 12
RECENT_LIFT_DAYS = 35


def fmt_num(value: float | None, digits: int = 2) -> str:
    """81.25, 81.1, 60 and 2,430: no trailing zeros, thousands separated."""
    if value is None:
        return "?"
    rounded = round(float(value), digits)
    if rounded.is_integer():
        return f"{int(rounded):,}"
    return f"{rounded:,.{digits}f}".rstrip("0")


def fmt_day(iso: str, *, today: str | None = None) -> str:
    """"9 Nov", with the year added when it differs from today's."""
    d = date_cls.fromisoformat(iso)
    text = f"{d.day} {d.strftime('%b')}"
    if today and d.year != date_cls.fromisoformat(today).year:
        text += f" {d.year}"
    return text


def fmt_signed(value: float, digits: int = 2) -> str:
    return ("+" if value > 0 else "−" if value < 0 else "") + fmt_num(abs(value), digits)


# ---------------------------------------------------------------------------
# Weight and tape.


def weight_block(metrics: Iterable[Row], *, today: str, since: str | None) -> str:
    summary = ledger.metric_summary(metrics, "weight", today=today, since=since)
    if not summary:
        return "Weight: no readings yet."
    series = ledger.metric_series(metrics, "weight")
    parts = [
        f"Weight (fasted, home scale): {fmt_num(summary['latest'])} kg on "
        f"{fmt_day(summary['latest_date'], today=today)}."
    ]
    if "since" in summary and summary["since"][0] != summary["latest_date"]:
        start_date, start_value = summary["since"]
        parts.append(
            f"Since the phase started ({fmt_num(start_value)} kg on {fmt_day(start_date, today=today)}): "
            f"{fmt_signed(summary['change_since'])} kg."
        )
    trends = []
    for weeks, key in ((3, "rate_3w"), (6, "rate_6w")):
        rate = summary[key]
        if rate is not None:
            trends.append(f"{fmt_signed(rate)} kg a week over the last {weeks} weeks")
    parts.append(
        "Trend: " + " and ".join(trends) + "."
        if trends
        else "Trend: too few readings in the last 6 weeks to compute one."
    )
    low_date, low = summary["lowest"]
    high_date, high = summary["highest"]
    parts.append(
        f"Lowest {fmt_num(low)} kg ({fmt_day(low_date, today=today)}), "
        f"highest {fmt_num(high)} kg ({fmt_day(high_date, today=today)})."
    )
    last = " · ".join(f"{fmt_day(d, today=today)} {fmt_num(v)}" for d, v in series[-6:])
    return " ".join(parts) + f"\nLast readings: {last}"


def tape_block(metrics: Iterable[Row], *, today: str, since: str | None) -> str:
    metrics = list(metrics)
    lines = []
    for metric, label in TAPE_SITES:
        summary = ledger.metric_summary(metrics, metric, today=today, since=since)
        if not summary:
            continue
        line = f"- {label}: {fmt_num(summary['latest'])} cm on {fmt_day(summary['latest_date'], today=today)}"
        extras = []
        if summary["previous"]:
            prev_date, prev = summary["previous"]
            extras.append(f"before that {fmt_num(prev)} on {fmt_day(prev_date, today=today)}")
        if "since" in summary and summary["since"][0] != summary["latest_date"]:
            start_date, start = summary["since"]
            extras.append(f"{fmt_num(start)} when the phase started")
        low_date, low = summary["lowest"]
        if low_date != summary["latest_date"]:
            extras.append(f"lowest {fmt_num(low)} on {fmt_day(low_date, today=today)}")
        lines.append(line + ("; " + "; ".join(extras) if extras else "") + ".")
    return "Tape (cold, fasted):\n" + "\n".join(lines) if lines else "Tape: no readings yet."


# ---------------------------------------------------------------------------
# Lifts.


def _sets_text(row: Row) -> str:
    load = row.get("load")
    unit = row.get("unit") or ""
    reps = ", ".join(str(r) for r in row.get("reps") or [])
    if load is None:
        return f"{reps} reps" if reps else "done"
    head = f"{fmt_num(load)} {unit}".strip()
    return f"{head} × {reps}" if reps else head


def lifts_block(lifts: Iterable[Row], *, today: str) -> str:
    lifts = list(lifts)
    cutoff = (date_cls.fromisoformat(today) - timedelta(days=RECENT_LIFT_DAYS)).isoformat()
    recent: dict[str, list[Row]] = {}
    for row in lifts:
        if row["date"] >= cutoff:
            recent.setdefault(row["exercise"], []).append(row)
    if not recent:
        return "Lifts: nothing logged in the last 5 weeks."
    # Most often trained first.
    names = sorted(recent, key=lambda name: (-len({r["date"] for r in recent[name]}), name))
    lines = []
    for name in names[:MAX_LIFT_LINES]:
        history = ledger.lift_history(lifts, name)
        last_date = history[-1]["date"]
        last = "; ".join(_sets_text(r) for r in history if r["date"] == last_date)
        line = f"- {name.capitalize()}: last {fmt_day(last_date, today=today)}: {last}"
        best = ledger.best_set(lifts, name)
        if best and best["date"] != last_date:
            line += f". Best: {fmt_num(best['load'])} {best.get('unit') or ''} × {max(best['reps'])} ({fmt_day(best['date'], today=today)})"
        lines.append(line.rstrip() + ".")
    return "Lifts (last session, and the best set when it was earlier):\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# Food.


def _totals_text(totals: dict[str, Any]) -> str:
    if totals["basis"] == "none":
        return "nothing logged"
    text = f"{fmt_num(totals['kcal'], 0)} kcal, {fmt_num(totals['protein'], 0)} g protein"
    if totals["basis"] == "items":
        return f"{text} from {totals['items']} logged item{'s' if totals['items'] != 1 else ''}"
    if totals["basis"] == "reported+items":
        n = totals["items"]
        return f"{text}: an earlier running total plus {n} item{'s' if n != 1 else ''} logged since"
    return text + (" (reported total)" if totals["final"] else " (running total, day not closed)")


def food_block(food: Iterable[Row], *, today: str) -> str:
    food = list(food)
    day = date_cls.fromisoformat(today)
    yesterday = (day - timedelta(days=1)).isoformat()
    lines = [f"- Today ({fmt_day(today)}): {_totals_text(ledger.day_totals(food, today))}."]
    for item in sorted(ledger.day_items(food, today), key=lambda r: r.get("time", "")):
        qty = f" ({item['qty']})" if item.get("qty") else ""
        when = f"{item['time']} " if item.get("time") else ""
        lines.append(
            f"  · {when}{item['name']}{qty}: {fmt_num(item.get('kcal'), 0)} kcal, "
            f"{fmt_num(item.get('protein'), 1)} g [{item['id']}]"
        )
    lines.append(f"- Yesterday: {_totals_text(ledger.day_totals(food, yesterday))}.")
    week = ledger.intake_average(food, start=(day - timedelta(days=7)).isoformat(), end=yesterday)
    if week["days_logged"]:
        lines.append(
            f"- Last 7 days: average {fmt_num(week['kcal'], 0)} kcal and {fmt_num(week['protein'], 0)} g protein "
            f"over {week['days_logged']} logged day{'s' if week['days_logged'] != 1 else ''} "
            "(only what was logged; a low day may be incomplete)."
        )
    return "Food:\n" + "\n".join(lines)


# ---------------------------------------------------------------------------


def numbers(agent_slug: str, *, today: str, phase_start: str | None = None) -> str:
    """The whole numbers block for one agent."""
    metrics = ledger.read_log(agent_slug, ledger.METRICS)
    return "\n\n".join(
        [
            weight_block(metrics, today=today, since=phase_start),
            tape_block(metrics, today=today, since=phase_start),
            lifts_block(ledger.read_log(agent_slug, ledger.LIFTS), today=today),
            food_block(ledger.read_log(agent_slug, ledger.FOOD), today=today),
        ]
    )
