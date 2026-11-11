"""Tests for starting a fresh conversation: pending memory writes and the prompt clock."""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("OPENROUTER_API_KEY", "test-key")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot  # noqa: E402
import config  # noqa: E402
from agents.base import AgentConfig  # noqa: E402


class NewWaitsForMemoryWritesTests(unittest.IsolatedAsyncioTestCase):
    async def _run_new(self, memory_task: asyncio.Task) -> list[bool]:
        """Run /new with one memory write in flight; return whether it was done at reset."""
        done_at_reset: list[bool] = []

        def fake_reset(model: str | None = None) -> None:
            done_at_reset.append(memory_task.done())
            bot.session["model"] = model or "openai/gpt-6.1-sol"

        message = SimpleNamespace(
            reply_text=AsyncMock(),
            chat=SimpleNamespace(send_action=AsyncMock()),
        )
        update = SimpleNamespace(message=message)
        ctx = SimpleNamespace(args=[])

        bot._memory_tasks.add(memory_task)
        memory_task.add_done_callback(bot._memory_tasks.discard)
        with patch.dict(bot.session, {"web_search": False}), \
                patch.object(bot, "_is_allowed_user", return_value=True), \
                patch.object(bot, "_reset_session", fake_reset):
            await bot.cmd_new(update, ctx)
        message.chat.send_action.assert_awaited_with("typing")
        return done_at_reset

    async def test_new_waits_for_pending_memory_write(self) -> None:
        task = asyncio.create_task(asyncio.sleep(0.05))
        self.assertEqual(await self._run_new(task), [True])

    async def test_new_gives_up_after_the_timeout(self) -> None:
        task = asyncio.create_task(asyncio.Event().wait())
        try:
            with patch.object(bot, "MEMORY_WRITE_WAIT_SECONDS", 0.01):
                self.assertEqual(await self._run_new(task), [False])
        finally:
            task.cancel()


class PromptClockTests(unittest.TestCase):
    def test_prompt_uses_the_memory_timezone(self) -> None:
        agent = AgentConfig(
            name="Test", slug="test", system_prompt_template="{current_datetime}\n{memory}"
        )
        now = datetime(2026, 11, 11, 15, 26, tzinfo=ZoneInfo("Asia/Makassar"))
        with patch.object(config, "local_now", return_value=now), \
                patch.object(config, "MEMORY_TIMEZONE_LABEL", "Makassar"), \
                patch.object(config, "TTS_ENABLED", False):
            prompt = agent.build_system_prompt("")
        self.assertTrue(prompt.startswith("2026-11-11, Wednesday, 15:26 Makassar\n"))


if __name__ == "__main__":
    unittest.main()
