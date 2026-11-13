"""Tests for food reference lookups and archive search.

All entries are made up.
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

import config  # noqa: E402
import recall  # noqa: E402

SLUG = "test_agent"
FOODBOOK = [
    {"name": "Corner Cafe small chicken strips", "aliases": ["chicken strips"], "serving": "1 portion",
     "kcal": 183, "protein_g": 36, "updated": "2026-09-01", "note": ""},
    {"name": "Corner Cafe medium protein waffle", "aliases": [], "serving": "1 waffle with syrup",
     "kcal": 610, "protein_g": 41, "updated": "2026-09-30", "note": ""},
    {"name": "Iced mocha", "aliases": ["mocha"], "serving": "1 cup", "kcal": 230, "protein_g": 27,
     "updated": "2026-10-12", "note": ""},
    {"name": "Whey", "aliases": ["protein shake"], "serving": "1 scoop", "kcal": 120, "protein_g": 24,
     "updated": "2026-09-15", "note": "mixed with water"},
]


class TokenTests(unittest.TestCase):
    def test_stopwords_dropped_and_plurals_folded(self) -> None:
        self.assertEqual(recall.tokens("Add the chicken strips I had today"), ["chicken", "strip"])
        self.assertEqual(recall.tokens("User's whey: 2 scoops"), ["whey", "scoop"])


class FoodTests(unittest.TestCase):
    def test_exact_name_and_alias(self) -> None:
        names = [f["name"] for f in recall.match_foods(FOODBOOK, "had an iced mocha and a protein shake")]
        self.assertEqual(set(names), {"Iced mocha", "Whey"})

    def test_partial_name_needs_two_words(self) -> None:
        names = [f["name"] for f in recall.match_foods(FOODBOOK, "breakfast was the corner cafe waffle")]
        self.assertEqual(names[0], "Corner Cafe medium protein waffle")
        self.assertEqual(recall.match_foods(FOODBOOK, "I went to a cafe"), [])

    def test_unrelated_message_matches_nothing(self) -> None:
        self.assertEqual(recall.match_foods(FOODBOOK, "bench went well, 60 kg for 8"), [])

    def test_food_line(self) -> None:
        self.assertEqual(recall.food_line(FOODBOOK[3]), "- Whey (1 scoop): 120 kcal, 24 g protein. Note: mixed with water")
        self.assertEqual(
            recall.food_line({"name": "Mujadara", "kcal": None, "protein_g": None}),
            "- Mujadara: no values recorded.",
        )


class ArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = patch.object(config, "DATA_DIR", Path(tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.folder = Path(tmp.name) / SLUG / "archive"
        self.folder.mkdir(parents=True)
        filler = "".join(
            f"- [2026-02-{1 + i % 28:02d}] User says the session number {i} went fine. _(user · workout)_\n"
            for i in range(200)
        )
        (self.folder / "old_memory.md").write_text(
            "## Plans\n\n"
            "- [2026-03-31] User attended a photography workshop on a rooftop. _(user · logistics)_\n"
            "- [2026-04-02] Bring a towel and a water bottle to training. _(assistant · logistics)_\n"
            "- [2026-05-01] Eat more fibre at breakfast. _(assistant · nutrition)_\n"
            "- [2026-05-02] Eat more fibre at lunch. _(assistant · nutrition)_\n"
            "- [2026-05-03] Take fibre capsules at dinner. _(assistant · nutrition)_\n" + filler
        )
        (self.folder / "expired_notes.jsonl").write_text(
            json.dumps({"created": "2026-01-14T08:22:00+08:00", "bullet": "- [2026-01-14 08:22] Jan 14 belly tape at the navel reads 95 cm."}) + "\n"
            + json.dumps({"date": "2026-01-20", "user": "today's totals?", "assistant": "500 calories so far."}) + "\n"
        )
        recall._cache.clear()

    def test_word_forms_match_and_a_very_rare_word_is_enough(self) -> None:
        hits = recall.search_archive(SLUG, "what did the photographer say?")
        self.assertEqual([h.date for h in hits], ["2026-03-31"])
        self.assertEqual(hits[0].origin, "old_memory.md")

    def test_common_words_alone_match_nothing(self) -> None:
        self.assertEqual(recall.search_archive(SLUG, "how did the session go, user says?"), [])

    def test_one_shared_word_that_is_not_very_rare_is_not_enough(self) -> None:
        self.assertEqual(recall.search_archive(SLUG, "fibre"), [])

    def test_two_shared_words_match_and_newer_wins_ties(self) -> None:
        hits = recall.search_archive(SLUG, "eat more fibre", limit=5)
        self.assertEqual([h.date for h in hits], ["2026-05-02", "2026-05-01"])

    def test_notes_and_chat_rows_are_searchable(self) -> None:
        self.assertEqual(recall.search_archive(SLUG, "navel tape reading")[0].date, "2026-01-14")
        self.assertIn("500 calories", recall.search_archive(SLUG, "calories totals")[0].text)

    def test_empty_archive_or_query(self) -> None:
        self.assertEqual(recall.search_archive("nobody", "photography"), [])
        self.assertEqual(recall.search_archive(SLUG, "the and of"), [])


if __name__ == "__main__":
    unittest.main()
