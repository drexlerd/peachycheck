"""Releases: the version, init and upgrade, pinned hooks and the packages."""

import io
import subprocess
import tarfile
import tomllib
import zipfile
from pathlib import Path

import pytest

from plumbcheck import cli, config, hooks

ROOT = Path(__file__).resolve().parents[1]


def test_the_version_is_the_project_s(
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert cli.version() == project["version"]
    with pytest.raises(SystemExit):
        cli.main(["--version"])
    assert capsys.readouterr().out == f"plumbcheck {cli.version()}\n"


@pytest.fixture
def release(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Path, str]]:
    """plumbcheck 0.1.0 from PyPI, offline; records ``uv add``."""
    monkeypatch.setattr(cli, "released", lambda: True)
    monkeypatch.setattr(cli, "version", lambda: "0.1.0")

    def install(root: Path | None = None) -> int:
        del root
        return 0

    monkeypatch.setattr(cli, "install", install)
    added: list[tuple[Path, str]] = []

    def uv_add(root: Path, requirement: str) -> None:
        added.append((root, requirement))
        pyproject = root / "pyproject.toml"
        pyproject.write_text(f'[dependency-groups]\ndev = ["{requirement}"]\n')

    monkeypatch.setattr(cli, "_uv_add", uv_add)
    return added


def test_init_sets_a_repository_up_once(
    tmp_path: Path,
    release: list[tuple[Path, str]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    (tmp_path / "uv.lock").write_text("")
    (tmp_path / ".gitmodules").write_text("")
    org = str(Path(__file__).parent / "fixtures" / "org" / config.NAME)
    root = str(tmp_path)
    assert cli.main(["--root", root, "init", "--extends", org]) == 0
    own = (tmp_path / config.NAME).read_text()
    assert own.endswith(f"EXTENDS: {org}\nPLUMBCHECK_VERSION: 0.1.0\n")
    workflow = (tmp_path / ".github/workflows/plumbcheck.yml").read_text()
    assert workflow == (
        cli.WORKFLOW.format(ref="v0.1.0") + "    secrets: inherit\n"
    )
    assert (tmp_path / "ruff.toml").is_file()
    assert release == [(tmp_path, "plumbcheck==0.1.0")]
    first = capsys.readouterr().out
    assert "plumbcheck.yml: written" in first
    assert "plumbcheck==0.1.0 in the dev dependencies" in first
    assert cli.main(["--root", root, "init"]) == 0
    again = capsys.readouterr().out
    assert "written" not in again and "already" in again
    assert len(release) == 1


def test_a_git_install_pins_its_commit_and_adds_no_dev_pin(
    tmp_path: Path,
    release: list[tuple[Path, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "released", lambda: False)
    monkeypatch.setattr(cli, "_commit", lambda: "abc123")
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    assert cli.init(tmp_path) == 0
    own = (tmp_path / config.NAME).read_text()
    assert own.endswith("\nPLUMBCHECK_VERSION: abc123\n")
    assert "EXTENDS" not in own
    assert release == []
    assert (tmp_path / ".github/workflows/plumbcheck.yml").read_text() == (
        cli.WORKFLOW.format(ref="abc123")
    )


def test_upgrade_moves_the_pins(
    tmp_path: Path,
    release: list[tuple[Path, str]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    own = tmp_path / config.NAME
    own.write_text("PLUMBCHECK_VERSION: abc123\nDISABLE_LINTERS: [X]\n")
    workflow = tmp_path / ".github/workflows/check.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(cli.WORKFLOW.format(ref="abc123"))
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[dependency-groups]\ndev = ["plumbcheck"]\n')
    assert cli.upgrade(tmp_path, None) == 0
    assert own.read_text() == (
        "PLUMBCHECK_VERSION: 0.1.0\nDISABLE_LINTERS: [X]\n"
    )
    assert workflow.read_text() == cli.WORKFLOW.format(ref="v0.1.0")
    assert release == [(tmp_path, "plumbcheck==0.1.0")]
    assert cli.main(["--root", str(tmp_path), "upgrade", "--to", "v0.3.1"]) == 0
    assert "PLUMBCHECK_VERSION: 0.3.1\n" in own.read_text()
    assert workflow.read_text() == cli.WORKFLOW.format(ref="v0.3.1")
    assert release[-1] == (tmp_path, "plumbcheck==0.3.1")
    capsys.readouterr()
    assert cli.upgrade(tmp_path, "0.3.1") == 0
    assert len(release) == 2
    assert "already" in capsys.readouterr().out
    # Without a pin there is nothing to move.
    pyproject.write_text("[project]\nname = 'x'\n")
    assert cli.upgrade(tmp_path, "0.4.0") == 0
    assert len(release) == 2
    # A file without the version gets it.
    own.write_text("DISABLE_LINTERS: [X]\n")
    assert cli.upgrade(tmp_path, "0.4.0") == 0
    assert own.read_text().endswith("PLUMBCHECK_VERSION: 0.4.0\n")
    assert cli.upgrade(tmp_path / "missing", None) == 1


def _venv(root: Path, version: str, answer: str) -> None:
    """``root``'s .venv with plumbcheck ``version`` answering ``answer``."""
    site = root / ".venv/lib/python3.12/site-packages"
    (site / f"plumbcheck-{version}.dist-info").mkdir(parents=True)
    command = root / ".venv/bin/plumbcheck"
    command.parent.mkdir(parents=True)
    command.write_text(f"#!/bin/sh\ncat > /dev/null\n{answer}\n")
    command.chmod(0o755)


def _hook(monkeypatch: pytest.MonkeyPatch, root: Path) -> int:
    monkeypatch.setattr("sys.stdin", io.StringIO(f'{{"cwd": "{root}"}}'))
    return hooks.run("stop")


def test_a_hook_answers_with_the_repository_s_own_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / config.NAME).write_text("# hooked\n")
    _venv(tmp_path, "0.0.1", "echo pinned >&2; exit 2")
    assert hooks.pinned(tmp_path) == tmp_path / ".venv/bin/plumbcheck"
    assert _hook(monkeypatch, tmp_path) == 2
    assert capsys.readouterr().err == "pinned\n"
    # The same version answers here.
    other = tmp_path / "same"
    _venv(other, cli.version(), "exit 2")
    assert hooks.pinned(other) is None


def test_a_broken_pinned_version_falls_back_to_this_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / config.NAME).write_text("# hooked\n")
    _venv(tmp_path, "0.0.1", "echo crashed >&2; exit 1")

    def here(payload: dict[str, object]) -> hooks.Reply:
        del payload
        return hooks.Reply(stdout="here")

    monkeypatch.setattr(hooks, "stop", here)
    assert _hook(monkeypatch, tmp_path) == 0
    assert capsys.readouterr().out == "here\n"


@pytest.fixture(scope="module")
def dists(tmp_path_factory: pytest.TempPathFactory) -> list[Path]:
    """The sdist and the wheel, built from this checkout."""
    out = tmp_path_factory.mktemp("dist")
    subprocess.run(
        ["uv", "build", "--quiet", "--out-dir", str(out), str(ROOT)],
        check=True,
    )
    return sorted(out.glob("plumbcheck-*"))


def _members(dist: Path) -> dict[str, bytes]:
    if dist.suffix == ".whl":
        with zipfile.ZipFile(dist) as wheel:
            return {name: wheel.read(name) for name in wheel.namelist()}
    with tarfile.open(dist) as sdist:
        return {
            member.name: sdist.extractfile(member).read()  # type: ignore[union-attr]
            for member in sdist.getmembers()
            if member.isfile()
        }


def test_no_package_contains_guides_or_an_organization_s_content(
    dists: list[Path],
) -> None:
    assert sorted(d.suffix for d in dists) == [".gz", ".whl"]
    for dist in dists:
        members = _members(dist)
        # The defaults and their rules ship.
        assert any(n.endswith("defaults/plumbcheck.yml") for n in members)
        assert any(n.endswith("defaults/rules/.vale.ini") for n in members)
        # Only names: a failure must not print a guide's text.
        found = [
            name
            for name, content in members.items()
            if any(
                part in name
                for part in ("references/", "fixtures/", "overlay.toml")
            )
            or any(
                text in content.decode(errors="ignore")
                for text in ("verdog-ai", "CppCoreGuidelines", "isocpp")
            )
        ]
        assert found == [], dist.name


def test_the_wheel_carries_the_license(dists: list[Path]) -> None:
    names = set(_members(next(d for d in dists if d.suffix == ".whl")))
    assert any(n.endswith(".dist-info/licenses/LICENSE") for n in names)
