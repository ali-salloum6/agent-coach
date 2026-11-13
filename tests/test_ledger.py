"""Tests for the structured logs (body metrics, food, lifts, events).

All values are made up.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("OPENROUTER_API_KEY", "test-key")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import ledger  # noqa: E402

SLUG = "test_agent"


class LedgerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = patch.object(config, "DATA_DIR", Path(tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)


class MetricTests(LedgerTestCase):
    def test_same_day_reading_replaces_the_earlier_one(self) -> None:
        ledger.add_metric(SLUG, date="2026-02-02", metric="weight", value=81.2, unit="kg")
        _, replaced = ledger.add_metric(
            SLUG, date="2026-02-02", metric="Weight", value=80.8, unit="kg"
        )
        rows = ledger.read_log(SLUG, ledger.METRICS)
        self.assertTrue(replaced)
        self.assertEqual([(r["date"], r["value"]) for r in rows], [("2026-02-02", 80.8)])

    def test_non_standard_reading_sits_beside_and_stays_out_of_the_series(self) -> None:
        ledger.add_metric(SLUG, date="2026-02-03", metric="weight", value=82.0, unit="kg")
        _, replaced = ledger.add_metric(
            SLUG, date="2026-02-03", metric="weight", value=82.9, unit="kg",
            cond="gym scale, clothed",
        )
        rows = ledger.read_log(SLUG, ledger.METRICS)
        self.assertFalse(replaced)
        self.assertEqual(len(rows), 2)
        self.assertEqual(ledger.metric_series(rows, "weight"), [("2026-02-03", 82.0)])
        self.assertEqual(len(ledger.metric_series(rows, "weight", standard_only=False)), 2)

    def test_rows_are_stored_oldest_first(self) -> None:
        ledger.add_metric(SLUG, date="2026-02-09", metric="weight", value=81.0, unit="kg")
        ledger.add_metric(SLUG, date="2026-02-02", metric="weight", value=81.2, unit="kg")
        dates = [r["date"] for r in ledger.read_log(SLUG, ledger.METRICS)]
        self.assertEqual(dates, ["2026-02-02", "2026-02-09"])

    def test_invalid_date_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ledger.add_metric(SLUG, date="yesterday", metric="weight", value=80, unit="kg")

    def test_rate_per_week_is_the_trend_over_the_window(self) -> None:
        series = [("2026-02-02", 80.0), ("2026-02-09", 80.3), ("2026-02-16", 80.6), ("2026-02-23", 80.9)]
        rate = ledger.rate_per_week(series, weeks=3, today="2026-02-23")
        self.assertAlmostEqual(rate, 0.3, places=6)

    def test_rate_needs_three_readings_spread_over_the_window(self) -> None:
        two = [("2026-02-16", 80.6), ("2026-02-23", 80.9)]
        bunched = [("2026-02-21", 80.6), ("2026-02-22", 80.7), ("2026-02-23", 80.9)]
        self.assertIsNone(ledger.rate_per_week(two, weeks=3, today="2026-02-23"))
        self.assertIsNone(ledger.rate_per_week(bunched, weeks=3, today="2026-02-23"))

    def test_summary_reports_change_since_phase_start(self) -> None:
        for d, v in [("2026-01-05", 85.05), ("2026-01-12", 84.3), ("2026-02-09", 87.1), ("2026-02-23", 86.75)]:
            ledger.add_metric(SLUG, date=d, metric="weight", value=v, unit="kg")
        rows = ledger.read_log(SLUG, ledger.METRICS)
        summary = ledger.metric_summary(rows, "weight", today="2026-02-23", since="2026-01-12")
        self.assertEqual(summary["latest"], 86.75)
        self.assertEqual(summary["lowest"], ("2026-01-12", 84.3))
        self.assertEqual(summary["since"], ("2026-01-12", 84.3))
        self.assertEqual(summary["change_since"], 2.45)
        self.assertIsNone(ledger.metric_summary(rows, "navel", today="2026-02-23"))


class FoodTests(LedgerTestCase):
    def test_day_total_is_summed_from_items(self) -> None:
        ledger.add_food(SLUG, date="2026-02-11", name="apple", kcal=80, protein=0.4)
        ledger.add_food(SLUG, date="2026-02-11", name="burrito", kcal=900, protein=40)
        ledger.add_food(SLUG, date="2026-02-11", name="fish and potatoes", kcal=520, protein=42)
        ledger.add_food(SLUG, date="2026-02-10", name="porridge", kcal=400, protein=20)
        totals = ledger.day_totals(ledger.read_log(SLUG, ledger.FOOD), "2026-02-11")
        self.assertEqual(
            (totals["kcal"], totals["protein"], totals["items"], totals["basis"]),
            (1500, 82.4, 3, "items"),
        )

    def test_a_total_reported_after_items_covers_them(self) -> None:
        with patch.object(ledger.config, "local_now", return_value=ledger.datetime(2026, 2, 11, 13, 0, tzinfo=config.MEMORY_TIMEZONE)):
            ledger.add_food(SLUG, date="2026-02-11", name="apple", kcal=80, protein=0)
        with patch.object(ledger.config, "local_now", return_value=ledger.datetime(2026, 2, 11, 22, 0, tzinfo=config.MEMORY_TIMEZONE)):
            ledger.set_day_total(SLUG, date="2026-02-11", kcal=2000, protein=120)
        totals = ledger.day_totals(ledger.read_log(SLUG, ledger.FOOD), "2026-02-11")
        self.assertEqual((totals["kcal"], totals["basis"], totals["final"]), (2000, "reported", True))

    def test_items_logged_after_a_running_total_add_to_it(self) -> None:
        with patch.object(ledger.config, "local_now", return_value=ledger.datetime(2026, 2, 11, 9, 0, tzinfo=config.MEMORY_TIMEZONE)):
            ledger.set_day_total(SLUG, date="2026-02-11", kcal=700, protein=30, final=False)
        with patch.object(ledger.config, "local_now", return_value=ledger.datetime(2026, 2, 11, 13, 0, tzinfo=config.MEMORY_TIMEZONE)):
            ledger.add_food(SLUG, date="2026-02-11", name="lunch", kcal=600, protein=45)
        totals = ledger.day_totals(ledger.read_log(SLUG, ledger.FOOD), "2026-02-11")
        self.assertEqual((totals["kcal"], totals["protein"], totals["items"], totals["basis"]), (1300, 75, 1, "reported+items"))

    def test_save_times_compare_across_time_zones(self) -> None:
        # 12:00 at UTC+3 is 17:00 at UTC+8: the item saved at 16:00 UTC+8 came first.
        rows = [
            {"date": "2026-02-11", "kind": "day_total", "kcal": 700, "protein": 30, "final": False,
             "added": "2026-02-11T12:00:00+03:00"},
            {"date": "2026-02-11", "kind": "item", "kcal": 100, "protein": 5, "added": "2026-02-11T16:00:00+08:00"},
            {"date": "2026-02-11", "kind": "item", "kcal": 200, "protein": 10, "added": "2026-02-11T18:00:00+08:00"},
        ]
        totals = ledger.day_totals(rows, "2026-02-11")
        self.assertEqual((totals["kcal"], totals["items"]), (900, 1))

    def test_a_forgotten_snack_adds_to_a_closed_day(self) -> None:
        with patch.object(ledger.config, "local_now", return_value=ledger.datetime(2026, 2, 11, 22, 0, tzinfo=config.MEMORY_TIMEZONE)):
            ledger.set_day_total(SLUG, date="2026-02-11", kcal=2000, protein=120, final=True)
        with patch.object(ledger.config, "local_now", return_value=ledger.datetime(2026, 2, 12, 9, 0, tzinfo=config.MEMORY_TIMEZONE)):
            ledger.add_food(SLUG, date="2026-02-11", name="snack", kcal=150, protein=5)
        totals = ledger.day_totals(ledger.read_log(SLUG, ledger.FOOD), "2026-02-11")
        self.assertEqual((totals["kcal"], totals["protein"], totals["basis"]), (2150, 125, "reported+items"))

    def test_reported_total_is_replaced_not_duplicated(self) -> None:
        ledger.set_day_total(SLUG, date="2026-02-09", kcal=1700, protein=90, final=False)
        ledger.set_day_total(SLUG, date="2026-02-09", kcal=2300, protein=140)
        rows = ledger.read_log(SLUG, ledger.FOOD)
        self.assertEqual(len(rows), 1)
        totals = ledger.day_totals(rows, "2026-02-09")
        self.assertEqual((totals["kcal"], totals["basis"], totals["final"]), (2300, "reported", True))

    def test_day_without_records_has_no_total(self) -> None:
        totals = ledger.day_totals([], "2026-02-11")
        self.assertEqual((totals["kcal"], totals["basis"]), (None, "none"))

    def test_correcting_and_removing_an_item(self) -> None:
        row = ledger.add_food(SLUG, date="2026-02-11", name="rice", kcal=130, protein=2.7, qty="100 g")
        ledger.update_food(SLUG, row["id"], qty="200 g", kcal=260, protein=5.4, id="hacked")
        rows = ledger.read_log(SLUG, ledger.FOOD)
        self.assertEqual((rows[0]["id"], rows[0]["kcal"], rows[0]["qty"]), (row["id"], 260, "200 g"))
        self.assertTrue(ledger.remove_food(SLUG, row["id"]))
        self.assertFalse(ledger.remove_food(SLUG, row["id"]))
        self.assertEqual(ledger.read_log(SLUG, ledger.FOOD), [])

    def test_average_skips_missing_and_running_days(self) -> None:
        ledger.set_day_total(SLUG, date="2026-02-03", kcal=2500, protein=150)
        ledger.set_day_total(SLUG, date="2026-02-04", kcal=1400, protein=100, final=False)
        ledger.add_food(SLUG, date="2026-02-05", name="all day", kcal=2300, protein=140)
        avg = ledger.intake_average(
            ledger.read_log(SLUG, ledger.FOOD), start="2026-02-03", end="2026-02-06"
        )
        self.assertEqual(
            (avg["kcal"], avg["protein"], avg["days_logged"], avg["days_missing"]),
            (2400, 145, 2, 2),
        )


class LiftTests(LedgerTestCase):
    def test_same_exercise_load_and_day_is_replaced(self) -> None:
        ledger.add_lift(SLUG, date="2026-02-09", exercise="Bench Press", load=70, unit="kg", reps=[6])
        _, replaced = ledger.add_lift(
            SLUG, date="2026-02-09", exercise="bench press", load=70, unit="kg", reps=[6, 4]
        )
        rows = ledger.read_log(SLUG, ledger.LIFTS)
        self.assertTrue(replaced)
        self.assertEqual([(r["exercise"], r["reps"]) for r in rows], [("bench press", [6, 4])])

    def test_best_set_is_heaviest_then_most_reps(self) -> None:
        ledger.add_lift(SLUG, date="2026-01-29", exercise="bench press", load=70, unit="kg", reps=[8, 6, 4])
        ledger.add_lift(SLUG, date="2026-02-16", exercise="bench press", load=70, unit="kg", reps=[10])
        ledger.add_lift(SLUG, date="2026-01-12", exercise="bench press", load=65, unit="kg", reps=[8, 7, 6])
        ledger.add_lift(SLUG, date="2026-02-09", exercise="row", load=70, unit="kg", reps=[11, 11])
        rows = ledger.read_log(SLUG, ledger.LIFTS)
        best = ledger.best_set(rows, "Bench press")
        self.assertEqual((best["date"], best["load"], max(best["reps"])), ("2026-02-16", 70, 10))
        self.assertEqual(len(ledger.lift_history(rows, "bench press")), 3)
        self.assertIsNone(ledger.best_set(rows, "squat"))


class EventTests(LedgerTestCase):
    def test_same_event_is_recorded_once(self) -> None:
        _, new = ledger.add_event(SLUG, date="2026-02-01", kind="milestone", text="Phase two starts.")
        _, again = ledger.add_event(SLUG, date="2026-02-01", kind="milestone", text="phase two  starts.")
        self.assertTrue(new)
        self.assertFalse(again)
        self.assertEqual(len(ledger.read_log(SLUG, ledger.EVENTS)), 1)


if __name__ == "__main__":
    unittest.main()
