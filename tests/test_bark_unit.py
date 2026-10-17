"""Portable, GUI-free Red contracts for Bark configuration defaults."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

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


class _CountingSlots:
    def __init__(self) -> None:
        self.available = 1

    def acquire(self, blocking: bool = True) -> bool:
        if self.available == 0:
            return False
        self.available -= 1
        return True

    def release(self) -> None:
        self.available += 1


class _RecordingExecutor:
    def __init__(self) -> None:
        self.jobs = []

    def submit(self, callback) -> None:
        self.jobs.append(callback)


class BarkSchedulerFailureTests(unittest.TestCase):
    def test_unencodable_title_is_skipped_and_later_valid_send_runs(self) -> None:
        import human_loop_server

        slots = _CountingSlots()
        executor = _RecordingExecutor()
        synthetic_url = "http://127.0.0.1:8081/synthetic-key"
        with (
            mock.patch.object(human_loop_server.human_loop_config, "get_bark_settings",
                              return_value=(True, synthetic_url)),
            mock.patch.object(human_loop_server, "_bark_slots", slots),
            mock.patch.object(human_loop_server, "_bark_executor", executor),
            mock.patch.object(human_loop_server, "_out") as diagnostic,
        ):
            human_loop_server._schedule_bark_notification({
                "notify_heading": chr(0xD800), "notify_preview": "malformed title",
            })
            self.assertEqual(slots.available, 1)
            self.assertEqual(executor.jobs, [])
            diagnostic.assert_called_once()
            self.assertNotIn(synthetic_url, str(diagnostic.call_args))

            human_loop_server._schedule_bark_notification({
                "notify_heading": "Valid title", "notify_preview": "Valid body",
            })
            self.assertEqual(slots.available, 0)
            self.assertEqual(len(executor.jobs), 1)

            response = mock.MagicMock()
            response.__enter__.return_value = response
            with mock.patch.object(human_loop_server, "urlopen", return_value=response) as urlopen:
                executor.jobs[0]()

            request = urlopen.call_args.args[0]
            query = parse_qs(urlsplit(request.full_url).query)
            self.assertEqual(query["title"], ["Valid title"])
            self.assertEqual(query["body"], ["Valid body"])
            self.assertEqual(slots.available, 1)

            with mock.patch.object(human_loop_server, "_bark_executor") as failed_executor:
                failed_executor.submit.side_effect = RuntimeError(synthetic_url)
                human_loop_server._schedule_bark_notification({
                    "notify_heading": "Queue failure", "notify_preview": "must stay isolated",
                })
            self.assertEqual(slots.available, 1)
            self.assertNotIn(synthetic_url, str(diagnostic.call_args))


if __name__ == "__main__":
    unittest.main()
