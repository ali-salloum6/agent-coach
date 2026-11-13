"""Tests for the numbers block computed from the logs.

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

import brief  # noqa: E402
import config  # noqa: E402
import ledger  # noqa: E402

SLUG = "test_agent"
TODAY = "2026-02-23"


class BriefTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = patch.object(config, "DATA_DIR", Path(tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)


class FormatTests(unittest.TestCase):
    def test_numbers_and_days(self) -> None:
        self.assertEqual(brief.fmt_num(81.25), "81.25")
        self.assertEqual(brief.fmt_num(81.10), "81.1")
        self.assertEqual(brief.fmt_num(70.0), "70")
        self.assertEqual(brief.fmt_num(2430.4, 0), "2,430")
        self.assertEqual(brief.fmt_signed(1.35), "+1.35")
        self.assertEqual(brief.fmt_signed(-0.9), "−0.9")
        self.assertEqual(brief.fmt_day("2026-02-09"), "9 Feb")
        self.assertEqual(brief.fmt_day("2025-12-31", today=TODAY), "31 Dec 2025")


class WeightAndTapeTests(BriefTestCase):
    def setUp(self) -> None:
        super().setUp()
        for d, v in [("2025-11-03", 99.0), ("2026-01-26", 84.3), ("2026-02-02", 86.0),
                     ("2026-02-09", 86.3), ("2026-02-16", 86.6), ("2026-02-23", 86.9)]:
            ledger.add_metric(SLUG, date=d, metric="weight", value=v, unit="kg")
        ledger.add_metric(SLUG, date="2026-02-23", metric="weight", value=87.6, unit="kg", cond="gym scale")
        ledger.add_metric(SLUG, date="2026-01-26", metric="belly", value=95.4, unit="cm")
        ledger.add_metric(SLUG, date="2026-02-23", metric="belly", value=95.5, unit="cm")
        self.metrics = ledger.read_log(SLUG, ledger.METRICS)

    def test_weight_block(self) -> None:
        text = brief.weight_block(self.metrics, today=TODAY, since="2026-01-26")
        self.assertIn("86.9 kg on 23 Feb.", text)
        self.assertIn("Since the phase started (84.3 kg on 26 Jan): +2.6 kg.", text)
        self.assertIn("+0.3 kg a week over the last 3 weeks", text)
        self.assertIn("Lowest 84.3 kg (26 Jan), highest 99 kg (3 Nov 2025).", text)
        self.assertTrue(text.endswith("16 Feb 86.6 · 23 Feb 86.9"))
        self.assertNotIn("87.6", text)

    def test_tape_block(self) -> None:
        text = brief.tape_block(self.metrics, today=TODAY, since="2026-01-26")
        self.assertIn(
            "- Belly at the navel: 95.5 cm on 23 Feb; before that 95.4 on 26 Jan; "
            "95.4 when the phase started; lowest 95.4 on 26 Jan.",
            text,
        )
        self.assertNotIn("Waist", text)

    def test_empty_logs(self) -> None:
        self.assertEqual(brief.weight_block([], today=TODAY, since=None), "Weight: no readings yet.")
        self.assertEqual(brief.tape_block([], today=TODAY, since=None), "Tape: no readings yet.")


class LiftTests(BriefTestCase):
    def test_last_session_and_earlier_best(self) -> None:
        ledger.add_lift(SLUG, date="2026-02-02", exercise="bench press", load=70, unit="kg", reps=[10])
        ledger.add_lift(SLUG, date="2026-02-16", exercise="bench press", load=70, unit="kg", reps=[7, 5])
        ledger.add_lift(SLUG, date="2026-02-16", exercise="row", load=70, unit="kg", reps=[11, 11])
        ledger.add_lift(SLUG, date="2025-12-01", exercise="leg press", load=250, unit="kg", reps=[15])
        text = brief.lifts_block(ledger.read_log(SLUG, ledger.LIFTS), today=TODAY)
        self.assertIn("- Bench press: last 16 Feb: 70 kg × 7, 5. Best: 70 kg × 10 (2 Feb).", text)
        self.assertIn("- Row: last 16 Feb: 70 kg × 11, 11.", text)
        self.assertNotIn("Leg press", text)

    def test_nothing_recent(self) -> None:
        self.assertEqual(brief.lifts_block([], today=TODAY), "Lifts: nothing logged in the last 5 weeks.")


class FoodTests(BriefTestCase):
    def test_today_items_are_listed_and_summed(self) -> None:
        apple = ledger.add_food(SLUG, date=TODAY, time="13:10", name="apple", kcal=80, protein=0.4, qty="small")
        ledger.add_food(SLUG, date=TODAY, time="15:27", name="fish and potatoes", kcal=520, protein=42)
        ledger.set_day_total(SLUG, date="2026-02-22", kcal=2300, protein=140)
        ledger.set_day_total(SLUG, date="2026-02-21", kcal=2700, protein=160)
        ledger.set_day_total(SLUG, date="2026-02-20", kcal=2650, protein=150, final=False)
        text = brief.food_block(ledger.read_log(SLUG, ledger.FOOD), today=TODAY)
        self.assertIn("- Today (23 Feb): 600 kcal, 42 g protein from 2 logged items.", text)
        self.assertIn(f"  · 13:10 apple (small): 80 kcal, 0.4 g [{apple['id']}]", text)
        self.assertIn("- Yesterday: 2,300 kcal, 140 g protein (reported total).", text)
        self.assertIn("- Last 7 days: average 2,500 kcal and 150 g protein over 2 logged days (only what was logged; a low day may be incomplete).", text)

    def test_empty_day(self) -> None:
        text = brief.food_block([], today=TODAY)
        self.assertIn("- Today (23 Feb): nothing logged.", text)
        self.assertNotIn("Last 7 days", text)


if __name__ == "__main__":
    unittest.main()
