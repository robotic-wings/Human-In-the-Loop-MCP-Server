"""Explicit graphical Red cases for Bark settings.

Run on a desktop: ``.venv/bin/python tests/bark_gui_cases.py``.
"""

from __future__ import annotations

import os
import tempfile
import tkinter as tk
import unittest
from pathlib import Path

import human_loop_config
import management_console


def _walk(widget: tk.Misc):
    yield widget
    for child in widget.winfo_children():
        yield from _walk(child)


class BarkSettingsGuiCases(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old = os.environ.get("HUMAN_LOOP_CONFIG")
        self.path = Path(self.tmp.name) / "settings.json"
        os.environ["HUMAN_LOOP_CONFIG"] = str(self.path)
        self.root = tk.Tk()
        self.root.withdraw()

    def tearDown(self) -> None:
        self.root.destroy()
        if self.old is None:
            os.environ.pop("HUMAN_LOOP_CONFIG", None)
        else:
            os.environ["HUMAN_LOOP_CONFIG"] = self.old
        self.tmp.cleanup()

    def _tab(self) -> management_console.NotificationTab:
        return management_console.NotificationTab(self.root)

    @staticmethod
    def _text(tab: management_console.NotificationTab, cls: str, text: str) -> tk.Misc:
        for widget in _walk(tab.frame):
            if widget.winfo_class() == cls and widget.cget("text") == text:
                return widget
        raise AssertionError(f"missing rendered {cls}: {text!r}")

    def _controls(self, tab: management_console.NotificationTab) -> tuple[tk.Checkbutton, tk.Entry]:
        toggle = self._text(tab, "Checkbutton", "Enable Bark notifications")
        label = self._text(tab, "Label", "Bark URL:")
        row = int(label.grid_info()["row"])
        for widget in tab.body.winfo_children():
            if widget.winfo_class() == "Entry" and int(widget.grid_info().get("row", -1)) == row:
                return toggle, widget
        raise AssertionError("missing Bark URL field")

    def _save(self, tab: management_console.NotificationTab) -> None:
        self._text(tab, "Button", "Save").invoke()
        self.root.update_idletasks()

    def test_save_and_reopen_persists_private_http_url_independently_of_mute(self) -> None:
        tab = self._tab()
        toggle, url = self._controls(tab)
        self.assertEqual(int(toggle.getvar(toggle.cget("variable"))), 0)
        self.assertEqual(url.get(), "")
        toggle.invoke()
        url.insert(0, "http://127.0.0.1:8081/key/保留")
        self._save(tab)
        saved = human_loop_config.load_config()["notification"]
        self.assertTrue(saved["bark_enabled"])
        self.assertEqual(saved["bark_url"], "http://127.0.0.1:8081/key/保留")
        self.assertFalse(saved["muted"])
        reopened_toggle, reopened_url = self._controls(self._tab())
        self.assertEqual(int(reopened_toggle.getvar(reopened_toggle.cget("variable"))), 1)
        self.assertEqual(reopened_url.get(), "http://127.0.0.1:8081/key/保留")

    def test_disable_then_save_and_reopen_keeps_the_url(self) -> None:
        bark_url = "https://bark.example.test/disabled-key"
        human_loop_config.save_config({"notification": {
            "bark_enabled": True, "bark_url": bark_url,
        }})
        tab = self._tab()
        toggle, _ = self._controls(tab)
        self.assertEqual(int(toggle.getvar(toggle.cget("variable"))), 1)
        toggle.invoke()
        self._save(tab)
        saved = human_loop_config.load_config()["notification"]
        self.assertFalse(saved["bark_enabled"])
        self.assertEqual(saved["bark_url"], bark_url)
        reopened_toggle, reopened_url = self._controls(self._tab())
        self.assertEqual(int(reopened_toggle.getvar(reopened_toggle.cget("variable"))), 0)
        self.assertEqual(reopened_url.get(), bark_url)

    def test_invalid_enabled_urls_warn_and_leave_saved_bytes_unchanged(self) -> None:
        human_loop_config.save_config({"notification": {"bark_enabled": True, "bark_url": "http://127.0.0.1:8081/key"}})
        before = self.path.read_bytes()
        tab = self._tab()
        toggle, url = self._controls(tab)
        if not int(toggle.getvar(toggle.cget("variable"))):
            toggle.invoke()
        warnings = []
        original_warning = management_console.messagebox.showwarning
        management_console.messagebox.showwarning = lambda title, message: warnings.append((title, message))
        try:
            for invalid in ("", "ftp://host/key", "http://user@host/key", "https://host/key#part", "https://host:bad/key", "https://host", "https://host:70000/key", "https://host/key\nnext"):
                with self.subTest(url=invalid):
                    warning_count = len(warnings)
                    url.delete(0, tk.END)
                    url.insert(0, invalid)
                    self._save(tab)
                    self.assertEqual(len(warnings), warning_count + 1)
                    self.assertEqual(self.path.read_bytes(), before)
        finally:
            management_console.messagebox.showwarning = original_warning

    def test_save_preserves_ring_mute_and_unknown_notification_settings(self) -> None:
        human_loop_config.save_config({"profile": {"name": "Ada"}, "server": {"http_port": 9123}, "notification": {
            "ringtone_path": "/tmp/ring.wav", "muted": True, "future_key": "kept",
        }})
        tab = self._tab()
        toggle, url = self._controls(tab)
        toggle.invoke()
        url.insert(0, "https://bark.example.test/key")
        self._save(tab)
        config = human_loop_config.load_config()
        self.assertEqual(config["profile"]["name"], "Ada")
        self.assertEqual(config["server"]["http_port"], 9123)
        self.assertEqual(config["notification"]["ringtone_path"], "/tmp/ring.wav")
        self.assertTrue(config["notification"]["muted"])
        self.assertEqual(config["notification"]["future_key"], "kept")
        self.assertTrue(config["notification"]["bark_enabled"])
        self.assertEqual(config["notification"]["bark_url"], "https://bark.example.test/key")

    def test_save_write_failure_shows_error_without_saved_status_or_disk_change(self) -> None:
        human_loop_config.save_config({"notification": {
            "bark_enabled": True, "bark_url": "https://bark.example.test/original",
        }})
        before = self.path.read_bytes()
        tab = self._tab()
        toggle, url = self._controls(tab)
        if not int(toggle.getvar(toggle.cget("variable"))):
            toggle.invoke()
        url.delete(0, tk.END)
        url.insert(0, "https://bark.example.test/not-written")
        errors = []
        original_save = management_console.human_loop_config.save_config
        original_error = management_console.messagebox.showerror

        def fail_save(_config):
            raise OSError("disk write failed")

        management_console.human_loop_config.save_config = fail_save
        management_console.messagebox.showerror = lambda title, message: errors.append((title, message))
        try:
            self._save(tab)
        finally:
            management_console.human_loop_config.save_config = original_save
            management_console.messagebox.showerror = original_error
        self.assertEqual(errors, [("Save", "Could not save config:\ndisk write failed")])
        self.assertNotEqual(tab.status.cget("text"), "Saved.")
        self.assertEqual(self.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
