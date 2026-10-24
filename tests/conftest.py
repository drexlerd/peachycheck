"""What every test shares."""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def counters(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The Stop hook counts its attempts in the test's folder."""
    from peachycheck import hooks

    monkeypatch.setattr(hooks, "COUNTERS", tmp_path / "counters")


@pytest.fixture(autouse=True)
def results(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Kept lint results go to the test's folder, not the user's cache."""
    from peachycheck import linting

    monkeypatch.setattr(linting, "CACHE", tmp_path / "results")
