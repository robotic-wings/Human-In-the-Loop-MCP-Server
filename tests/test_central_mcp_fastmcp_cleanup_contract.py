"""Red contract for removing the legacy fastmcp dependency (SPEC §10.4/§2.4)."""

from __future__ import annotations

import tomllib
from pathlib import Path


def test_pyproject_dependencies_exclude_fastmcp() -> None:
    with open(Path("pyproject.toml"), "rb") as handle:
        data = tomllib.load(handle)
    dependencies = data["project"]["dependencies"]
    assert not any(str(dep).startswith("fastmcp") for dep in dependencies)
