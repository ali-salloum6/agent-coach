"""Tests for the memory v2 writer: checks and application of each operation.

All data is made up.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("OPENROUTER_API_KEY", "test-key")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cards  # noqa: E402
import config  # noqa: E402
import ledger  # noqa: E402
import memory_v2  # noqa: E402
import recall  # noqa: E402
import writer_v2 as w  # noqa: E402

SLUG = "gym_coach"
NOW = datetime(2026, 2, 23, 14, 0, tzinfo=config.MEMORY_TIMEZONE)
TODAY = "2026-02-23"

PROFILE = """# Profile

## Basics
- **Location**: Lisbon since Jan 2026.
- **Work**: designer.

## Goals
- **Aim**: get stronger.
"""

THREADS = """# Open threads

## Skin
- Using a retinoid twice a week.

## Sleep
- Trying a 23:00 bedtime.
"""

PLAYBOOK = """# Playbook

## Targets
- Calories: 2,500 kcal every day (set 2 Feb).
- Protein: 150 g a day.

## Pivot rules
- Weight trend above 0.3 kg a week over 3 weeks: cut 100 kcal a day.
"""


class WriterTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        patcher = patch.object(config, "DATA_DIR", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        cards.write_card(SLUG, cards.PROFILE, PROFILE)
        cards.write_card(SLUG, cards.THREADS, THREADS)
        cards.write_card(SLUG, cards.PLAYBOOK, PLAYBOOK)

    def apply(self, op: dict, user: str = "", assistant: str = "", attachment: bool = False) -> str:
        return w.apply_op(SLUG, op, w.Exchange(user, assistant, attachment), now=NOW)

    def rejects(self, op: dict, user: str = "", assistant: str = "", attachment: bool = False) -> str:
        with self.assertRaises(w.Rejected) as ctx:
            self.apply(op, user, assistant, attachment)
        return str(ctx.exception)


class EvidenceTests(WriterTestCase):
    def test_evidence_must_quote_the_claimed_text(self) -> None:
        op = {"op": "note", "text": "x", "source": "user", "evidence": "never said this"}
        self.assertIn("not a quote", self.rejects(op, user="hello there"))

    def test_quotes_may_start_inside_bold_text(self) -> None:
        op = {"op": "log_food", "name": "rice", "kcal": 130, "protein": 3, "source": "assistant",
              "evidence": "Cooked rice, 100 grams:** 130 calories, 3 grams of protein."}
        self.apply(op, user="had rice", assistant="- **Cooked rice, 100 grams:** 130 calories, 3 grams of protein.")
        self.assertEqual(len(ledger.read_log(SLUG, ledger.FOOD)), 1)

    def test_user_only_ops_refuse_the_coachs_words(self) -> None:
        op = {"op": "set_profile", "section": "Basics", "key": "Work", "value": "teacher",
              "source": "assistant", "evidence": "you are a teacher"}
        self.assertIn("user's own words", self.rejects(op, assistant="you are a teacher"))

    def test_unknown_op(self) -> None:
        self.assertIn("unknown op", self.rejects({"op": "delete_everything"}))


class FoodTests(WriterTestCase):
    REPLY = "Logged: rice 100 g, 130 kcal and 2.7 g protein; chicken 125 g, 205 kcal and 38.8 g protein."

    def test_log_food_with_stated_numbers(self) -> None:
        op = {"op": "log_food", "name": "rice", "qty": "100 g", "kcal": 130, "protein": 2.7,
              "time": "13:05", "source": "user", "evidence": "had rice and chicken"}
        self.apply(op, user="had rice and chicken", assistant=self.REPLY)
        rows = ledger.read_log(SLUG, ledger.FOOD)
        self.assertEqual((rows[0]["date"], rows[0]["name"], rows[0]["kcal"]), (TODAY, "rice", 130))

    def test_negligible_counts_as_zero(self) -> None:
        op = {"op": "log_food", "name": "chili sauce", "kcal": 20, "protein": 0, "source": "user", "evidence": "20 g chili sauce"}
        self.apply(op, user="20 g chili sauce", assistant="Chili sauce: 20 kcal, negligible protein.")
        self.assertEqual(ledger.read_log(SLUG, ledger.FOOD)[0]["protein"], 0)

    def test_invented_numbers_are_rejected(self) -> None:
        op = {"op": "log_food", "name": "rice", "kcal": 150, "protein": 3, "source": "user", "evidence": "had rice"}
        self.assertIn("150, 3", self.rejects(op, user="had rice", assistant=self.REPLY))
        self.assertEqual(ledger.read_log(SLUG, ledger.FOOD), [])

    def test_future_and_old_dates_are_rejected(self) -> None:
        op = {"op": "log_food", "name": "rice", "kcal": 130, "protein": 2.7, "source": "user", "evidence": "rice"}
        self.assertIn("future", self.rejects({**op, "date": "2026-02-24"}, user="rice", assistant=self.REPLY))
        self.assertIn("days ago", self.rejects({**op, "date": "2026-01-01"}, user="rice", assistant=self.REPLY))

    def test_short_quotes_must_be_the_whole_message(self) -> None:
        op = {"op": "log_food", "name": "rice", "kcal": 130, "protein": 2.7, "source": "user", "evidence": "rice"}
        self.assertIn("at least 12", self.rejects(op, user="had some rice today", assistant=self.REPLY))

    def test_nulls_and_nan_change_nothing(self) -> None:
        row = ledger.add_food(SLUG, date="2026-02-20", name="rice", kcal=130, protein=2.7)
        fix = {"op": "correct_food", "id": row["id"], "kcal": None, "date": None, "qty": "150 g",
               "source": "user", "evidence": "it was 150 g of rice"}
        self.apply(fix, user="it was 150 g of rice")
        kept = ledger.read_log(SLUG, ledger.FOOD)[0]
        self.assertEqual((kept["kcal"], kept["date"], kept["qty"]), (130, "2026-02-20", "150 g"))
        nan = {"op": "log_food", "name": "banana", "kcal": float("nan"), "source": "user", "evidence": "had a banana today"}
        self.assertIn("finite", self.rejects(nan, user="had a banana today", assistant="A banana is about 90 kcal."))

    def test_correct_and_remove_by_id(self) -> None:
        row = ledger.add_food(SLUG, date=TODAY, name="rice", kcal=130, protein=2.7, qty="100 g")
        fix = {"op": "correct_food", "id": row["id"], "qty": "200 g", "kcal": 260, "protein": 5.4,
               "source": "user", "evidence": "it was 200 g"}
        self.apply(fix, user="it was 200 g of rice", assistant="Updated: 260 kcal and 5.4 g protein.")
        self.assertEqual(ledger.read_log(SLUG, ledger.FOOD)[0]["kcal"], 260)
        drop = {"op": "remove_food", "id": row["id"], "source": "user", "evidence": "didn't eat the rice"}
        self.apply(drop, user="I didn't eat the rice after all")
        self.assertEqual(ledger.read_log(SLUG, ledger.FOOD), [])
        self.assertIn("no food row", self.rejects(drop, user="I didn't eat the rice after all"))


class MeasurementTests(WriterTestCase):
    def test_reading_must_be_in_the_users_message(self) -> None:
        op = {"op": "log_metric", "metric": "weight", "value": 81.4, "unit": "kg", "cond": "",
              "source": "user", "evidence": "81.4 this morning"}
        self.apply(op, user="81.4 this morning", assistant="Noted.")
        self.assertEqual(ledger.metric_series(ledger.read_log(SLUG, ledger.METRICS), "weight"), [(TODAY, 81.4)])
        echo = {**op, "value": 80.9, "source": "assistant", "evidence": "you were 80.9"}
        self.assertIn("80.9", self.rejects(echo, user="how am I doing?", assistant="Last week you were 80.9."))

    def test_photo_readings_may_come_from_the_coachs_description(self) -> None:
        op = {"op": "log_metric", "metric": "weight", "value": 81.4, "unit": "kg", "cond": "",
              "source": "assistant", "evidence": "The scale shows 81.4 kg"}
        self.apply(op, user="User sent 1 image(s).", assistant="The scale shows 81.4 kg.", attachment=True)
        self.assertEqual(len(ledger.read_log(SLUG, ledger.METRICS)), 1)

    def test_units_and_metric_names_are_checked(self) -> None:
        op = {"op": "log_metric", "metric": "weight", "value": 81.4, "unit": "lb", "source": "user", "evidence": "81.4"}
        self.assertIn("must be in kg", self.rejects(op, user="81.4"))
        self.assertIn("unknown metric", self.rejects({**op, "metric": "mood", "unit": "kg"}, user="81.4"))

    def test_lift_reps_and_load_are_checked(self) -> None:
        op = {"op": "log_lift", "exercise": "bench press", "load": 70, "unit": "kg", "reps": [8, 6],
              "source": "user", "evidence": "bench 70 for 8 and 6"}
        self.apply(op, user="bench 70 for 8 and 6")
        self.assertEqual(ledger.read_log(SLUG, ledger.LIFTS)[0]["reps"], [8, 6])
        self.assertIn("7", self.rejects({**op, "reps": [8, 7]}, user="bench 70 for 8 and 6"))
        self.assertIn("whole numbers", self.rejects({**op, "reps": ["8"]}, user="bench 70 for 8 and 6"))


class CardTests(WriterTestCase):
    def test_set_profile_replaces_a_key_and_keeps_history(self) -> None:
        op = {"op": "set_profile", "section": "Basics", "key": "Location", "value": "Porto since Feb 2026.",
              "source": "user", "evidence": "moved to Porto"}
        self.apply(op, user="I moved to Porto last week")
        card = cards.read_card(SLUG, cards.PROFILE)
        self.assertIn("- **Location**: Porto since Feb 2026.", card)
        self.assertNotIn("Lisbon", card)
        self.assertTrue(any("Lisbon" in p.read_text() for p in (self.root / SLUG / "history").glob("profile.*")))

    def test_set_profile_adds_a_new_key_at_the_end_of_its_section(self) -> None:
        op = {"op": "set_profile", "section": "goals", "key": "Event", "value": "a 10 km race in May.",
              "source": "user", "evidence": "signed up for a 10 km race"}
        self.apply(op, user="I signed up for a 10 km race in May")
        card = cards.read_card(SLUG, cards.PROFILE)
        self.assertIn("- **Aim**: get stronger.\n- **Event**: a 10 km race in May.", card)
        self.assertIn("no section", self.rejects({**op, "section": "Hobbies"}, user="I signed up for a 10 km race in May"))

    def test_threads_are_replaced_added_and_closed_to_the_archive(self) -> None:
        exchange = {"user": "retinoid is done, skin is clear; started magnesium"}
        self.apply({"op": "set_thread", "topic": "Supplements", "lines": ["Started magnesium."],
                    "source": "user", "evidence": "started magnesium"}, **exchange)
        self.apply({"op": "close_thread", "topic": "skin", "source": "user", "evidence": "skin is clear"}, **exchange)
        card = cards.read_card(SLUG, cards.THREADS)
        self.assertNotIn("## Skin", card)
        self.assertIn("## Supplements\n- Started magnesium.", card)
        self.assertIn("## Sleep", card)
        self.assertIn("retinoid", (self.root / SLUG / "archive" / "closed_threads.md").read_text())

    def test_food_reference_is_updated_by_name_or_alias(self) -> None:
        self.apply({"op": "food_reference", "name": "Corner Cafe wrap", "aliases": ["wrap"], "serving": "1 wrap",
                    "kcal": 600, "protein": 35, "source": "assistant", "evidence": "about 600 kcal"},
                   user="the wrap again", assistant="The wrap is about 600 kcal and 35 g protein.")
        self.apply({"op": "food_reference", "name": "Corner Cafe chicken wrap", "aliases": ["wrap"], "serving": "1 wrap",
                    "kcal": 540, "protein": 38, "source": "user", "evidence": "540 kcal and 38 g"},
                   user="the label says 540 kcal and 38 g")
        rows = recall.load_foodbook(SLUG)
        self.assertEqual([(r["name"], r["kcal"]) for r in rows], [("Corner Cafe chicken wrap", 540)])

    def test_note_goes_to_the_short_lived_notes(self) -> None:
        self.apply({"op": "note", "text": "Try the earlier gym slot this week.", "days": 7, "category": "logistics",
                    "source": "assistant", "evidence": "Try the earlier slot"},
                   assistant="Try the earlier slot this week.")
        self.assertIn("Try the earlier gym slot this week.", memory_v2.recent_notes(SLUG, NOW))
        self.assertIn("days must be", self.rejects({"op": "note", "text": "x", "days": 3, "source": "assistant",
                                                    "evidence": "Try"}, assistant="Try"))


class PlaybookTests(WriterTestCase):
    PROPOSAL = {"op": "propose_change", "section": "Targets", "old": "Calories: 2,500",
                "new": "Calories: 2,400 kcal every day (set 23 Feb).", "why": "gaining too fast",
                "source": "assistant", "evidence": "drop to 2,400"}

    def test_proposal_waits_until_the_user_agrees(self) -> None:
        self.apply(self.PROPOSAL, assistant="I suggest we drop to 2,400 a day.")
        self.assertIn("2,500", cards.read_card(SLUG, cards.PLAYBOOK))
        pending = memory_v2.read_pending(SLUG)
        self.assertEqual([p["id"] for p in pending], ["p1"])
        self.apply({"op": "apply_change", "proposal": "p1", "source": "user", "evidence": "ok, do it"}, user="ok, do it")
        card = cards.read_card(SLUG, cards.PLAYBOOK)
        self.assertIn("- Calories: 2,400 kcal every day (set 23 Feb).", card)
        self.assertNotIn("2,500", card)
        self.assertEqual(memory_v2.read_pending(SLUG), [])

    def test_proposal_ids_are_not_reused(self) -> None:
        self.apply(self.PROPOSAL, assistant="I suggest we drop to 2,400 a day.")
        self.apply({"op": "apply_change", "proposal": "p1", "source": "user", "evidence": "yes"}, user="yes")
        self.apply({**self.PROPOSAL, "old": "Protein: 150 g", "new": "Protein: 160 g a day."},
                   assistant="I suggest we drop to 2,400 a day, and protein to 160.")
        self.assertEqual([p["id"] for p in memory_v2.read_pending(SLUG)], ["p2"])

    def test_the_coach_cannot_change_the_playbook_directly(self) -> None:
        op = {**self.PROPOSAL, "op": "change_playbook"}
        self.assertIn("user's own words", self.rejects(op, assistant="drop to 2,400"))

    def test_user_change_with_new_numbers_from_the_exchange(self) -> None:
        op = {"op": "change_playbook", "section": "Targets", "old": "Protein: 150 g",
              "new": "Protein: 170 g a day.", "source": "user", "evidence": "protein to 170"}
        self.apply(op, user="let's put protein to 170")
        self.assertIn("- Protein: 170 g a day.", cards.read_card(SLUG, cards.PLAYBOOK))

    def test_new_numbers_must_come_from_the_exchange_or_the_old_line(self) -> None:
        keeps_date = {"op": "change_playbook", "section": "Targets", "old": "Calories: 2,500",
                      "new": "Calories: 2,300 kcal every day (set 2 Feb).", "source": "user", "evidence": "make it 2,300"}
        self.apply(keeps_date, user="make it 2,300")
        invented = {**keeps_date, "old": "Protein: 150 g", "new": "Protein: 175 g a day."}
        self.assertIn("175", self.rejects(invented, user="make it 2,300"))

    def test_old_text_must_match_exactly_one_bullet(self) -> None:
        op = {"op": "change_playbook", "section": "Nowhere", "old": "a day", "new": "x",
              "source": "user", "evidence": "x"}
        self.assertIn("exactly one", self.rejects(op, user="x"))

    def test_section_narrows_the_match_and_headings_never_match(self) -> None:
        op = {"op": "change_playbook", "section": "Targets", "old": "a day", "new": None,
              "source": "user", "evidence": "drop the protein line"}
        self.apply(op, user="drop the protein line")
        card = cards.read_card(SLUG, cards.PLAYBOOK)
        self.assertNotIn("Protein", card)
        self.assertIn("cut 100 kcal a day", card)
        heading = {**op, "section": "Nowhere", "old": "Pivot rules"}
        self.assertIn("exactly one", self.rejects(heading, user="drop the protein line"))

    def test_the_coachs_numbers_cannot_reach_the_playbook_through_a_vague_reply(self) -> None:
        op = {"op": "change_playbook", "section": "Targets", "old": "Calories: 2,500",
              "new": "Calories: 2,700 kcal every day.", "source": "user", "evidence": "I'm hungry, what should I do?"}
        self.assertIn("2700", self.rejects(op, user="I'm hungry, what should I do?",
                                          assistant="Raise calories to 2,700 a day."))
        short = {**op, "evidence": "I"}
        self.assertIn("at least 12", self.rejects(short, user="I'm hungry, what should I do?",
                                                  assistant="Raise calories to 2,700 a day."))

    def test_profile_numbers_must_be_the_users(self) -> None:
        op = {"op": "set_profile", "section": "Basics", "key": "Height", "value": "183 cm; 95 kg goal.",
              "source": "user", "evidence": "my height is 183 cm"}
        self.assertIn("95", self.rejects(op, user="my height is 183 cm"))

    def test_tripwires_are_set_and_removed_by_the_user(self) -> None:
        op = {"op": "set_tripwire", "id": "Gain Rate", "name": "Gaining too fast", "metric": "weight",
              "kind": "rate", "op_symbol": ">", "threshold": 0.3, "unit": "kg a week", "window_weeks": 3,
              "then": "Cut 100 kcal a day.", "source": "user", "evidence": "yes, 0.3 a week"}
        self.apply(op, user="yes, 0.3 a week is the limit")
        self.assertEqual([wi["id"] for wi in cards.read_tripwires(SLUG)], ["gain-rate"])
        coach_only = {**op, "threshold": 0.4, "evidence": "yes, 0.3 a week is the limit"}
        self.assertIn("0.4", self.rejects(coach_only, user="yes, 0.3 a week is the limit",
                                          assistant="Let's make it 0.4 a week."))
        self.assertIn("window_weeks", self.rejects({**op, "window_weeks": 30}, user="yes, 0.3 a week is the limit"))
        self.apply({"op": "remove_tripwire", "id": "gain-rate", "source": "user", "evidence": "drop that rule"},
                   user="drop that rule")
        self.assertEqual(cards.read_tripwires(SLUG), [])


class PayloadTests(WriterTestCase):
    def test_good_and_bad_ops_are_reported_separately(self) -> None:
        payload = json.dumps({"ops": [
            {"op": "log_metric", "metric": "weight", "value": 81.4, "unit": "kg", "source": "user", "evidence": "81.4 today"},
            {"op": "log_metric", "metric": "weight", "value": 99, "unit": "kg", "source": "user", "evidence": "81.4 today"},
            "not an op",
        ]})
        result = w.apply_payload(SLUG, payload, w.Exchange("81.4 today", "Noted."), now=NOW)
        self.assertEqual(len(result.applied), 1)
        self.assertEqual(len(result.rejected), 2)

    def test_unparseable_payload(self) -> None:
        result = w.apply_payload(SLUG, "no json here", w.Exchange("x", "y"), now=NOW)
        self.assertIsNotNone(result.error)

    def test_prompt_shows_food_ids_and_profile_sections(self) -> None:
        row = ledger.add_food(SLUG, date=TODAY, name="rice", kcal=130, protein=2.7)
        prompt = w.build_prompt(SLUG, w.Exchange("hi", "hello"), now=NOW)
        self.assertIn(f"[{row['id']}] {TODAY}", prompt)
        self.assertIn("Section is one of: Basics, Goals.", prompt)

    def test_full_run_with_a_stubbed_model_and_an_audit_line(self) -> None:
        payload = json.dumps({"ops": [{"op": "log_metric", "metric": "weight", "value": 81.4, "unit": "kg",
                                       "source": "user", "evidence": "81.4 today"}]})
        with patch.object(w.llm, "chat", AsyncMock(return_value=payload)):
            import asyncio
            result = asyncio.run(w.propose_and_apply(SLUG, "81.4 today", "Noted.", writer_model="m", now=NOW))
        self.assertEqual(len(result.applied), 1)
        audit = (self.root / SLUG / "memory_ops.jsonl").read_text().splitlines()
        self.assertEqual(json.loads(audit[-1])["applied"][0]["op"], "log_metric")


class NumbersInTests(unittest.TestCase):
    def test_dates_and_times_are_not_numbers(self) -> None:
        self.assertEqual(w.numbers_in("Calories: 2,300 kcal (set 28 Feb, 08:39, 2026-02-23)."), [2300.0])


if __name__ == "__main__":
    unittest.main()
