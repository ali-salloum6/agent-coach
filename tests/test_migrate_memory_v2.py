"""Tests for the rule-based part of the memory migration (synthetic data only)."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("OPENROUTER_API_KEY", "test-key")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import migrate_memory_v2 as mig  # noqa: E402

OLD_FILE = """\
# Test Memory

## Current Snapshot

- [2026-01-05] User is 180 cm tall. _(user · profile)_
  - [2026-02-01 10:00] status=superseded — Actually 181 cm.

## Time Series

### Weight

- [2026-01-05] User weighs 90.5 kg. (90.5 kg)
- [2026-01-05] User's weight is 90.5 kg today. (90.5 kg)
- [2026-01-12] User weighed exactly 90kg, down from 90.5kg on Jan 5. (90 kg)
- [2026-01-19] Jan 19 fasted weigh-in: 89.4kg (flat vs 89.5 on Jan 16). (89.4 kg)
- [2026-01-20] User has lost 3kg since starting, from 93kg to 90kg. (90 kg)
- [2026-01-21] User weighed 93 kg in late December when starting. (93 kg)
- [2026-01-22] Weighed 90.2 kg on the gym scale with clothes on. (90.2 kg)
- [2026-01-23] Weighed 89-90 kg on two scales. (89-90 kg)
- [2026-01-26] User weighs approximately 89kg. (89 kg)

### Measurements

- [2026-01-05] User's narrowest waist measurement is 90 cm. (90 cm)
- [2026-01-05] Belly at the navel is 100 cm vs 90 cm at the waist; a 10 cm differential. (100 cm)
- [2026-01-12] Tape on 2026-01-12: waist (narrowest) 89cm, belly 99.5cm, hips 104cm.
- [2026-01-12] User's upper waist measurement decreased by 1 cm to 89 cm. (89 cm)
- [2026-01-12] User lost 0.5 cm off their belly this week. (-0.5 cm)
- [2026-01-19] Post-workout pumped chest at nipple level measured 105 cm. (105 cm)
- [2026-01-19] Shoulder tape is about 120cm, a 2cm drop from the Jan 5 reading of 122cm. (2 cm)
- [2026-01-19] Estimated daily calorie total: about 2,100 kcal. (2100 kcal)
- [2026-01-19] User benched 60kg for 8 reps.

### Nutrition

- [2026-01-18] Estimated calorie intake so far today is approximately 1,400 kcal. (1400 kcal)
- [2026-01-18] Today's running total is about 2,050 calories and 140g protein.
- [2026-01-17] Estimated running calorie total at 15:00 was 1,690 kcal; not a final daily total. (1690 kcal)
- [2026-01-17] Estimated today's total calorie intake at about 2,790 kcal. (2790 kcal)
- [2026-01-17] Running protein total is about 75g, not a final daily total. (75 g protein)
- [2026-01-19] Estimated daily protein total: about 150–160 g. (150–160 g protein)
- [2026-01-20] Yesterday's final intake was about 2,250 calories and 158g protein.
- [2026-01-20] User has approximately 600 calories and 50g of protein remaining for today.
- [2026-01-20] Whey logs at about 120 kcal and 24g protein per scoop. (24 g protein)
- [2026-01-17] Estimated daily total including the proposed dinner is about 2,900 kcal. (2900 kcal)

### Sleep

- [2026-01-18] User slept from 12 to 9 (9 hours). (9 hours)
- [2026-01-19] User slept badly.

## Timeline

- [2026-01-05] User started the cut. _(user · milestone)_
"""


def convert(markdown: str):
    entries = mig.parse_entries(markdown)
    out = mig.Collector()
    by_sub = {name: [e for e in entries if e.sub == name] for name in ("Weight", "Measurements", "Nutrition", "Sleep")}
    mig.convert_weight(by_sub["Weight"], out)
    intake = mig.convert_tape(by_sub["Measurements"], out)
    mig.convert_sleep(by_sub["Sleep"], out)
    days = mig.convert_intake(by_sub["Nutrition"] + intake, out)
    return entries, out, days


class ParseTests(unittest.TestCase):
    def test_entries_carry_line_number_section_and_notes(self) -> None:
        entries = mig.parse_entries(OLD_FILE)
        first = entries[0]
        self.assertEqual((first.n, first.section, first.sub), (5, "Current Snapshot", None))
        self.assertEqual((first.date, first.category, first.text), ("2026-01-05", "profile", "User is 180 cm tall."))
        self.assertEqual(len(first.notes), 1)
        self.assertEqual(entries[1].sub, "Weight")

    def test_suffix_value(self) -> None:
        self.assertEqual(mig.suffix_value("- [2026-01-05] x (90.5 kg)"), (90.5, None, "kg"))
        self.assertEqual(mig.suffix_value("- [2026-01-05] x (2,100 kcal)"), (2100.0, None, "kcal"))
        self.assertEqual(mig.suffix_value("- [2026-01-05] x (89-90 kg)"), (89.0, 90.0, "kg"))
        self.assertEqual(mig.suffix_value("- [2026-01-05] x (handle gap 14 cm)"), (None, None, None))


class WeightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.entries, self.out, _ = convert(OLD_FILE)
        self.weights = {(k[0], k[2]): v["value"] for k, v in self.out.metrics.items() if k[1] == "weight"}

    def test_duplicates_collapse_and_comparisons_do_not_redate(self) -> None:
        self.assertEqual(self.weights[("2026-01-05", "")], 90.5)
        self.assertEqual(self.weights[("2026-01-12", "")], 90.0)
        self.assertEqual(self.weights[("2026-01-19", "")], 89.4)

    def test_derived_and_restated_readings_are_left_out(self) -> None:
        for day in ("2026-01-20", "2026-01-21", "2026-01-23"):
            self.assertNotIn((day, ""), self.weights)

    def test_non_standard_and_approximate_readings_are_marked(self) -> None:
        self.assertEqual(self.weights[("2026-01-22", "non-standard scale or conditions")], 90.2)
        self.assertEqual(self.out.metrics[("2026-01-26", "weight", "")]["note"], "approximate")


class TapeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.entries, self.out, self.days = convert(OLD_FILE)
        self.m = {k: v["value"] for k, v in self.out.metrics.items()}

    def test_site_comes_from_the_first_site_named(self) -> None:
        self.assertEqual(self.m[("2026-01-05", "waist", "")], 90.0)
        self.assertEqual(self.m[("2026-01-05", "belly", "")], 100.0)

    def test_several_readings_in_one_sentence(self) -> None:
        self.assertEqual(self.m[("2026-01-12", "belly", "")], 99.5)
        self.assertEqual(self.m[("2026-01-12", "hips", "")], 104.0)
        self.assertEqual(self.m[("2026-01-12", "waist", "")], 89.0)

    def test_changes_are_not_readings(self) -> None:
        self.assertNotIn(("2026-01-19", "shoulders", ""), self.m)
        self.assertEqual([k for k in self.m if k[1] == "belly" and k[0] == "2026-01-12"], [("2026-01-12", "belly", "")])

    def test_post_workout_reading_is_non_standard(self) -> None:
        self.assertEqual(self.m[("2026-01-19", "chest", "post-workout")], 105.0)

    def test_lift_filed_under_measurements_goes_to_review(self) -> None:
        lift = next(e for e in self.entries if "benched" in e.line)
        decision = self.out.decisions[lift.n]
        self.assertEqual((decision.to, decision.review), ("archive", True))


class IntakeTests(unittest.TestCase):
    def setUp(self) -> None:
        _, self.out, self.days = convert(OLD_FILE)

    def test_last_total_of_the_day_wins_and_running_is_flagged(self) -> None:
        day = self.days["2026-01-18"]
        self.assertEqual((day["kcal"], day["protein"], day["kcal_running"]), (2050.0, 140.0, True))

    def test_not_a_final_total_is_running_and_does_not_outrank_later_lines(self) -> None:
        day = self.days["2026-01-17"]
        self.assertEqual((day["kcal"], day["kcal_running"]), (2790.0, False))
        self.assertEqual((day["protein"], day["protein_running"]), (75.0, True))

    def test_yesterday_moves_to_the_day_before_and_is_final(self) -> None:
        day = self.days["2026-01-19"]
        self.assertEqual((day["kcal"], day["protein"]), (2250.0, 158.0))
        self.assertFalse(day["kcal_running"])

    def test_remaining_budget_and_food_facts_are_not_totals(self) -> None:
        self.assertNotIn("2026-01-20", self.days)

    def test_sleep_hours(self) -> None:
        self.assertEqual(self.out.metrics[("2026-01-18", "sleep", "")]["value"], 9.0)
        self.assertNotIn(("2026-01-19", "sleep", ""), self.out.metrics)


if __name__ == "__main__":
    unittest.main()
