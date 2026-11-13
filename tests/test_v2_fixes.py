"""Tests for fixes from the memory v2 code review. All data is made up."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("OPENROUTER_API_KEY", "test-key")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot  # noqa: E402
import cards  # noqa: E402
import config  # noqa: E402
import llm  # noqa: E402
import memory_v2  # noqa: E402
import recall  # noqa: E402
import review  # noqa: E402

SLUG = "gym_coach"


class TempData(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for name, value in (("DATA_DIR", self.root), ("MEMORY_VERSION", "2")):
            patcher = patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)


class ReplyWaitsForMemoryTests(TempData, unittest.IsolatedAsyncioTestCase):
    async def test_v2_reply_waits_for_the_previous_memory_step(self) -> None:
        async def fake_stream(**kwargs):
            yield ("content", "Noted.")

        message = SimpleNamespace(reply_text=AsyncMock(), chat=SimpleNamespace(send_action=AsyncMock()))
        update = SimpleNamespace(message=message, effective_chat=SimpleNamespace(id=1, type="group"))
        ctx = SimpleNamespace(bot=SimpleNamespace(do_api_request=AsyncMock()))
        wait = AsyncMock()
        with patch.dict(bot.session, {"model": "m", "history": [{"role": "system", "content": "s"}], "tts": False}), \
                patch.object(bot, "_wait_for_memory_writes", wait), \
                patch.object(bot.llm, "chat_stream", fake_stream), \
                patch.object(bot, "_persist_session"), \
                patch.object(bot, "_background_extract", AsyncMock()):
            await bot._conversation_reply(update, ctx, user_content="hi", memory_user_text="hi",
                                          stream=True, web_search=False, allow_fallback=True)
        wait.assert_awaited_once()


class SchedulingTests(TempData):
    def _app(self) -> SimpleNamespace:
        return SimpleNamespace(job_queue=MagicMock())

    def test_monday_is_day_one_and_a_bad_setting_does_not_crash(self) -> None:
        app = self._app()
        with patch.object(config, "WEEKLY_REVIEW_DAY", "monday"), patch.object(config, "WEEKLY_REVIEW_TIME", "12:00"):
            bot._schedule_weekly_review(app)
        self.assertEqual(app.job_queue.run_daily.call_args.kwargs["days"], (1,))
        app = self._app()
        with patch.object(config, "WEEKLY_REVIEW_DAY", "mon"):
            bot._schedule_weekly_review(app)
        app.job_queue.run_daily.assert_not_called()

    def test_missed_review_is_caught_up_at_start(self) -> None:
        memory_v2.write_phase(SLUG, {"name": "build", "start": "2026-01-26"})
        review.mark_reviewed(SLUG, 2, "2026-02-09")
        app = self._app()
        with patch.object(bot.ledger, "today_iso", return_value="2026-02-24"):
            bot._schedule_weekly_review(app)
        app.job_queue.run_once.assert_called_once()


class OneFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_busy_fallback_is_not_followed_by_another(self) -> None:
        busy = {"error": {"message": "high demand: exceeds the maximum usage size"}}
        models = []

        def handler(request: httpx.Request) -> httpx.Response:
            models.append(json.loads(request.content)["model"])
            return httpx.Response(200, json=busy)

        transport = httpx.MockTransport(handler)
        chain = {"a": "b", "b": "c"}
        with patch.object(llm, "_httpx_client", lambda timeout: httpx.AsyncClient(transport=transport)), \
                patch.dict(config.RATE_LIMIT_FALLBACK, chain, clear=True):
            with self.assertRaises(RuntimeError):
                await llm.chat([], model="a")
        self.assertEqual(models, ["a", "b"])

    def test_memory_step_effort_override(self) -> None:
        body = llm._build_request_body([], "openai/gpt-6.1-sol", False, "low")
        self.assertEqual(body["reasoning"], {"effort": "low"})
        self.assertNotIn("reasoning", llm._build_request_body([], "moonshotai/kimi-k3.1-pro", False, "low"))


class HistoryAndArchiveTests(TempData):
    def test_two_edits_in_one_second_keep_both_versions(self) -> None:
        fixed = datetime(2026, 2, 23, 9, 0, tzinfo=config.MEMORY_TIMEZONE)
        with patch.object(config, "local_now", return_value=fixed):
            cards.write_card(SLUG, cards.PROFILE, "# Profile\nv1")
            cards.write_card(SLUG, cards.PROFILE, "# Profile\nv2")
            cards.write_card(SLUG, cards.PROFILE, "# Profile\nv3")
        saved = sorted(p.read_text() for p in (self.root / SLUG / "history").glob("profile.*"))
        self.assertEqual(saved, ["# Profile\nv1\n", "# Profile\nv2\n"])

    def test_expired_notes_are_searchable(self) -> None:
        (self.root / f"{SLUG}.recent.pruned.jsonl").write_text(json.dumps(
            {"created": "2026-02-01T10:00:00+08:00", "bullet": "- [2026-02-01 10:00] The physiotherapist suggested hamstring stretches."}) + "\n")
        recall._cache.clear()
        hits = recall.search_archive(SLUG, "what did the physiotherapist say?")
        self.assertEqual(hits[0].date, "2026-02-01")


if __name__ == "__main__":
    unittest.main()
