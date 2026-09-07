"""Build and verify distributions with the exact declared minimum setuptools."""

from __future__ import annotations

import subprocess
import sys
import tomllib
from importlib.metadata import version
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    requirement = next(
        requirement
        for value in config["build-system"]["requires"]
        if (requirement := Requirement(value)).name == "setuptools"
    )
    lower_bounds = [
        Version(item.version) for item in requirement.specifier if item.operator == ">="
    ]
    if not lower_bounds:
        raise ValueError(
            "Declare an inclusive minimum setuptools version for this gate"
        )
    minimum = max(lower_bounds)
    installed = Version(version("setuptools"))
    if installed != minimum or installed not in requirement.specifier:
        raise RuntimeError(
            f"Minimum-builder gate needs setuptools=={minimum}; installed {installed}"
        )
    output = root / "build" / "minimum-dist"
    subprocess.run(
        [sys.executable, "-m", "build", "--no-isolation", "--outdir", str(output)],
        cwd=root,
        check=True,
        timeout=120,
    )
    subprocess.run(
        [
            sys.executable,
            str(root / "scripts" / "check_generic_distribution.py"),
            str(output),
        ],
        cwd=root,
        check=True,
        timeout=120,
    )
    print(
        f"Minimum setuptools {minimum}: wheel, sdist, metadata and offline example passed"
    )


if __name__ == "__main__":
    main()
