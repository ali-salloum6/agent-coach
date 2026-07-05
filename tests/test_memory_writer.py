"""End-to-end tests for the structured memory writer with a stubbed LLM."""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import memory_template  # noqa: E402
import memory_writer  # noqa: E402


MOSCOW = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 5, 13, 12, 0, tzinfo=MOSCOW)


class MemoryWriterTestBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._data_dir_patch = patch.object(config, "DATA_DIR", Path(self._tmp.name))
        self._data_dir_patch.start()

    def tearDown(self) -> None:
        self._data_dir_patch.stop()
        self._tmp.cleanup()

    def _canonical(self, slug: str = "gym_coach") -> Path:
        return Path(self._tmp.name) / f"{slug}.md"

    def _recent(self, slug: str = "gym_coach") -> Path:
        return Path(self._tmp.name) / f"{slug}.recent.md"

    def _audit(self, slug: str = "gym_coach") -> Path:
        return Path(self._tmp.name) / f"{slug}.memory_ops.jsonl"


def _llm_returns(payload: dict):
    """Helper that returns an awaitable producing the given JSON payload."""
    text = json.dumps(payload)

    async def _fake(**kwargs):
        return text

    return _fake


class RoutingTests(MemoryWriterTestBase):
    async def test_measurement_routes_to_time_series_weight(self) -> None:
        user = "I weighed 95.2 kg this morning."
        asst = "Nice, on track."
        payload = {
            "ops": [
                {
                    "op": "add_measurement",
                    "source": "user",
                    "date": "2026-05-13",
                    "category": "weight",
                    "value": 95.2,
                    "unit": "kg",
                    "summary": "Weight 95.2 kg.",
                    "evidence_quote": "I weighed 95.2 kg",
                    "retention": "long_term",
                }
            ]
        }
        with patch("memory_writer.llm.propose_memory_ops", _llm_returns(payload)):
            result = await memory_writer.propose_and_apply(
                "gym_coach", user, asst, timestamp=NOW, writer_model=config.DEFAULT_MODEL
            )
        self.assertEqual(result.accepted, 1)
        canonical = self._canonical().read_text(encoding="utf-8")
        weight_section = canonical[canonical.index("### Weight"):]
        weight_section = weight_section[: weight_section.index("### Sleep")]
        self.assertIn("Weight 95.2 kg.", weight_section)
        # No recent file growth for long-term measurement.
        recent = self._recent().read_text(encoding="utf-8")
        self.assertNotIn("Weight 95.2", recent)

    async def test_recent_note_writes_to_recent_with_expiry(self) -> None:
        user = "Ate a banana with peanut butter."
        asst = "Fine for a quick snack."
        payload = {
            "ops": [
                {
                    "op": "add_recent_note",
                    "source": "user",
                    "category": "nutrition",
                    "summary": "Ate a banana with peanut butter.",
                    "evidence_quote": "Ate a banana with peanut butter.",
                    "retention": "recent",
                    "ttl_days": 2,
                }
            ]
        }
        with patch("memory_writer.llm.propose_memory_ops", _llm_returns(payload)):
            result = await memory_writer.propose_and_apply(
                "gym_coach", user, asst, timestamp=NOW, writer_model=config.DEFAULT_MODEL
            )
        self.assertEqual(result.accepted, 1)
        recent = self._recent().read_text(encoding="utf-8")
        self.assertIn("<!-- recent:", recent)
        self.assertIn("Ate a banana with peanut butter.", recent)
        self.assertIn("ttl_days=2", recent)


class AntiFloodAndRetentionTests(MemoryWriterTestBase):
    async def test_transient_assistant_plan_lands_in_recent(self) -> None:
        user = "Lower back is sore today."
        asst = "Plan for today: skip squats and walk 20 min."
        payload = {
            "ops": [
                {
                    "op": "add_plan",
                    "source": "assistant",
                    "category": "workout",
                    "summary": "Skip squats today; walk 20 min.",
                    "evidence_quote": "Plan for today: skip squats and walk 20 min.",
                    "retention": "long_term",
                }
            ]
        }
        with patch("memory_writer.llm.propose_memory_ops", _llm_returns(payload)):
            await memory_writer.propose_and_apply(
                "gym_coach", user, asst, timestamp=NOW, writer_model=config.DEFAULT_MODEL
            )
        canonical = self._canonical().read_text(encoding="utf-8")
        recent = self._recent().read_text(encoding="utf-8")
        # Plan should NOT appear in long-term plan section.
        plan_section_start = canonical.index("## Plans, Advice, And Rationale")
        plan_section_end = canonical.index("## Timeline")
        self.assertNotIn(
            "Skip squats today; walk 20 min.",
            canonical[plan_section_start:plan_section_end],
        )
        # It SHOULD be in recent notes.
        self.assertIn("Skip squats today; walk 20 min.", recent)

    async def test_expired_recent_pruned_on_next_run(self) -> None:
        # Seed an expired recent note directly.
        old_entry = memory_template.build_recent_entry(
            summary="Old note from days ago.",
            category="logistics",
            source="user",
            ttl_days=1,
            now=NOW - timedelta(days=5),
        )
        recent_path = self._recent()
        memory_template.ensure_recent(recent_path, "gym_coach")
        memory_template.append_recent_entry(recent_path, old_entry)

        # Run writer; it should prune before applying.
        payload = {"ops": [{"op": "noop", "reason": "nothing"}]}
        with patch("memory_writer.llm.propose_memory_ops", _llm_returns(payload)):
            result = await memory_writer.propose_and_apply(
                "gym_coach", "user msg", "asst reply", timestamp=NOW, writer_model=config.DEFAULT_MODEL
            )
        self.assertGreaterEqual(result.pruned_recent, 1)
        self.assertNotIn(
            "Old note from days ago.",
            self._recent().read_text(encoding="utf-8"),
        )

    async def test_max_ops_cap_enforced(self) -> None:
        # 8 valid weight measurements; only 5 should be accepted.
        ops = []
        for _ in range(8):
            ops.append(
                {
                    "op": "add_measurement",
                    "source": "user",
                    "date": "2026-05-13",
                    "category": "weight",
                    "value": 95,
                    "unit": "kg",
                    "summary": "Weight 95 kg.",
                    "evidence_quote": "weighed 95 kg",
                    "retention": "long_term",
                }
            )
        payload = {"ops": ops}
        user = "I weighed 95 kg."
        asst = "ok"
        with patch("memory_writer.llm.propose_memory_ops", _llm_returns(payload)):
            result = await memory_writer.propose_and_apply(
                "gym_coach", user, asst, timestamp=NOW, writer_model=config.DEFAULT_MODEL
            )
        self.assertEqual(result.accepted, 5)
        self.assertGreaterEqual(result.rejected, 3)


class SupersedeTests(MemoryWriterTestBase):
    async def test_supersede_annotates_existing_line_and_archives_it(self) -> None:
        # Seed a current-snapshot fact we can supersede.
        canonical_path = self._canonical()
        memory_template.ensure_canonical(canonical_path, "gym_coach")
        md = canonical_path.read_text(encoding="utf-8")
        md = memory_template.append_under_heading(
            md,
            section="Current Snapshot",
            subsection=None,
            body="- [2026-01-01] User takes 5g creatine daily.\n",
        )
        canonical_path.write_text(md, encoding="utf-8")

        user = "I stopped creatine last week."
        asst = "ok"
        payload = {
            "ops": [
                {
                    "op": "supersede",
                    "source": "user",
                    "summary": "User stopped creatine.",
                    "evidence_quote": "I stopped creatine last week.",
                    "retention": "historical",
                    "target_ref": "User takes 5g creatine daily.",
                }
            ]
        }
        with patch("memory_writer.llm.propose_memory_ops", _llm_returns(payload)):
            result = await memory_writer.propose_and_apply(
                "gym_coach", user, asst, timestamp=NOW, writer_model=config.DEFAULT_MODEL
            )
        self.assertEqual(result.accepted, 1)
        new_md = canonical_path.read_text(encoding="utf-8")
        # Original line still present.
        self.assertIn("User takes 5g creatine daily.", new_md)
        # Annotation appended.
        self.assertIn("status=superseded", new_md)
        # Historical section logged.
        self.assertIn("## Superseded / Historical", new_md)
        hist_idx = new_md.index("## Superseded / Historical")
        self.assertIn("creatine", new_md[hist_idx:])


class AuditLogTests(MemoryWriterTestBase):
    async def test_audit_log_records_accepted_and_rejected(self) -> None:
        payload = {
            "ops": [
                {
                    "op": "add_measurement",
                    "source": "user",
                    "category": "weight",
                    "summary": "Weight 95 kg.",
                    "evidence_quote": "weighed 95 kg",
                    "retention": "long_term",
                },
                {
                    "op": "add_measurement",
                    "source": "user",
                    "category": "weight",
                    "summary": "Weight 80 kg.",
                    "evidence_quote": "not in source",
                    "retention": "long_term",
                },
            ]
        }
        with patch("memory_writer.llm.propose_memory_ops", _llm_returns(payload)):
            await memory_writer.propose_and_apply(
                "gym_coach", "I weighed 95 kg.", "ok", timestamp=NOW, writer_model=config.DEFAULT_MODEL
            )
        audit_text = self._audit().read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(audit_text), 1)
        record = json.loads(audit_text[0])
        statuses = [op["status"] for op in record["ops"]]
        self.assertIn("accepted", statuses)
        self.assertIn("rejected", statuses)


class LoadCombinedMemoryTests(MemoryWriterTestBase):
    async def test_load_combined_strips_machine_comments_from_recent(self) -> None:
        canonical_path = self._canonical()
        memory_template.ensure_canonical(canonical_path, "gym_coach")
        recent_path = self._recent()
        memory_template.ensure_recent(recent_path, "gym_coach")
        entry = memory_template.build_recent_entry(
            summary="User feels less sore today.",
            category="pain",
            source="user",
            ttl_days=2,
            now=NOW,
        )
        memory_template.append_recent_entry(recent_path, entry)

        combined = memory_writer.load_combined_memory("gym_coach", now=NOW)
        self.assertIn("# Gym Coach Memory", combined)
        self.assertIn("## Recent Notes (auto-expiring)", combined)
        self.assertIn("User feels less sore today.", combined)
        self.assertNotIn("<!-- recent:", combined)


if __name__ == "__main__":
    unittest.main()
