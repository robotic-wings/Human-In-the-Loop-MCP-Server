"""Source-removal contract for retired local-management artifacts."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
RETIRED_TRACKED_PATHS = {
    "service_manager.py",
    "management_console.py",
    "human_loop_config.py",
    "notify.wav",
}
RETIRED_MODULES = ("service_manager", "management_console", "human_loop_config")
RETIRED_REFERENCES = (
    "human_loop_server",
    "human_loop_config",
    "management_console",
    "service_manager",
    "notify.wav",
    "human_loop_http",
)


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


def _clean_environment() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}


def test_retired_local_management_source_and_asset_are_not_tracked() -> None:
    tracked = _tracked_paths()
    assert RETIRED_TRACKED_PATHS.isdisjoint(tracked)
    for retired_path in RETIRED_TRACKED_PATHS:
        assert not (ROOT / retired_path).exists(), retired_path


def test_retired_local_management_modules_fail_by_absence(tmp_path: Path) -> None:
    for module in RETIRED_MODULES:
        result = subprocess.run(
            [sys.executable, "-c", f"import {module}"],
            cwd=tmp_path,
            env={**_clean_environment(), "PYTHONPATH": str(ROOT)},
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
        assert result.returncode != 0, module
        assert "ModuleNotFoundError" in result.stderr


def test_retained_product_python_sources_do_not_reference_retired_local_stack() -> None:
    tracked = _tracked_paths()
    product_sources = sorted(
        ROOT / path
        for path in tracked
        if path.endswith(".py") and not path.startswith("tests/")
    )
    for source in product_sources:
        text = source.read_text(encoding="utf-8").lower()
        assert not any(reference in text for reference in RETIRED_REFERENCES), source


@pytest.mark.parametrize("legacy_argument", ["--stdio", "--local", "--service"])
def test_central_entrypoint_still_rejects_retired_local_modes(legacy_argument: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "central_service", legacy_argument],
        cwd=ROOT,
        env={**_clean_environment(), "PYTHONPATH": str(ROOT)},
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert result.returncode != 0
    assert "rejected" in (result.stdout + result.stderr).lower()
