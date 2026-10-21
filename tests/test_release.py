"""Releases: the version, init and upgrade, pinned hooks and the packages."""

import io
import re
import subprocess
import tarfile
import tomllib
import zipfile
from pathlib import Path

import pytest

from verdog_standards import cli, config, engine, hooks
from verdog_standards import sources as s

ROOT = Path(__file__).resolve().parents[1]


def test_the_version_is_the_project_s(
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert cli.version() == project["version"]
    with pytest.raises(SystemExit):
        cli.main(["--version"])
    assert capsys.readouterr().out == f"verdog-standards {cli.version()}\n"


@pytest.fixture
def release(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Path, str]]:
    """verdog-standards 0.2.0 from PyPI, offline; records ``uv add``."""
    monkeypatch.setattr(cli, "released", lambda: True)
    monkeypatch.setattr(cli, "version", lambda: "0.2.0")
    monkeypatch.setattr(cli, "install", lambda: 0)
    organization = config.ORGANIZATION.read_text()

    def text(source: str) -> str:
        """The organization's file stands in for every URL."""
        return (
            organization
            if source.startswith("https://")
            else Path(source).read_text()
        )

    monkeypatch.setattr(config, "_text", text)
    added: list[tuple[Path, str]] = []

    def uv_add(root: Path, requirement: str) -> None:
        added.append((root, requirement))
        pyproject = root / "pyproject.toml"
        pyproject.write_text(f'[dependency-groups]\ndev = ["{requirement}"]\n')

    monkeypatch.setattr(cli, "_uv_add", uv_add)
    return added


def _url(ref: str) -> str:
    return cli.ORGANIZATION_URL.format(ref=ref)


def test_init_sets_a_repository_up_once(
    tmp_path: Path,
    release: list[tuple[Path, str]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    (tmp_path / "uv.lock").write_text("")
    (tmp_path / ".gitmodules").write_text("")
    assert cli.main(["--root", str(tmp_path), "init"]) == 0
    own = (tmp_path / "standards.yml").read_text()
    assert own.endswith(f"EXTENDS: {_url('v0.2.0')}\n")
    workflow = (tmp_path / ".github/workflows/standards.yml").read_text()
    assert workflow == cli.WORKFLOW + "    secrets: inherit\n"
    assert (tmp_path / "ruff.toml").is_file()
    assert release == [(tmp_path, "verdog-standards==0.2.0")]
    first = capsys.readouterr().out
    assert "standards.yml: written" in first
    assert "verdog-standards==0.2.0 in the dev dependencies" in first
    assert cli.main(["--root", str(tmp_path), "init"]) == 0
    again = capsys.readouterr().out
    assert "written" not in again and "already" in again
    assert len(release) == 1


def test_a_git_install_extends_its_commit_and_pins_nothing(
    tmp_path: Path,
    release: list[tuple[Path, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "released", lambda: False)
    monkeypatch.setattr(cli, "_commit", lambda: "abc123")
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    assert cli.init(tmp_path) == 0
    own = (tmp_path / "standards.yml").read_text()
    assert own.endswith(f"EXTENDS: {_url('abc123')}\n")
    assert release == []
    assert (tmp_path / ".github/workflows/standards.yml").read_text() == (
        cli.WORKFLOW
    )


def test_upgrade_moves_extends_and_the_pin(
    tmp_path: Path,
    release: list[tuple[Path, str]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    own = tmp_path / "standards.yml"
    own.write_text(f"EXTENDS: {_url('abc123')}\nDISABLE_LINTERS: [X]\n")
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[dependency-groups]\ndev = ["verdog-standards"]\n')
    assert cli.upgrade(tmp_path, None) == 0
    assert own.read_text() == (
        f"EXTENDS: {_url('v0.2.0')}\nDISABLE_LINTERS: [X]\n"
    )
    assert release == [(tmp_path, "verdog-standards==0.2.0")]
    assert cli.main(["--root", str(tmp_path), "upgrade", "--to", "v0.3.1"]) == 0
    assert _url("v0.3.1") in own.read_text()
    assert release[-1] == (tmp_path, "verdog-standards==0.3.1")
    capsys.readouterr()
    assert cli.upgrade(tmp_path, "0.3.1") == 0
    assert len(release) == 2
    assert "already" in capsys.readouterr().out
    # Without a pin there is nothing to move.
    pyproject.write_text("[project]\nname = 'x'\n")
    assert cli.upgrade(tmp_path, "0.4.0") == 0
    assert len(release) == 2
    assert cli.upgrade(tmp_path / "missing", None) == 1


def _venv(root: Path, version: str, answer: str) -> None:
    """``root``'s .venv with verdog-standards ``version`` answering ``answer``."""
    site = root / ".venv/lib/python3.12/site-packages"
    (site / f"verdog_standards-{version}.dist-info").mkdir(parents=True)
    command = root / ".venv/bin/verdog-standards"
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
    (tmp_path / "standards.yml").write_text(f"EXTENDS: {config.ORGANIZATION}\n")
    _venv(tmp_path, "0.0.1", "echo pinned >&2; exit 2")
    assert hooks.pinned(tmp_path) == tmp_path / ".venv/bin/verdog-standards"
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
    (tmp_path / "standards.yml").write_text(f"EXTENDS: {config.ORGANIZATION}\n")
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
    return sorted(out.glob("verdog_standards-*"))


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


def test_no_package_contains_the_core_guidelines(dists: list[Path]) -> None:
    assert sorted(d.suffix for d in dists) == [".gz", ".whl"]
    # Their rules' titles, as test_sources checks the committed files.
    guide = engine.guides()["cpp-core-guidelines"]
    rules = s.read_index(engine.tree(guide))
    titles = {r.title.partition(": ")[2] for r in rules} - {""}
    quoted = re.compile("|".join(re.escape(t) for t in titles if len(t) > 24))
    for dist in dists:
        members = _members(dist)
        # Only names: a failure must not print the guide's text.
        found = [
            name
            for name, content in members.items()
            if "cpp-core-guidelines" in name
            or quoted.search(content.decode(errors="ignore"))
        ]
        assert found == [], dist.name


def test_the_wheel_carries_every_license(dists: list[Path]) -> None:
    names = set(_members(next(d for d in dists if d.suffix == ".whl")))
    licenses = [n for n in names if ".dist-info/licenses/" in n]
    bundled = [
        p.relative_to(ROOT).as_posix()
        for pattern in ("standards/**/LICENSE", "rules/**/LICENSE")
        for p in ROOT.glob(pattern)
    ]
    assert bundled
    for path in ["LICENSE", *bundled]:
        assert any(n.endswith(f"/licenses/{path}") for n in licenses), path
