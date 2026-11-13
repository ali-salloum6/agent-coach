"""Tests for changing phase in memory v2. All data is made up."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("OPENROUTER_API_KEY", "test-key")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cards  # noqa: E402
import config  # noqa: E402
import ledger  # noqa: E402
import memory_v2  # noqa: E402
import phases  # noqa: E402
import review  # noqa: E402
import writer_v2 as w  # noqa: E402

SLUG = "gym_coach"
TODAY = "2026-02-23"
NOW = datetime(2026, 2, 23, 9, 0, tzinfo=config.MEMORY_TIMEZONE)
STORY = """# Story

## Chapter 1: The cut, 1 March to 20 December 2025 (closed)
- Lost 10 kg over the year.

## Chapter 2: The build, since 26 January 2026 (open)
- Start, 26 Jan: 84.3 kg.
- Week 1, 26 Jan to 1 Feb: approved hand-written line.
- Week 2, 2 Feb to 8 Feb: computed line.
- Week 3, 9 Feb to 15 Feb: computed line.
- So far: old line.
"""
PLAYBOOK = """# Playbook

## Phase
- Build, since 26 Jan 2026.

## Targets
- Calories: 2,500 kcal every day.
"""


class PhaseTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for name, value in (("DATA_DIR", self.root), ("MEMORY_VERSION", "2")):
            patcher = patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        memory_v2.write_phase(SLUG, {"name": "build", "title": "The build", "start": "2026-01-26",
                                     "protect_through_week": 1})
        cards.write_card(SLUG, cards.STORY, STORY)
        cards.write_card(SLUG, cards.PLAYBOOK, PLAYBOOK)
        for d, v in [("2026-01-26", 84.3), ("2026-02-02", 84.6), ("2026-02-09", 84.9), ("2026-02-16", 85.2),
                     ("2026-02-22", 85.4)]:
            ledger.add_metric(SLUG, date=d, metric="weight", value=v, unit="kg")
        ledger.add_metric(SLUG, date="2026-02-22", metric="belly", value=92.0, unit="cm")
        ledger.add_lift(SLUG, date="2026-02-10", exercise="bench press", load=70, unit="kg", reps=[8, 6])


class StartPhaseTests(PhaseTestCase):
    def test_closes_the_chapter_and_opens_the_next(self) -> None:
        phases.start_phase(SLUG, name="cut", start=TODAY, plan="2,000 kcal a day and 160 g protein", today=TODAY)
        story = cards.read_card(SLUG, cards.STORY)
        self.assertIn("## Chapter 2: The build, 26 January 2026 to 22 February 2026 (closed)", story)
        self.assertIn("- Week 1, 26 Jan to 1 Feb: approved hand-written line.", story)
        self.assertIn("- Weeks 2 to 3, 2 Feb to 15 Feb: ", story)
        self.assertIn("- Summary, 26 Jan to 22 Feb (about 4 weeks): weight 84.3 to 85.4 kg over 5 readings", story)
        self.assertNotIn("old line", story)
        self.assertIn("## Chapter 3: Cut, since 23 February 2026 (open)", story)
        self.assertIn("- Start, 23 Feb: weight 85.4 kg (22 Feb); navel 92 cm (22 Feb).", story)
        self.assertIn("- Plan: 2,000 kcal a day and 160 g protein.", story)
        self.assertTrue(story.rstrip().endswith("- So far: no readings yet."))

    def test_phase_file_playbook_note_and_week_count(self) -> None:
        review.mark_reviewed(SLUG, 4, "2026-02-23")
        phases.start_phase(SLUG, name="cut", start=TODAY, today=TODAY)
        phase = memory_v2.read_phase(SLUG)
        self.assertEqual((phase["name"], phase["title"], phase["start"], phase["protect_through_week"]),
                         ("cut", "Cut", TODAY, 0))
        self.assertEqual(phase["history"], [{"name": "build", "title": "The build", "start": "2026-01-26",
                                             "end": "2026-02-22"}])
        playbook = cards.read_card(SLUG, cards.PLAYBOOK)
        self.assertIn("## Phase\n- Cut, since 23 Feb 2026.\n- Previous phase: The build, 26 Jan to 22 Feb;", playbook)
        self.assertIn("- Calories: 2,500 kcal every day.", playbook)
        self.assertIn("Phase changed to cut on 23 Feb", memory_v2.recent_notes(SLUG, NOW))
        self.assertEqual(review.last_reviewed_week(SLUG), 0)
        self.assertEqual(memory_v2.phase_week(phase, "2026-03-02"), 2)

    def test_a_phase_cannot_start_before_the_current_one(self) -> None:
        with self.assertRaises(ValueError):
            phases.start_phase(SLUG, name="cut", start="2026-01-20", today=TODAY)


class ScheduleTests(PhaseTestCase):
    def test_future_start_waits_for_its_day(self) -> None:
        phases.schedule_phase(SLUG, name="cut", start="2026-03-02", plan="", today=TODAY)
        self.assertIsNone(phases.apply_due(SLUG, today="2026-03-01"))
        live = memory_v2.live_context(SLUG, "hi", now=NOW)
        self.assertIn("Next phase: cut from 2 Mar, agreed 23 Feb.", live)
        self.assertEqual(phases.apply_due(SLUG, today="2026-03-02"), "phase cut from 2026-03-02")
        phase = memory_v2.read_phase(SLUG)
        self.assertEqual((phase["name"], phase["start"]), ("cut", "2026-03-02"))
        self.assertNotIn("next", phase)
        self.assertIsNone(phases.apply_due(SLUG, today="2026-03-03"))


class WriterOpTests(PhaseTestCase):
    def apply(self, op: dict, user: str, assistant: str = "") -> str:
        return w.apply_op(SLUG, op, w.Exchange(user, assistant), now=NOW)

    def test_the_users_decision_starts_today(self) -> None:
        op = {"op": "change_phase", "name": "Cut", "start": TODAY, "plan": "2,000 kcal a day",
              "source": "user", "evidence": "let's start the cut today"}
        self.assertEqual(self.apply(op, "let's start the cut today", "Cut at 2,000 kcal a day."), "phase cut from 2026-02-23")

    def test_a_later_start_is_scheduled(self) -> None:
        op = {"op": "change_phase", "name": "cut", "start": "2026-03-02", "source": "user",
              "evidence": "we start the cut next Monday"}
        self.assertEqual(self.apply(op, "we start the cut next Monday"), "phase cut scheduled for 2026-03-02")

    def test_checks(self) -> None:
        base = {"op": "change_phase", "name": "cut", "start": TODAY, "source": "user",
                "evidence": "let's start the cut today"}
        user = "let's start the cut today"
        for op, reason in (
            ({**base, "source": "assistant"}, "user's own words"),
            ({**base, "start": "2026-05-01"}, "within"),
            ({**base, "start": "2026-01-20"}, "within"),
            ({**base, "name": "cut!!"}, "short phase name"),
            ({**base, "plan": "1,800 kcal a day"}, "1800"),
        ):
            with self.assertRaises(w.Rejected) as ctx:
                self.apply(op, user, "Sounds good.")
            self.assertIn(reason, str(ctx.exception))


class ArchiveTests(PhaseTestCase):
    def test_oldest_closed_chapter_moves_to_the_archive(self) -> None:
        ledger.add_metric(SLUG, date="2025-03-01", metric="weight", value=95.0, unit="kg")
        ledger.add_metric(SLUG, date="2025-12-20", metric="weight", value=85.0, unit="kg")
        story = review.archive_oldest_chapter(SLUG, STORY, today=TODAY)
        self.assertIn("## Chapter 1: The cut, 1 March to 20 December 2025 (closed)\n\n"
                      "- Summary: weight 95 to 85 kg over 2 readings.\n- The full chapter is in the archive.", story)
        archive = (self.root / SLUG / "archive" / "story_chapters.md").read_text()
        self.assertIn("- Lost 10 kg over the year.", archive)
        self.assertIsNone(review.archive_oldest_chapter(SLUG, story, today=TODAY))

    def test_a_full_story_archives_old_chapters_before_failing(self) -> None:
        big = STORY.replace("- Lost 10 kg over the year.", "- Lost 10 kg over the year. " + "x" * 3000)
        story = review.condense_story(SLUG, big, today=TODAY, limit=len(big) - 2000, phase_start="2026-01-26",
                                      protect_through=1)
        self.assertNotIn("x" * 3000, story)
        self.assertIn("- The full chapter is in the archive.", story)
        self.assertIn("approved hand-written line", story)


if __name__ == "__main__":
    unittest.main()
