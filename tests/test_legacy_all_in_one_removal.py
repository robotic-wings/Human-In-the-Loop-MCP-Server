"""Source-removal contract for the retired all-in-one MCP server."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
RETIRED_TRACKED_PATHS = {
    "human_loop_server.py",
    "tests/test_bark_unit.py",
    "tests/e2e_gui_autoclick.py",
    "tests/bark_gui_cases.py",
    "tests/bark_runtime_cases.py",
    "tests/bark_transport_cases.py",
}


def _tracked_paths() -> set[str]:
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
        timeout=10,
    )
    return set(result.stdout.splitlines())


def _source_text(path: Path) -> str:
    return path.read_text(encoding="utf-8").lower()


def test_retired_all_in_one_server_and_exclusive_drivers_are_not_tracked() -> None:
    tracked = _tracked_paths()
    assert RETIRED_TRACKED_PATHS.isdisjoint(tracked)
    assert not (ROOT / "human_loop_server.py").exists()


def test_retired_server_module_and_direct_execution_fail_by_absence(
    tmp_path: Path,
) -> None:
    clean_environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    imported = subprocess.run(
        [sys.executable, "-c", "import human_loop_server"],
        cwd=tmp_path,
        env=clean_environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert imported.returncode != 0
    assert "ModuleNotFoundError" in imported.stderr

    executed = subprocess.run(
        [sys.executable, str(ROOT / "human_loop_server.py")],
        cwd=tmp_path,
        env=clean_environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert executed.returncode != 0
    assert "No such file" in executed.stderr or "can't open file" in executed.stderr.lower()


def test_remaining_central_product_source_has_no_all_in_one_stdio_or_gui_server() -> None:
    retained_product_sources = (ROOT / "central_service.py", ROOT / "call_coordinator.py")
    prohibited = ("from fastmcp", "fastmcp(", "mcp.run(", "import tkinter", "tkinter.")
    for source in retained_product_sources:
        text = _source_text(source)
        assert not any(marker in text for marker in prohibited), source


@pytest.mark.parametrize("legacy_argument", ["--stdio", "--local", "--service"])
def test_central_entrypoint_rejects_retired_modes(legacy_argument: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "central_service", legacy_argument],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert result.returncode != 0
    assert "rejected" in (result.stdout + result.stderr).lower()
