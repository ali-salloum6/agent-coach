"""Structured logs for the v2 memory: body metrics, food, lifts and events.

Each log is one JSON object per line in `data/<slug>/logs/<name>.jsonl`.
The files stay small (a few thousand rows a year), so every operation loads
the whole log and rewrites it atomically. Plain text keeps them readable,
hand-editable and covered by the nightly backup.

Rows are facts about what happened. They are never pasted into the prompt
raw: `brief.py` shows the coach summaries computed here (latest value, trend,
records, the day's meals and totals), so the arithmetic is done by code.
"""

from __future__ import annotations

import json
import os
import secrets
from datetime import date as date_cls, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import config

METRICS = "metrics"
FOOD = "food"
LIFTS = "lifts"
EVENTS = "events"
LOG_NAMES: tuple[str, ...] = (METRICS, FOOD, LIFTS, EVENTS)

Row = dict[str, Any]


# ---------------------------------------------------------------------------
# Storage.


def agent_dir(agent_slug: str) -> Path:
    return config.DATA_DIR / agent_slug


def log_path(agent_slug: str, name: str) -> Path:
    if name not in LOG_NAMES:
        raise ValueError(f"unknown log {name!r}")
    return agent_dir(agent_slug) / "logs" / f"{name}.jsonl"


def read_log(agent_slug: str, name: str) -> list[Row]:
    path = log_path(agent_slug, name)
    if not path.exists():
        return []
    rows: list[Row] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def write_log(agent_slug: str, name: str, rows: Iterable[Row]) -> None:
    """Write the whole log, oldest first, replacing the file atomically."""
    path = log_path(agent_slug, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(rows, key=lambda r: (r.get("date", ""), r.get("time", "")))
    body = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in ordered)
    tmp = path.with_suffix(".jsonl.tmp")
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, path)


def _new_id(rows: Iterable[Row]) -> str:
    taken = {r.get("id") for r in rows}
    while True:
        candidate = secrets.token_hex(3)
        if candidate not in taken:
            return candidate


def _stamp(row: Row, rows: list[Row], src: str) -> Row:
    row["id"] = _new_id(rows)
    row["src"] = src
    row["added"] = config.local_now().isoformat(timespec="seconds")
    return row


def _check_date(value: str) -> str:
    date_cls.fromisoformat(value)
    return value


def _norm(text: str | None) -> str:
    return " ".join((text or "").lower().split())


# ---------------------------------------------------------------------------
# Body metrics: weight, tape sites and anything else measured over time.


def add_metric(
    agent_slug: str,
    *,
    date: str,
    metric: str,
    value: float,
    unit: str,
    cond: str = "",
    note: str = "",
    src: str = "chat",
) -> tuple[Row, bool]:
    """Record a reading. Returns the row and whether it replaced an older one.

    One reading per day, metric and condition. `cond` stays empty for the
    standard protocol (for weight: fasted, home scale). A non-standard
    reading (gym scale, post-workout tape) carries a `cond` and sits beside
    the standard one without entering trends.
    """
    rows = read_log(agent_slug, METRICS)
    key = (_check_date(date), _norm(metric), _norm(cond))
    row: Row = {
        "date": date,
        "metric": _norm(metric),
        "value": value,
        "unit": unit,
        "cond": cond.strip(),
        "note": note.strip(),
    }
    replaced = False
    kept: list[Row] = []
    for existing in rows:
        if (existing["date"], existing["metric"], _norm(existing.get("cond"))) == key:
            replaced = True
            continue
        kept.append(existing)
    _stamp(row, kept, src)
    kept.append(row)
    write_log(agent_slug, METRICS, kept)
    return row, replaced


def metric_series(
    rows: Iterable[Row], metric: str, *, standard_only: bool = True
) -> list[tuple[str, float]]:
    """(date, value) pairs for one metric, oldest first."""
    wanted = _norm(metric)
    out = [
        (r["date"], float(r["value"]))
        for r in rows
        if r["metric"] == wanted and not (standard_only and r.get("cond"))
    ]
    return sorted(out)


def rate_per_week(
    series: list[tuple[str, float]], *, weeks: int, today: str
) -> float | None:
    """Least-squares slope over the last `weeks` weeks, in units per week.

    Needs at least 3 readings that span at least half the window; otherwise
    the trend is not meaningful and None is returned.
    """
    end = date_cls.fromisoformat(today)
    start = end - timedelta(days=weeks * 7)
    points = [
        ((date_cls.fromisoformat(d) - start).days, v)
        for d, v in series
        if start <= date_cls.fromisoformat(d) <= end
    ]
    if len(points) < 3:
        return None
    xs = [p[0] for p in points]
    if max(xs) - min(xs) < weeks * 7 / 2:
        return None
    n = len(points)
    mean_x = sum(xs) / n
    mean_y = sum(p[1] for p in points) / n
    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom == 0:
        return None
    slope_per_day = sum((x - mean_x) * (y - mean_y) for x, y in points) / denom
    return slope_per_day * 7


def metric_summary(
    rows: Iterable[Row], metric: str, *, today: str, since: str | None = None
) -> dict[str, Any] | None:
    """Latest reading, extremes and trend for one metric.

    `since` is usually the start date of the current phase: the summary then
    includes the first reading on or after it and the change from there.
    """
    series = metric_series(rows, metric)
    if not series:
        return None
    latest_date, latest = series[-1]
    low = min(series, key=lambda p: p[1])
    high = max(series, key=lambda p: p[1])
    summary: dict[str, Any] = {
        "metric": _norm(metric),
        "latest": latest,
        "latest_date": latest_date,
        "previous": series[-2] if len(series) > 1 else None,
        "lowest": low,
        "highest": high,
        "readings": len(series),
        "rate_3w": rate_per_week(series, weeks=3, today=today),
        "rate_6w": rate_per_week(series, weeks=6, today=today),
    }
    if since:
        after = [p for p in series if p[0] >= since]
        if after:
            summary["since"] = after[0]
            summary["change_since"] = round(latest - after[0][1], 2)
    return summary


# ---------------------------------------------------------------------------
# Food: items eaten, plus reported day totals for days without items.


def add_food(
    agent_slug: str,
    *,
    date: str,
    name: str,
    kcal: float | None,
    protein: float | None,
    qty: str = "",
    time: str = "",
    note: str = "",
    src: str = "chat",
) -> Row:
    rows = read_log(agent_slug, FOOD)
    row: Row = {
        "date": _check_date(date),
        "time": time,
        "kind": "item",
        "name": name.strip(),
        "qty": qty.strip(),
        "kcal": kcal,
        "protein": protein,
        "note": note.strip(),
    }
    _stamp(row, rows, src)
    rows.append(row)
    write_log(agent_slug, FOOD, rows)
    return row


def set_day_total(
    agent_slug: str,
    *,
    date: str,
    kcal: float | None,
    protein: float | None,
    final: bool = True,
    note: str = "",
    src: str = "chat",
) -> Row:
    """Record a reported total for a day, replacing an earlier reported total.

    Used when the items are unknown (history, or "I ate about 2,300 today").
    When a day also has items, the items win: see `day_totals`.
    """
    rows = [
        r
        for r in read_log(agent_slug, FOOD)
        if not (r["kind"] == "day_total" and r["date"] == date)
    ]
    row: Row = {
        "date": _check_date(date),
        "time": "",
        "kind": "day_total",
        "kcal": kcal,
        "protein": protein,
        "final": final,
        "note": note.strip(),
    }
    _stamp(row, rows, src)
    rows.append(row)
    write_log(agent_slug, FOOD, rows)
    return row


def update_food(agent_slug: str, row_id: str, **changes: Any) -> Row | None:
    """Correct one food row in place. Returns the new row, or None if missing."""
    rows = read_log(agent_slug, FOOD)
    for row in rows:
        if row.get("id") == row_id:
            allowed = {"date", "time", "name", "qty", "kcal", "protein", "note"}
            row.update({k: v for k, v in changes.items() if k in allowed})
            write_log(agent_slug, FOOD, rows)
            return row
    return None


def remove_food(agent_slug: str, row_id: str) -> bool:
    rows = read_log(agent_slug, FOOD)
    kept = [r for r in rows if r.get("id") != row_id]
    if len(kept) == len(rows):
        return False
    write_log(agent_slug, FOOD, kept)
    return True


def day_items(rows: Iterable[Row], date: str) -> list[Row]:
    return [r for r in rows if r["date"] == date and r["kind"] == "item"]


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _added(row: Row) -> datetime:
    """When a row was saved, comparable across time zones (oldest if unknown)."""
    try:
        when = datetime.fromisoformat(row.get("added") or "")
    except ValueError:
        return _EPOCH
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def day_totals(rows: Iterable[Row], date: str) -> dict[str, Any]:
    """Calories and protein for one day.

    A reported total ("about 1,200 so far", a closed day, or a day carried
    over from the old memory) covers everything logged before it; items
    logged after it are added to it. Without a reported total the day is the
    sum of its items. `basis` says which: "items", "reported",
    "reported+items" or "none". `final` is True only for a closed day with
    nothing added since.
    """
    rows = list(rows)
    items = day_items(rows, date)
    reported = next((r for r in rows if r["date"] == date and r["kind"] == "day_total"), None)

    def summed(chosen: list[Row]) -> tuple[int, float]:
        return round(sum(r.get("kcal") or 0 for r in chosen)), round(sum(r.get("protein") or 0 for r in chosen), 1)

    if reported:
        later = [r for r in items if _added(r) > _added(reported)]
        if later:
            kcal, protein = summed(later)
            return {
                "date": date,
                "kcal": round((reported.get("kcal") or 0) + kcal),
                "protein": round((reported.get("protein") or 0) + protein, 1),
                "items": len(later),
                "basis": "reported+items",
                "final": None,
            }
        return {
            "date": date,
            "kcal": reported.get("kcal"),
            "protein": reported.get("protein"),
            "items": 0,
            "basis": "reported",
            "final": reported.get("final", True),
        }
    if items:
        kcal, protein = summed(items)
        return {"date": date, "kcal": kcal, "protein": protein, "items": len(items), "basis": "items", "final": None}
    return {"date": date, "kcal": None, "protein": None, "items": 0, "basis": "none", "final": None}


def intake_average(rows: Iterable[Row], *, start: str, end: str) -> dict[str, Any]:
    """Average daily calories and protein over the logged days in a range.

    Days without any record, and days whose only record is a running
    (non-final) total, are left out and counted in `days_missing`.
    """
    rows = list(rows)
    first, last = date_cls.fromisoformat(start), date_cls.fromisoformat(end)
    kcal: list[float] = []
    protein: list[float] = []
    missing = 0
    day = first
    while day <= last:
        totals = day_totals(rows, day.isoformat())
        usable = totals["basis"] in ("items", "reported+items") or (
            totals["basis"] == "reported" and totals["final"]
        )
        if usable and totals["kcal"] is not None:
            kcal.append(totals["kcal"])
            if totals["protein"] is not None:
                protein.append(totals["protein"])
        else:
            missing += 1
        day += timedelta(days=1)
    return {
        "start": start,
        "end": end,
        "days_logged": len(kcal),
        "days_missing": missing,
        "kcal": round(sum(kcal) / len(kcal)) if kcal else None,
        "protein": round(sum(protein) / len(protein)) if protein else None,
    }


# ---------------------------------------------------------------------------
# Lifts: one row per exercise, load and day.


def add_lift(
    agent_slug: str,
    *,
    date: str,
    exercise: str,
    load: float | None,
    unit: str | None,
    reps: list[int],
    note: str = "",
    src: str = "chat",
) -> tuple[Row, bool]:
    """Record the sets done at one load. Replaces the same exercise, load and day."""
    rows = read_log(agent_slug, LIFTS)
    key = (_check_date(date), _norm(exercise), load)
    row: Row = {
        "date": date,
        "exercise": _norm(exercise),
        "load": load,
        "unit": unit,
        "reps": list(reps),
        "note": note.strip(),
    }
    kept = [r for r in rows if (r["date"], r["exercise"], r.get("load")) != key]
    replaced = len(kept) != len(rows)
    _stamp(row, kept, src)
    kept.append(row)
    write_log(agent_slug, LIFTS, kept)
    return row, replaced


def lift_history(rows: Iterable[Row], exercise: str) -> list[Row]:
    wanted = _norm(exercise)
    return sorted((r for r in rows if r["exercise"] == wanted), key=lambda r: r["date"])


def best_set(rows: Iterable[Row], exercise: str) -> Row | None:
    """The heaviest load lifted, and the most reps done at that load."""
    history = [r for r in lift_history(rows, exercise) if r.get("load") is not None and r.get("reps")]
    if not history:
        return None
    return max(history, key=lambda r: (r["load"], max(r["reps"]), r["date"]))


# ---------------------------------------------------------------------------
# Events: dated milestones, decisions, injuries and moves.


def add_event(
    agent_slug: str, *, date: str, kind: str, text: str, src: str = "chat"
) -> tuple[Row, bool]:
    """Record an event once. Returns the row and whether it was new."""
    rows = read_log(agent_slug, EVENTS)
    for existing in rows:
        if existing["date"] == date and _norm(existing["text"]) == _norm(text):
            return existing, False
    row: Row = {"date": _check_date(date), "kind": _norm(kind), "text": text.strip()}
    _stamp(row, rows, src)
    rows.append(row)
    write_log(agent_slug, EVENTS, rows)
    return row, True


def today_iso(now: datetime | None = None) -> str:
    return (now or config.local_now()).astimezone(config.MEMORY_TIMEZONE).date().isoformat()
