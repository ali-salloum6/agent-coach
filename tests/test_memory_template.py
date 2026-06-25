"""Tests for canonical Markdown layout and recent-memory pruning."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import memory_template as mt  # noqa: E402


NOW = datetime(2026, 5, 13, 10, 0, tzinfo=timezone.utc)


class CanonicalTemplateTests(unittest.TestCase):
    def test_template_has_all_sections(self) -> None:
        md = mt.canonical_template("gym_coach")
        for section in mt.CANONICAL_SECTIONS:
            self.assertIn(f"## {section}", md)
        for sub in mt.TIME_SERIES_DEFAULT_SUBSECTIONS:
            self.assertIn(f"### {sub}", md)

    def test_append_routes_to_time_series_subsection(self) -> None:
        md = mt.canonical_template("gym_coach")
        new_md = mt.append_under_heading(
            md,
            section="Time Series",
            subsection="Weight",
            body="- [2026-05-13] Weight 95.1 kg.\n",
        )
        # The Weight subsection now contains the bullet, and other sections
        # are untouched.
        weight_idx = new_md.index("### Weight")
        next_sub_idx = new_md.index("### Sleep")
        self.assertIn(
            "- [2026-05-13] Weight 95.1 kg.",
            new_md[weight_idx:next_sub_idx],
        )

    def test_append_creates_missing_subsection(self) -> None:
        md = mt.canonical_template("gym_coach")
        new_md = mt.append_under_heading(
            md,
            section="Time Series",
            subsection="Custom Category",
            body="- [2026-05-13] First entry.\n",
        )
        self.assertIn("### Custom Category", new_md)
        self.assertIn("- [2026-05-13] First entry.", new_md)

    def test_append_creates_missing_top_section(self) -> None:
        md = "# title\n\n"
        new_md = mt.append_under_heading(
            md,
            section="Timeline",
            subsection=None,
            body="- [2026-05-13] First event.\n",
        )
        self.assertIn("## Timeline", new_md)
        self.assertIn("- [2026-05-13] First event.", new_md)

    def test_time_series_subsection_routing(self) -> None:
        self.assertEqual(mt.time_series_subsection("weight"), "Weight")
        self.assertEqual(mt.time_series_subsection("mental_health"), "Mental Health")
        self.assertEqual(mt.time_series_subsection("unknown_cat"), "Unknown Cat")


class RecentEntryTests(unittest.TestCase):
    def test_entry_render_round_trip(self) -> None:
        entry = mt.build_recent_entry(
            summary="User ate a banana.",
            category="nutrition",
            source="user",
            ttl_days=2,
            now=NOW,
        )
        rendered = entry.render()
        self.assertIn("<!-- recent:", rendered)
        self.assertIn("ttl_days=2", rendered)
        self.assertIn("category=nutrition", rendered)
        self.assertIn("- [", rendered)
        self.assertIn("expires", rendered)
        self.assertIn("User ate a banana.", rendered)

    def test_prune_expired_drops_old_entries(self) -> None:
        recent_fresh = mt.build_recent_entry(
            summary="Fresh note.",
            category="nutrition",
            source="user",
            ttl_days=2,
            now=NOW,
        )
        recent_old = mt.build_recent_entry(
            summary="Old note from days ago.",
            category="logistics",
            source="user",
            ttl_days=1,
            now=NOW - timedelta(days=4),
        )
        content = (
            mt.recent_template("gym_coach")
            + "\n"
            + recent_old.render()
            + "\n"
            + recent_fresh.render()
        )
        pruned, count, archived = mt.prune_expired(content, now=NOW)
        self.assertEqual(count, 1)
        self.assertEqual(len(archived), 1)
        self.assertIn("Old note from days ago.", archived[0]["bullet"])
        self.assertNotIn("Old note from days ago.", pruned)
        self.assertIn("Fresh note.", pruned)

    def test_prune_keeps_unmarked_lines(self) -> None:
        content = (
            mt.recent_template("gym_coach")
            + "\n- This bullet has no machine metadata; keep it.\n"
        )
        pruned, count, archived = mt.prune_expired(content, now=NOW)
        self.assertEqual(count, 0)
        self.assertEqual(archived, [])
        self.assertIn("This bullet has no machine metadata", pruned)

    def test_prune_keeps_missing_or_invalid_expires(self) -> None:
        content = (
            mt.recent_template("gym_coach")
            + "\n<!-- recent: created=2026-05-01T10:00:00+03:00 category=workout source=user -->\n"
            + "- [2026-05-01 10:00 Moscow | workout · user] No expires field.\n"
            + "\n<!-- recent: created=2026-05-01T11:00:00+03:00 expires=not-a-date ttl_days=7 category=workout source=user -->\n"
            + "- [2026-05-01 11:00 Moscow | workout · user] Bad expires.\n"
        )
        pruned, count, archived = mt.prune_expired(content, now=NOW)
        self.assertEqual(count, 0)
        self.assertIn("No expires field.", pruned)
        self.assertIn("Bad expires.", pruned)

    def test_prune_skips_blank_lines_before_bullet(self) -> None:
        recent_old = mt.build_recent_entry(
            summary="Expired with gap.",
            category="logistics",
            source="user",
            ttl_days=1,
            now=NOW - timedelta(days=4),
        )
        content = mt.recent_template("gym_coach") + "\n" + recent_old.render().replace(
            "\n-", "\n\n\n-"
        )
        pruned, count, archived = mt.prune_expired(content, now=NOW)
        self.assertEqual(count, 1)
        self.assertNotIn("Expired with gap.", pruned)
        self.assertIn("Expired with gap.", archived[0]["bullet"])

    def test_prune_recent_file_archives_removed_entries(self) -> None:
        import tempfile

        recent_old = mt.build_recent_entry(
            summary="Archive me.",
            category="nutrition",
            source="assistant",
            ttl_days=1,
            now=NOW - timedelta(days=3),
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gym_coach.recent.md"
            path.write_text(mt.recent_template("gym_coach") + "\n" + recent_old.render())
            count = mt.prune_recent_file(path, now=NOW)
            archive = path.with_name("gym_coach.recent.pruned.jsonl")
            self.assertEqual(count, 1)
            self.assertTrue(archive.exists())
            self.assertIn("Archive me.", archive.read_text(encoding="utf-8"))
            self.assertNotIn("Archive me.", path.read_text(encoding="utf-8"))

    def test_visible_recent_strips_machine_comments(self) -> None:
        entry = mt.build_recent_entry(
            summary="Fresh note.",
            category="nutrition",
            source="user",
            ttl_days=2,
            now=NOW,
        )
        content = mt.recent_template("gym_coach") + "\n" + entry.render()
        visible = mt.visible_recent(content, now=NOW)
        self.assertNotIn("<!-- recent:", visible)
        self.assertIn("Fresh note.", visible)


if __name__ == "__main__":
    unittest.main()
