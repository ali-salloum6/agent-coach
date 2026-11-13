"""Tests for state cards and tripwires.

All values are made up.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("OPENROUTER_API_KEY", "test-key")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cards  # noqa: E402
import config  # noqa: E402
import ledger  # noqa: E402

SLUG = "test_agent"
GAIN = {"id": "gain", "name": "Gaining too fast", "metric": "weight", "kind": "rate", "op": ">",
        "threshold": 0.25, "unit": "kg a week", "window_weeks": 3, "then": "Cut 100–150 kcal a day."}
NAVEL = {"id": "navel", "name": "Belly at 91 cm", "metric": "belly", "kind": "level", "op": ">=",
         "threshold": 91, "unit": "cm", "consecutive": 2, "then": "Stop the surplus."}


class CardsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        patcher = patch.object(config, "DATA_DIR", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)


class CardTests(CardsTestCase):
    def test_rewrite_keeps_the_previous_version(self) -> None:
        cards.write_card(SLUG, cards.PROFILE, "# Profile\n- **Location**: Lisbon")
        cards.write_card(SLUG, cards.PROFILE, "# Profile\n- **Location**: Porto")
        self.assertEqual(cards.read_card(SLUG, cards.PROFILE), "# Profile\n- **Location**: Porto\n")
        history = list((self.root / SLUG / "history").glob("profile.*.md"))
        self.assertEqual(len(history), 1)
        self.assertIn("Lisbon", history[0].read_text())

    def test_unchanged_text_adds_no_history(self) -> None:
        cards.write_card(SLUG, cards.STORY, "# Story")
        cards.write_card(SLUG, cards.STORY, "# Story\n")
        self.assertFalse((self.root / SLUG / "history").exists())

    def test_card_over_budget_is_refused_and_the_old_one_stays(self) -> None:
        cards.write_card(SLUG, cards.THREADS, "# Threads")
        with self.assertRaises(cards.CardTooLong):
            cards.write_card(SLUG, cards.THREADS, "x" * (cards.BUDGETS[cards.THREADS] + 1))
        self.assertEqual(cards.read_card(SLUG, cards.THREADS), "# Threads\n")

    def test_missing_card_reads_empty_and_unknown_card_is_an_error(self) -> None:
        self.assertEqual(cards.read_card(SLUG, cards.PLAYBOOK), "")
        with self.assertRaises(ValueError):
            cards.read_card(SLUG, "diary")


class TripwireTests(CardsTestCase):
    def _weights(self, values: list[tuple[str, float]]) -> list[dict]:
        return [{"date": d, "metric": "weight", "value": v, "cond": ""} for d, v in values]

    def test_rate_rule_trips_when_the_trend_exceeds_the_limit(self) -> None:
        rows = self._weights([("2026-02-02", 80.0), ("2026-02-09", 80.3), ("2026-02-16", 80.6), ("2026-02-23", 80.9)])
        result = cards.check_tripwire(GAIN, rows, today="2026-02-23")
        self.assertEqual(result["status"], "tripped")
        self.assertEqual(result["detail"], "trend is +0.3 kg a week over the last 3 weeks (trips above 0.25 kg a week)")

    def test_rate_rule_is_clear_below_the_limit(self) -> None:
        rows = self._weights([("2026-02-02", 80.0), ("2026-02-09", 80.1), ("2026-02-16", 80.2), ("2026-02-23", 80.3)])
        self.assertEqual(cards.check_tripwire(GAIN, rows, today="2026-02-23")["status"], "clear")

    def test_rate_rule_cannot_tell_with_too_few_readings(self) -> None:
        rows = self._weights([("2026-02-16", 80.6), ("2026-02-23", 80.9)])
        result = cards.check_tripwire(GAIN, rows, today="2026-02-23")
        self.assertEqual(result["status"], "unknown")

    def test_level_rule_needs_every_one_of_the_last_readings(self) -> None:
        def belly(values):
            return [{"date": d, "metric": "belly", "value": v, "cond": ""} for d, v in values]
        once = belly([("2026-02-16", 90.4), ("2026-02-23", 91.2)])
        twice = belly([("2026-02-09", 90.0), ("2026-02-16", 91.0), ("2026-02-23", 91.2)])
        self.assertEqual(cards.check_tripwire(NAVEL, once, today="2026-02-23")["status"], "clear")
        result = cards.check_tripwire(NAVEL, twice, today="2026-02-23")
        self.assertEqual(result["status"], "tripped")
        self.assertEqual(result["detail"], "last 2 readings in a row: 91 on 16 Feb, 91.2 on 23 Feb (trips at or above 91 cm)")
        self.assertEqual(cards.check_tripwire(NAVEL, belly([("2026-02-23", 92.0)]), today="2026-02-23")["status"], "unknown")

    def test_non_standard_readings_do_not_trip_a_rule(self) -> None:
        rows = [
            {"date": "2026-02-16", "metric": "belly", "value": 88.5, "cond": ""},
            {"date": "2026-02-23", "metric": "belly", "value": 88.6, "cond": ""},
            {"date": "2026-02-23", "metric": "belly", "value": 92.0, "cond": "post-workout"},
        ]
        self.assertEqual(cards.check_tripwire(NAVEL, rows, today="2026-02-23")["status"], "clear")

    def test_status_block(self) -> None:
        path = cards.tripwires_path(SLUG)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps([GAIN, NAVEL]))
        for d, v in [("2026-02-02", 80.0), ("2026-02-09", 80.3), ("2026-02-16", 80.6), ("2026-02-23", 80.9)]:
            ledger.add_metric(SLUG, date=d, metric="weight", value=v, unit="kg")
        text = cards.tripwire_lines(SLUG, today="2026-02-23")
        self.assertIn("- Gaining too fast: TRIPPED; trend is +0.3 kg a week over the last 3 weeks (trips above 0.25 kg a week). Rule: Cut 100–150 kcal a day.", text)
        self.assertIn("- Belly at 91 cm: cannot tell yet; needs 2 readings; has 0.", text)

    def test_no_tripwires_no_block(self) -> None:
        self.assertEqual(cards.tripwire_lines(SLUG, today="2026-02-23"), "")


if __name__ == "__main__":
    unittest.main()
