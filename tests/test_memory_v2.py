"""Tests for what the coach sees in memory v2, the weekly review, and the bot wiring.

All data is made up.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("OPENROUTER_API_KEY", "test-key")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot  # noqa: E402
import cards  # noqa: E402
import config  # noqa: E402
import ledger  # noqa: E402
import memory_template  # noqa: E402
import memory_v2  # noqa: E402
import review  # noqa: E402

SLUG = "gym_coach"
NOW = datetime(2026, 2, 23, 14, 0, tzinfo=config.MEMORY_TIMEZONE)
TODAY = "2026-02-23"
STORY = """# Story

## Chapter 1: The cut (closed)
- Lost 10 kg.

## Chapter 2: The build, since 26 January 2026 (open)
- Start: 84.3 kg.
- Week 1, 26 Jan to 1 Feb: hand-written line.
- Week 3, 9 Feb to 15 Feb: hand-written line.
- So far: old line.
"""


class V2TestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for target, value in ((config, ("DATA_DIR", self.root)), (config, ("MEMORY_VERSION", "2"))):
            patcher = patch.object(target, *value)
            patcher.start()
            self.addCleanup(patcher.stop)
        memory_v2.write_phase(SLUG, {"name": "build", "start": "2026-01-26"})
        cards.write_card(SLUG, cards.PROFILE, "# Profile\n- **Location**: Porto.")
        cards.write_card(SLUG, cards.PLAYBOOK, "# Playbook\n## Targets\n- Calories: 2,500 kcal.")
        cards.write_card(SLUG, cards.STORY, STORY)
        cards.write_card(SLUG, cards.THREADS, "# Open threads\n## Sleep\n- 23:00 bedtime.")
        for d, v in [("2026-01-26", 84.3), ("2026-02-02", 84.6), ("2026-02-09", 84.9), ("2026-02-16", 85.2)]:
            ledger.add_metric(SLUG, date=d, metric="weight", value=v, unit="kg")
        ledger.add_metric(SLUG, date="2026-02-16", metric="belly", value=92.0, unit="cm")
        ledger.add_lift(SLUG, date="2026-02-10", exercise="bench press", load=70, unit="kg", reps=[8, 6])
        ledger.add_lift(SLUG, date="2026-02-18", exercise="bench press", load=72.5, unit="kg", reps=[6, 5])
        ledger.set_day_total(SLUG, date="2026-02-17", kcal=2500, protein=150)
        ledger.set_day_total(SLUG, date="2026-02-18", kcal=2300, protein=140)
        ledger.add_event(SLUG, date="2026-02-19", kind="health", text="Booked the blood test.")


class ViewTests(V2TestCase):
    def test_stable_memory_is_the_four_cards_in_order(self) -> None:
        text = memory_v2.stable_memory(SLUG)
        order = [text.index(h) for h in ("# Profile", "# Playbook", "# Story", "# Open threads")]
        self.assertEqual(order, sorted(order))

    def test_live_block_has_phase_numbers_pending_notes_and_matches(self) -> None:
        memory_v2.add_pending(SLUG, {"date": TODAY, "section": "Targets", "old": "Calories: 2,500",
                                     "new": "Calories: 2,400 kcal.", "why": "fast gain"})
        memory_template.append_recent_entry(memory_v2.recent_path(SLUG), memory_template.build_recent_entry(
            summary="Gym closed on Friday.", category="logistics", source="user", ttl_days=7, now=NOW))
        (self.root / SLUG / "foodbook.jsonl").write_text(json.dumps(
            {"name": "Corner Cafe wrap", "aliases": [], "serving": "1 wrap", "kcal": 540, "protein_g": 38}) + "\n")
        live = memory_v2.live_context(SLUG, "had the corner cafe wrap", now=NOW)
        self.assertIn("Phase: build, week 5 (since 26 Jan).", live)
        self.assertIn("Weight (fasted, home scale): 85.2 kg on 16 Feb.", live)
        self.assertIn('- [p1] Targets: replace "Calories: 2,500" with "Calories: 2,400 kcal."', live)
        self.assertIn("- 23 Feb 14:00: Gym closed on Friday.", live)
        self.assertIn("- Corner Cafe wrap (1 wrap): 540 kcal, 38 g protein.", live)

    def test_live_block_goes_into_the_request_only(self) -> None:
        history = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
        sent = memory_v2.with_live_context(history, "LIVE")
        self.assertEqual(sent[-1]["content"], "LIVE\n\n---\n\nhi")
        self.assertEqual(history[-1]["content"], "hi")
        parts = [{"type": "text", "text": "photo"}]
        sent = memory_v2.with_live_context([{"role": "user", "content": parts}], "LIVE")
        self.assertEqual(sent[-1]["content"][0], {"type": "text", "text": "LIVE"})
        self.assertEqual(len(parts), 1)

    def test_phase_week(self) -> None:
        phase = {"start": "2026-01-26"}
        self.assertEqual(memory_v2.phase_week(phase, "2026-01-26"), 1)
        self.assertEqual(memory_v2.phase_week(phase, "2026-02-01"), 1)
        self.assertEqual(memory_v2.phase_week(phase, "2026-02-02"), 2)
        self.assertIsNone(memory_v2.phase_week(phase, "2026-01-01"))


class ReviewTests(V2TestCase):
    def test_week_numbering_follows_the_phase_not_the_weekday(self) -> None:
        self.assertEqual(review.last_finished_week("2026-02-23", "2026-01-26"), (4, "2026-02-16", "2026-02-22"))
        # A review on Friday refers to the same finished week, not the 7 days before it.
        self.assertEqual(review.last_finished_week("2026-02-27", "2026-01-26"), (4, "2026-02-16", "2026-02-22"))
        self.assertEqual(review.last_finished_week("2026-02-01", "2026-01-26"), None)

    def test_protected_week_lines_are_never_replaced_or_folded(self) -> None:
        memory_v2.write_phase(SLUG, {"name": "build", "start": "2026-01-26", "protect_through_week": 3})
        review.run(SLUG, today="2026-02-16")  # week 3 finished: protected
        story = cards.read_card(SLUG, cards.STORY)
        self.assertIn("- Week 3, 9 Feb to 15 Feb: hand-written line.", story)
        self.assertIn("- So far: weight +0.9 kg", story)
        folded = review.condense_story(SLUG, story, today="2026-02-16", limit=1, phase_start="2026-01-26",
                                       protect_through=3)
        self.assertIn("hand-written line", folded)

    def test_no_open_chapter_or_phase_still_gives_a_checkin(self) -> None:
        cards.write_card(SLUG, cards.STORY, "# Story\n\n## Chapter 2: The build (closed)\n- Done.")
        result = review.run(SLUG, today=TODAY)
        self.assertFalse(result["story_updated"])
        self.assertTrue(result["text"].startswith("Week 4 check-in"))
        (self.root / SLUG / "cards" / "phase.json").unlink()
        text = review.run(SLUG, today=TODAY)["text"]
        self.assertTrue(text.startswith("Check-in for the last 7 days (16 Feb to 22 Feb)"))

    def test_catch_up_only_after_a_first_review(self) -> None:
        self.assertFalse(review.is_due(SLUG, today=TODAY))
        review.mark_reviewed(SLUG, 3, "2026-02-16")
        self.assertTrue(review.is_due(SLUG, today=TODAY))
        review.run(SLUG, today=TODAY)
        self.assertFalse(review.is_due(SLUG, today="2026-02-27"))
        self.assertTrue(review.is_due(SLUG, today="2026-03-02"))

    def test_full_cards_are_flagged_in_the_checkin(self) -> None:
        cards.write_card(SLUG, cards.THREADS, "# Open threads\n" + "x" * int(cards.BUDGETS[cards.THREADS] * 0.95))
        self.assertIn("the threads card is 95% full", review.checkin_text(SLUG, today=TODAY))

    def test_many_readings_make_a_short_line(self) -> None:
        for day in range(16, 23):
            ledger.add_metric(SLUG, date=f"2026-02-{day}", metric="weight", value=85 + day / 100, unit="kg")
        line = review.week_line(SLUG, 4, "2026-02-16", "2026-02-22", today=TODAY)
        self.assertIn("weight 85.16 to 85.22 kg over 7 readings", line)

    def test_week_line_from_the_logs(self) -> None:
        line = review.week_line(SLUG, 4, "2026-02-16", "2026-02-22", today=TODAY)
        self.assertEqual(
            line,
            "- Week 4, 16 Feb to 22 Feb: weight 85.2 kg on 16 Feb; navel 92 cm on 16 Feb; "
            "bench 72.5 kg × 6, 5 on 18 Feb; new bests: bench press 72.5 kg × 6; 1 training day logged; "
            "food about 2,400 kcal and 145 g protein a day over 2 logged days; 19 Feb: Booked the blood test.",
        )

    def test_story_gets_the_week_in_order_and_a_fresh_so_far_line(self) -> None:
        result = review.run(SLUG, today=TODAY)
        self.assertTrue(result["story_updated"])
        story = cards.read_card(SLUG, cards.STORY)
        weeks = [line[:9] for line in story.splitlines() if line.startswith("- Week")]
        self.assertEqual(weeks, ["- Week 1,", "- Week 3,", "- Week 4,"])
        self.assertIn("- So far: weight +0.9 kg since the start (84.3 to 85.2 kg); navel between 92 and 92 cm; "
                      "best bench 72.5 kg × 6.", story)
        self.assertNotIn("old line", story)
        self.assertIn("## Chapter 1: The cut (closed)\n- Lost 10 kg.", story)

    def test_rerunning_the_same_week_replaces_its_line(self) -> None:
        review.run(SLUG, today=TODAY)
        review.run(SLUG, today=TODAY)
        story = cards.read_card(SLUG, cards.STORY)
        self.assertEqual(story.count("- Week 4,"), 1)

    def test_checkin_text_has_rules_and_a_reminder(self) -> None:
        cards.tripwires_path(SLUG).write_text(json.dumps([{
            "id": "navel", "name": "Navel at 91 cm", "metric": "belly", "kind": "level", "op": ">=",
            "threshold": 91, "unit": "cm", "consecutive": 1, "then": "Stop the surplus."}]))
        text = review.checkin_text(SLUG, today=TODAY)
        self.assertTrue(text.startswith("Week 4 check-in (16 Feb to 22 Feb)"))
        self.assertIn("- Navel at 91 cm: TRIPPED; last reading: 92 on 16 Feb (trips at or above 91 cm). The rule says: Stop the surplus.", text)
        self.assertIn("No weigh-in or navel tape logged today yet.", text)
        ledger.add_metric(SLUG, date=TODAY, metric="weight", value=85.0, unit="kg")
        ledger.add_metric(SLUG, date=TODAY, metric="belly", value=91.5, unit="cm")
        self.assertNotIn("logged today yet", review.checkin_text(SLUG, today=TODAY))

    def test_long_story_folds_old_weeks_into_months(self) -> None:
        lines = STORY.replace("- Week 1, 26 Jan to 1 Feb: hand-written line.\n- Week 3, 9 Feb to 15 Feb: hand-written line.\n", "")
        weeks = "".join(f"- Week {n}, {d} Jan to {d + 6} Jan: {'x' * 300}.\n" for n, d in ((1, 1), (2, 8), (3, 15), (4, 22)))
        weeks += "".join(f"- Week {n}, {d} Feb to {d + 6} Feb: {'y' * 300}.\n" for n, d in ((5, 1), (6, 8), (7, 15)))
        weeks += "".join(f"- Week {n}, {d} Mar to {d + 6} Mar: {'z' * 300}.\n" for n, d in ((8, 1), (9, 8), (10, 15), (11, 22)))
        story = lines.replace("- So far: old line.\n", weeks + "- So far: old line.\n")
        folded = review.condense_story(SLUG, story, today="2026-03-30", limit=len(story) - 500,
                                       phase_start="2026-01-01")
        # Weeks whose first day is in January (1 to 5, by the phase start) fold into one line.
        self.assertEqual(folded.count("x" * 300), 0)
        self.assertIn("- Weeks 1 to 5, 1 Jan to 4 Feb: ", folded)
        self.assertEqual(sum(1 for line in folded.splitlines() if line.startswith("- Week ")), 6)
        self.assertLessEqual(len(folded), len(story) - 500)
        kept = review.condense_story(SLUG, folded, today="2026-03-30", limit=1, phase_start="2026-01-01")
        self.assertEqual(sum(1 for line in kept.splitlines() if line.startswith("- Week ")), review.KEEP_WEEK_LINES)
        self.assertEqual([line[:22] for line in kept.splitlines() if line.startswith("- Weeks")],
                         ["- Weeks 1 to 7, 1 Jan "])


class BotWiringTests(V2TestCase):
    def test_request_carries_the_live_block_and_history_does_not(self) -> None:
        history = [{"role": "system", "content": "sys"}, {"role": "user", "content": "[Sent at x]\n\nhow am I doing?"}]
        update = SimpleNamespace(effective_chat=SimpleNamespace(id=42))
        with patch.dict(bot.session, {"history": history}):
            sent = bot._request_messages(update, "how am I doing?")
        self.assertIn("[Live data, computed from the logs", sent[-1]["content"])
        self.assertTrue(sent[-1]["content"].endswith("how am I doing?"))
        self.assertEqual(history[-1]["content"], "[Sent at x]\n\nhow am I doing?")
        self.assertEqual(memory_v2.chat_id(SLUG), 42)

    def test_v1_sends_the_history_unchanged(self) -> None:
        history = [{"role": "user", "content": "hi"}]
        with patch.object(config, "MEMORY_VERSION", "1"), patch.dict(bot.session, {"history": history}):
            self.assertEqual(bot._request_messages(SimpleNamespace(effective_chat=None), "hi"), history)

    def test_memory_step_goes_to_the_v2_writer(self) -> None:
        with patch.object(bot.writer_v2, "propose_and_apply", AsyncMock()) as v2, \
                patch.object(bot.memory, "extract_and_save", AsyncMock()) as v1:
            asyncio.run(bot._background_extract("u", "a", "m", has_attachment=True))
        v2.assert_awaited_once()
        self.assertTrue(v2.await_args.kwargs["has_attachment"])
        v1.assert_not_awaited()

    def test_new_conversation_uses_the_cards(self) -> None:
        with patch.dict(bot.session, {"history": [], "model": "m", "web_search": False, "tts": False}), \
                patch.object(bot, "_persist_session"), patch.object(bot.session_store, "clear"):
            bot._reset_session()
            prompt = bot.session["history"][0]["content"]
        self.assertIn("- **Location**: Porto.", prompt)
        self.assertIn("four cards", prompt)

    def test_checkin_is_sent_and_kept_in_the_history(self) -> None:
        sent = AsyncMock()
        with patch.dict(bot.session, {"history": [{"role": "system", "content": "sys"}]}), \
                patch.object(bot, "_persist_session"), patch.object(bot.ledger, "today_iso", return_value=TODAY):
            asyncio.run(bot._send_checkin(SimpleNamespace(send_message=sent), 42))
            last = bot.session["history"][-1]
        self.assertEqual(sent.await_args.args[0], 42)
        self.assertEqual(last["role"], "assistant")
        self.assertTrue(last["content"].startswith("Week 4 check-in"))


if __name__ == "__main__":
    unittest.main()
