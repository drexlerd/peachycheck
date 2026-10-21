"""The sample organization of tests/fixtures/org is in use in every test."""

from pathlib import Path

import pytest

from plumbcheck import engine

ORG = Path(__file__).parent / "fixtures" / "org"


@pytest.fixture(autouse=True)
def organization(monkeypatch: pytest.MonkeyPatch) -> None:
    """Use the sample organization; each test restores what it changed."""
    monkeypatch.setattr(engine, "IN_USE", engine.InUse())
    engine.use(ORG)


@pytest.fixture(autouse=True)
def counters(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The Stop hook counts its attempts in the test's folder."""
    from plumbcheck import hooks

    monkeypatch.setattr(hooks, "COUNTERS", tmp_path / "counters")
