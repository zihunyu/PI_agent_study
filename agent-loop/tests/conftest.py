from __future__ import annotations

from pathlib import Path

import pytest


def pytest_sessionstart(session: pytest.Session) -> None:
    """Fail fast when pytest resolves ``pi_agent_loop`` from another checkout."""

    import pi_agent_loop

    expected_package = (
        Path(__file__).resolve().parents[1] / "src" / "pi_agent_loop"
    ).resolve()
    imported_package = Path(pi_agent_loop.__file__).resolve().parent

    try:
        imported_package.relative_to(expected_package)
    except ValueError as error:
        raise pytest.UsageError(
            "pytest imported pi_agent_loop from the wrong checkout: "
            f"expected under {expected_package}, got {imported_package}. "
            "Install this checkout (python -m pip install -e .) or run pytest "
            "from its project directory."
        ) from error
