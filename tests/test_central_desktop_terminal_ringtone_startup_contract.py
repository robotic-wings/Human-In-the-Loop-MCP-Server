"""Contract: the terminal's startup ringtone selection must honor saved preferences.

The settings panel persists both ringtone picks into ``preferences.json``
beside the config; the config file's ``ringtone_path`` is bootstrap-only.
At startup the terminal must resolve ONE effective long ringtone (saved
preference if present, else the config bootstrap) and use it for BOTH the
player and the settings panel's preselection. The shipped bug: the panel was
preselected from ``config.ringtone_path`` directly, so after every restart
the panel showed (and let you accidentally re-save) the bootstrap default
even though a preference existed.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any


def _module() -> Any:
    return importlib.import_module("central_desktop_terminal")


def _config(tmp_path: Path, ringtone: str = "default-long.mp3") -> Any:
    module = _module()
    return module.DesktopTerminalConfig.from_mapping({
        "central_url": "https://central.test",
        "callsign": "NIGHTHAWK",
        "operator_password": "pw",
        "device_id": "desktop-1",
        "callback_credential": "cb",
        "listener_host": "127.0.0.1",
        "listener_port": 9100,
        "ringtones_dir": str(tmp_path / "ringtones"),
        "ringtone_path": str(tmp_path / "ringtones" / ringtone),
    })


def test_startup_selection_prefers_saved_long_ringtone_for_panel_and_player(tmp_path: Path) -> None:
    module = _module()
    config = _config(tmp_path)
    saved = tmp_path / "ringtones" / "saved-long.mp3"
    prefs_file = tmp_path / "preferences.json"
    prefs_file.write_text(json.dumps({
        "long_ringtone_path": str(saved),
        "short_ringtone_path": str(tmp_path / "ringtones" / "saved-short.mp3"),
    }), encoding="utf-8")

    selection = module.startup_ringtone_selection(config, prefs_file)

    assert selection.long_ringtone_path == str(saved), "player must use the saved long ringtone"
    assert selection.panel_ringtone_path == str(saved), \
        "panel preselection must show the saved pick, not the config bootstrap default"
    assert selection.short_ringtone_path == str(tmp_path / "ringtones" / "saved-short.mp3")


def test_startup_selection_falls_back_to_config_when_no_preferences(tmp_path: Path) -> None:
    module = _module()
    config = _config(tmp_path)
    prefs_file = tmp_path / "preferences.json"  # absent

    selection = module.startup_ringtone_selection(config, prefs_file)

    bootstrap = str(tmp_path / "ringtones" / "default-long.mp3")
    assert selection.long_ringtone_path == bootstrap
    assert selection.panel_ringtone_path == bootstrap
    assert selection.short_ringtone_path == ""


def test_startup_selection_survives_a_restart_roundtrip(tmp_path: Path) -> None:
    """Pick B via the panel save path, restart (re-resolve), still B."""
    module = _module()
    config = _config(tmp_path)
    prefs_file = tmp_path / "preferences.json"
    ringtones = tmp_path / "ringtones"
    ringtones.mkdir(parents=True, exist_ok=True)

    # Simulate the panel's save handlers choosing beta.mp3 for both channels.
    module.save_short_ringtone(str(prefs_file), str(ringtones / "beta.mp3"))
    prefs = module.load_preferences(str(prefs_file))
    prefs["long_ringtone_path"] = str(ringtones / "beta.mp3")
    module.save_preferences(str(prefs_file), prefs)

    # Restart: a fresh process re-resolves from disk.
    again = module.startup_ringtone_selection(config, prefs_file)
    assert again.panel_ringtone_path == str(ringtones / "beta.mp3")
    assert again.long_ringtone_path == str(ringtones / "beta.mp3")
    assert again.short_ringtone_path == str(ringtones / "beta.mp3")
