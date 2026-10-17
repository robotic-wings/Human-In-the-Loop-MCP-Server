"""Portable, GUI-free Red contracts for Bark configuration defaults."""

from __future__ import annotations

import asyncio
import inspect
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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


class _BarkCapture(BaseHTTPRequestHandler):
    requests = []

    def do_GET(self) -> None:  # noqa: N802 - required BaseHTTPRequestHandler spelling
        type(self).requests.append(self.path)
        self.send_response(200)
        self.end_headers()

    def log_message(self, _format, *_args) -> None:
        pass


class BarkSchedulerFailureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        _BarkCapture.requests = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _BarkCapture)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.bark_url = f"http://127.0.0.1:{self.server.server_port}/synthetic-key"

    async def asyncTearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    async def _schedule(self, params) -> None:
        import human_loop_server

        scheduled = human_loop_server._schedule_bark_notification(params)
        if inspect.isawaitable(scheduled):
            await scheduled

    async def _wait_for_requests(self, count: int) -> None:
        for _ in range(100):
            if len(_BarkCapture.requests) >= count:
                return
            await asyncio.sleep(0.01)
        self.fail(f"expected {count} Bark request(s), got {_BarkCapture.requests!r}")

    def _settings(self, human_loop_server):
        return mock.patch.object(human_loop_server.human_loop_config, "get_bark_settings",
                                 return_value=(True, self.bark_url))

    async def test_unencodable_title_is_skipped_and_later_valid_send_runs(self) -> None:
        import human_loop_server

        with self._settings(human_loop_server):
            await self._schedule({
                "notify_heading": chr(0xD800), "notify_preview": "malformed title",
            })
            await asyncio.sleep(0.05)
            self.assertEqual(_BarkCapture.requests, [])
            await self._schedule({
                "notify_heading": "Valid title", "notify_preview": "Valid body",
            })
            await self._wait_for_requests(1)
        query = parse_qs(urlsplit(_BarkCapture.requests[0]).query)
        self.assertEqual(query["title"], ["Valid title"])
        self.assertEqual(query["body"], ["Valid body"])

    async def test_failed_task_creation_does_not_exhaust_future_delivery(self) -> None:
        import human_loop_server

        with self._settings(human_loop_server):
            with mock.patch.object(asyncio.get_running_loop(), "create_task",
                                   side_effect=RuntimeError("task creation failed")):
                for attempt in range(8):
                    await self._schedule({
                        "notify_heading": f"failed {attempt}", "notify_preview": "retry later",
                    })
            await self._schedule({"notify_heading": "Healthy", "notify_preview": "delivers"})
            await self._wait_for_requests(1)
        query = parse_qs(urlsplit(_BarkCapture.requests[0]).query)
        self.assertEqual(query["title"], ["Healthy"])

    async def test_cancel_before_first_step_releases_future_delivery(self) -> None:
        import human_loop_server

        with self._settings(human_loop_server):
            before = asyncio.all_tasks()
            await self._schedule({"notify_heading": "Cancel", "notify_preview": "never starts"})
            created = asyncio.all_tasks() - before
            self.assertTrue(created, "scheduling a Bark notification must create deliverable work")
            for task in created:
                task.cancel()
            await asyncio.gather(*created, return_exceptions=True)
            await self._schedule({"notify_heading": "Healthy", "notify_preview": "delivers"})
            await self._wait_for_requests(1)
        query = parse_qs(urlsplit(_BarkCapture.requests[0]).query)
        self.assertEqual(query["title"], ["Healthy"])


if __name__ == "__main__":
    unittest.main()
