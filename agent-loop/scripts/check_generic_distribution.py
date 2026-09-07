"""Validate a wheel and sdist, then run the packaged offline example in isolation."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

from packaging.metadata import Metadata


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("distribution_dir", type=Path)
    options = parser.parse_args()
    directory = options.distribution_dir.resolve(strict=True)
    wheels = list(directory.glob("*.whl"))
    sources = list(directory.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sources) != 1:
        raise ValueError("Expected exactly one wheel and one sdist")
    example_dir = directory / "installed-example"
    example_dir.mkdir(exist_ok=True)
    required = {"__init__.py", "tools.py", "run_demo.py", "business.toml", "README.md"}
    with zipfile.ZipFile(wheels[0]) as archive:
        names = archive.namelist()
        assert "pi_agent_loop/py.typed" in names
        metadata_name = next(name for name in names if name.endswith(".dist-info/METADATA"))
        metadata = Metadata.from_email(archive.read(metadata_name), validate=True)
        assert metadata.name == "pi-agent-loop-python"
        for filename in required:
            suffix = f"share/pi-agent-loop/examples/business_package/{filename}"
            matches = [name for name in names if name.endswith(suffix)]
            assert len(matches) == 1, suffix
            (example_dir / filename).write_bytes(archive.read(matches[0]))
    with tarfile.open(sources[0], "r:gz") as archive:
        names = archive.getnames()
        for filename in required:
            assert any(
                name.endswith(f"examples/business_package/{filename}") for name in names
            ), filename
    # -I ignores source checkout/PYTHONPATH/editable preferences. Only the built
    # wheel and its extracted example are prepended explicitly; dependencies
    # remain provided by the caller's installed environment.
    script = """
import pathlib, runpy, sys
wheel, example = sys.argv[1:]
sys.path[:0] = [wheel, example]
import pi_agent_loop
assert pi_agent_loop.__file__.startswith(wheel), pi_agent_loop.__file__
for name in (
    "BusinessBundle", "load_business_bundle", "ExecutionPolicy", "ExecutionContext",
    "GenerationOptions", "SessionEventJournal", "SessionJournalCapabilities",
    "SynchronousSessionEventJournal", "JournalPlanStore", "JournalRunStore",
    "JournalOperationStore", "RuntimeBoundRouter", "validate_session_event_journal",
):
    assert name in pi_agent_loop.__all__ and getattr(pi_agent_loop, name) is not None, name
runpy.run_path(str(pathlib.Path(example) / "run_demo.py"), run_name="__main__")
"""
    completed = subprocess.run(
        [sys.executable, "-I", "-c", script, str(wheels[0]), str(example_dir)],
        cwd=directory,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    assert "completed" in completed.stdout, completed.stdout
    print(
        json.dumps(
            {
                "wheel": wheels[0].name,
                "sdist": sources[0].name,
                "public_apis": 13,
                "example_assets": len(required),
                "offline_example": "passed",
            }
        )
    )


if __name__ == "__main__":
    main()
