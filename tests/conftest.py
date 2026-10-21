"""What every test shares."""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def counters(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The Stop hook counts its attempts in the test's folder."""
    from plumbcheck import hooks

    monkeypatch.setattr(hooks, "COUNTERS", tmp_path / "counters")
