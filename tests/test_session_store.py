"""Tests for on-disk session persistence."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import session_store  # noqa: E402


class SessionStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name)
        self._patcher = patch.object(session_store.config, "DATA_DIR", self.data_dir)
        self._patcher.start()
        self.addCleanup(self._patcher.stop)

    def test_save_load_roundtrip(self) -> None:
        session = {
            "model": "z-ai/glm-5.2",
            "history": [
                {"role": "system", "content": "You are a coach."},
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello"},
            ],
            "web_search": True,
            "tts": False,
        }
        session_store.save("gym_coach", session)
        loaded = session_store.load("gym_coach")
        self.assertEqual(loaded, session)
        self.assertTrue(session_store.session_path("gym_coach").is_file())

    def test_clear_deletes_file(self) -> None:
        session_store.save(
            "gym_coach",
            {
                "model": "m",
                "history": [{"role": "system", "content": "x"}],
                "web_search": False,
                "tts": False,
            },
        )
        session_store.clear("gym_coach")
        self.assertIsNone(session_store.load("gym_coach"))
        self.assertFalse(session_store.session_path("gym_coach").is_file())

    def test_load_missing_returns_none(self) -> None:
        self.assertIsNone(session_store.load("nope"))

    def test_corrupt_json_returns_none(self) -> None:
        path = session_store.session_path("gym_coach")
        path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(session_store.load("gym_coach"))

    def test_empty_history_rejected(self) -> None:
        path = session_store.session_path("gym_coach")
        path.write_text(
            json.dumps({"model": "m", "history": [], "web_search": False, "tts": False}),
            encoding="utf-8",
        )
        self.assertIsNone(session_store.load("gym_coach"))


if __name__ == "__main__":
    unittest.main()
