"""Portable, GUI-free Red contracts for Bark configuration defaults."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

import human_loop_config


class BarkConfigDefaultsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_config = os.environ.get("HUMAN_LOOP_CONFIG")
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "settings.json"
        os.environ["HUMAN_LOOP_CONFIG"] = str(self.path)

    def tearDown(self) -> None:
        if self._old_config is None:
            os.environ.pop("HUMAN_LOOP_CONFIG", None)
        else:
            os.environ["HUMAN_LOOP_CONFIG"] = self._old_config
        self._tmp.cleanup()

    def test_new_config_disables_bark_without_a_url(self) -> None:
        notification = human_loop_config.load_config()["notification"]
        self.assertIn("bark_enabled", notification)
        self.assertIn("bark_url", notification)
        self.assertIs(notification["bark_enabled"], False)
        self.assertEqual(notification["bark_url"], "")

    def test_fresh_reader_keeps_explicit_bark_settings(self) -> None:
        cfg = human_loop_config.load_config()
        cfg["notification"].update({
            "bark_enabled": True,
            "bark_url": "http://127.0.0.1:8081/test-key",
        })
        human_loop_config.save_config(cfg)
        fresh = human_loop_config.load_config()["notification"]
        self.assertTrue(fresh["bark_enabled"])
        self.assertEqual(fresh["bark_url"], "http://127.0.0.1:8081/test-key")


if __name__ == "__main__":
    unittest.main()
