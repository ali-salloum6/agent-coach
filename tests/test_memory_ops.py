"""Tests for the JSON memory-operation validator."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

# Make project root importable when running with `python -m unittest discover`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import memory_ops  # noqa: E402


USER = "I slept 5 hours and weighed 95.2 kg. Lower back feels a bit sore today."
ASST = (
    "Take a Multivitamin mid-meal to avoid vitamin nausea. "
    "For today, skip squats. Plan for today: walk 20 min. "
    "Crisis protocol: if pain spikes, ice 15 min."
)


def _payload(*ops: dict) -> str:
    return json.dumps({"ops": list(ops)})


class ParseTests(unittest.TestCase):
    def test_rejects_invalid_json(self) -> None:
        res = memory_ops.validate("not json", user_message="x", assistant_response="y")
        self.assertIsNotNone(res.parse_error)
        self.assertEqual(res.accepted, [])
        self.assertEqual(res.rejected, [])

    def test_accepts_fenced_json(self) -> None:
        payload = "```json\n" + _payload({"op": "noop"}) + "\n```"
        res = memory_ops.validate(payload, user_message=USER, assistant_response=ASST)
        self.assertIsNone(res.parse_error)
        self.assertEqual(len(res.accepted), 1)
        self.assertEqual(res.accepted[0].op, "noop")

    def test_rejects_missing_ops_list(self) -> None:
        res = memory_ops.validate("{}", user_message=USER, assistant_response=ASST)
        self.assertIn("ops", res.parse_error)


class EvidenceQuoteTests(unittest.TestCase):
    def test_user_quote_must_match_user_message(self) -> None:
        payload = _payload(
            {
                "op": "add_measurement",
                "source": "user",
                "date": "2026-05-13",
                "category": "sleep",
                "value": 5,
                "unit": "hours",
                "summary": "Slept 5 hours.",
                "evidence_quote": "I slept 5 hours",
                "retention": "long_term",
            }
        )
        res = memory_ops.validate(payload, user_message=USER, assistant_response=ASST)
        self.assertEqual(len(res.accepted), 1)
        self.assertEqual(res.rejected, [])

    def test_user_quote_not_in_user_message_rejected(self) -> None:
        payload = _payload(
            {
                "op": "add_measurement",
                "source": "user",
                "category": "sleep",
                "summary": "Slept 7 hours.",
                "evidence_quote": "slept 7 hours",  # not in USER
                "retention": "long_term",
            }
        )
        res = memory_ops.validate(payload, user_message=USER, assistant_response=ASST)
        self.assertEqual(res.accepted, [])
        self.assertEqual(len(res.rejected), 1)
        self.assertIn("evidence_quote not found", res.rejected[0].reason)

    def test_quote_only_in_assistant_rejected_for_user_source(self) -> None:
        payload = _payload(
            {
                "op": "add_current_fact",
                "source": "user",
                "category": "supplement",
                "summary": "User decided multivitamin mid-meal.",
                "evidence_quote": "Multivitamin mid-meal",  # in ASST, not USER
                "retention": "long_term",
            }
        )
        res = memory_ops.validate(payload, user_message=USER, assistant_response=ASST)
        self.assertEqual(res.accepted, [])
        self.assertEqual(len(res.rejected), 1)

    def test_markdown_emphasis_in_source_does_not_block_match(self) -> None:
        """Plain evidence must match bold/italic wrappers in the assistant reply."""
        asst = (
            "Garlic baguette: 550 kcal\n\n"
            "**Total so far:** 810 calories, 41g protein\n\n"
            "Plenty of room left."
        )
        payload = _payload(
            {
                "op": "add_recent_note",
                "source": "assistant",
                "date": "2026-07-18",
                "category": "nutrition",
                "summary": "Running daily total 810 kcal after baguette.",
                "evidence_quote": "Total so far: 810 calories, 41g protein",
                "retention": "recent",
                "ttl_days": 2,
            }
        )
        res = memory_ops.validate(payload, user_message="The 30g version", assistant_response=asst)
        self.assertEqual(len(res.accepted), 1, res.rejected)
        self.assertEqual(res.rejected, [])


class SourceOpRules(unittest.TestCase):
    def test_assistant_cannot_create_current_fact(self) -> None:
        payload = _payload(
            {
                "op": "add_current_fact",
                "source": "assistant",
                "category": "profile",
                "summary": "User is 23 years old.",
                "evidence_quote": "Plan for today: walk 20 min",  # in ASST
                "retention": "long_term",
            }
        )
        res = memory_ops.validate(payload, user_message=USER, assistant_response=ASST)
        self.assertEqual(res.accepted, [])
        self.assertTrue(
            any("add_current_fact must be source=user" in r.reason for r in res.rejected)
        )


class PlanRetentionTests(unittest.TestCase):
    def test_transient_assistant_plan_downgraded_to_recent(self) -> None:
        payload = _payload(
            {
                "op": "add_plan",
                "source": "assistant",
                "category": "workout",
                "summary": "Skip squats today; walk 20 min.",
                "evidence_quote": "For today, skip squats",
                "retention": "long_term",
            }
        )
        res = memory_ops.validate(payload, user_message=USER, assistant_response=ASST)
        self.assertEqual(len(res.accepted), 1)
        accepted = res.accepted[0]
        self.assertEqual(accepted.retention, "recent")
        self.assertIsNotNone(accepted.ttl_days)

    def test_durable_protocol_plan_stays_long_term(self) -> None:
        payload = _payload(
            {
                "op": "add_plan",
                "source": "assistant",
                "category": "injury",
                "summary": "Crisis protocol: if pain spikes, ice 15 min.",
                "evidence_quote": "Crisis protocol: if pain spikes, ice 15 min",
                "retention": "long_term",
            }
        )
        res = memory_ops.validate(payload, user_message=USER, assistant_response=ASST)
        self.assertEqual(len(res.accepted), 1)
        self.assertEqual(res.accepted[0].retention, "long_term")


class CategoryTests(unittest.TestCase):
    def test_unknown_category_rejected(self) -> None:
        payload = _payload(
            {
                "op": "add_recent_note",
                "source": "user",
                "category": "snake-oil",
                "summary": "Tried snake oil today.",
                "evidence_quote": "I slept 5 hours",
                "retention": "recent",
                "ttl_days": 2,
            }
        )
        res = memory_ops.validate(payload, user_message=USER, assistant_response=ASST)
        self.assertEqual(res.accepted, [])
        self.assertTrue(any("category not allowed" in r.reason for r in res.rejected))

    def test_missing_category_for_recent_note_rejected(self) -> None:
        payload = _payload(
            {
                "op": "add_recent_note",
                "source": "user",
                "summary": "Ate an apple.",
                "evidence_quote": "I slept 5 hours",
                "retention": "recent",
                "ttl_days": 2,
            }
        )
        res = memory_ops.validate(payload, user_message=USER, assistant_response=ASST)
        self.assertEqual(res.accepted, [])


class RecentNoteTests(unittest.TestCase):
    def test_default_ttl_for_recent_note(self) -> None:
        payload = _payload(
            {
                "op": "add_recent_note",
                "source": "user",
                "category": "nutrition",
                "summary": "Light dinner today.",
                "evidence_quote": "I slept 5 hours",
                "retention": "recent",
            }
        )
        res = memory_ops.validate(payload, user_message=USER, assistant_response=ASST)
        self.assertEqual(len(res.accepted), 1)
        self.assertEqual(res.accepted[0].ttl_days, 2)

    def test_invalid_ttl_rejected(self) -> None:
        payload = _payload(
            {
                "op": "add_recent_note",
                "source": "user",
                "category": "nutrition",
                "summary": "Light dinner today.",
                "evidence_quote": "I slept 5 hours",
                "retention": "recent",
                "ttl_days": 5,
            }
        )
        res = memory_ops.validate(payload, user_message=USER, assistant_response=ASST)
        self.assertEqual(res.accepted, [])
        self.assertTrue(any("ttl_days" in r.reason for r in res.rejected))


class SupersedeTests(unittest.TestCase):
    def test_supersede_requires_target_ref(self) -> None:
        payload = _payload(
            {
                "op": "supersede",
                "source": "user",
                "summary": "I no longer take creatine.",
                "evidence_quote": "I slept 5 hours",
                "retention": "historical",
            }
        )
        res = memory_ops.validate(payload, user_message=USER, assistant_response=ASST)
        self.assertEqual(res.accepted, [])
        self.assertTrue(any("target_ref" in r.reason for r in res.rejected))

    def test_supersede_with_target_ref_accepted(self) -> None:
        payload = _payload(
            {
                "op": "supersede",
                "source": "user",
                "summary": "User stopped creatine.",
                "evidence_quote": "I slept 5 hours",
                "retention": "historical",
                "target_ref": "User takes creatine daily.",
            }
        )
        res = memory_ops.validate(payload, user_message=USER, assistant_response=ASST)
        self.assertEqual(len(res.accepted), 1)
        self.assertEqual(res.accepted[0].op, "supersede")


class AntiFloodTests(unittest.TestCase):
    def test_max_ops_enforced(self) -> None:
        ops = []
        for _ in range(memory_ops.MAX_ACCEPTED_OPS + 3):
            ops.append(
                {
                    "op": "add_measurement",
                    "source": "user",
                    "category": "weight",
                    "summary": "Weight 95.2 kg.",
                    "evidence_quote": "weighed 95.2 kg",
                    "retention": "long_term",
                }
            )
        res = memory_ops.validate(
            _payload(*ops), user_message=USER, assistant_response=ASST
        )
        non_noop_accepted = [op for op in res.accepted if op.op != "noop"]
        self.assertEqual(len(non_noop_accepted), memory_ops.MAX_ACCEPTED_OPS)
        self.assertGreaterEqual(len(res.rejected), 3)
        self.assertTrue(any("MAX_ACCEPTED_OPS" in r.reason for r in res.rejected))


if __name__ == "__main__":
    unittest.main()
