"""The command line ``plumbcheck``: lint, check, show and sync.

``FILES`` of a repository's ``plumbcheck.yml`` names the tool configurations
it shares: those of ``general`` with every repository, the others with each
repository that has their marker file. ``sync`` writes them, ``sync --check``
fails on any difference; ``lint`` runs the linters and ``hook`` answers the
agents' hooks. ``init`` sets a repository up, ``upgrade`` moves it to
another version. Builds and tests are the repository's own CI's.
"""

import argparse
import filecmp
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, Final, cast

from plumbcheck import config, engine, hooks, linting, sources
from plumbcheck.engine import GENERAL

PACKAGE = "plumbcheck"
WORKFLOW = """name: plumbcheck

on:
  pull_request:
  push:
    branches: [main]

jobs:
  plumbcheck:
    runs-on: ubuntu-latest
    steps:
      - uses: drexlerd/plumbcheck@{ref}
"""
# The action or the reusable workflow at any ref, and the pinned version.
USES = re.compile(
    r"(drexlerd/plumbcheck(?:/\.github/workflows/check\.yml)?@)[^\s#]+"
)
PIN = re.compile(r"^PLUMBCHECK_VERSION:.*$", re.M)


def version() -> str:
    """The installed version of plumbcheck."""
    return importlib.metadata.version(PACKAGE)


def released() -> bool:
    """Installed from a built package, not from a git or folder source."""
    try:
        found = importlib.metadata.distribution(PACKAGE)
    except importlib.metadata.PackageNotFoundError:
        return False
    direct = json.loads(found.read_text("direct_url.json") or "{}")
    return not {"vcs_info", "dir_info"} & set(direct)


def _commit() -> str:
    """The commit of plumbcheck this engine was installed from."""
    try:
        found = importlib.metadata.distribution(PACKAGE)
        direct = json.loads(found.read_text("direct_url.json") or "{}")
        commit = str(direct.get("vcs_info", {}).get("commit_id", ""))
    except importlib.metadata.PackageNotFoundError:
        commit = ""
    if commit:
        return commit
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=Path(__file__).parent,
        capture_output=True,
        text=True,
    )
    return head.stdout.strip() if head.returncode == 0 else "main"


def base_files(root: Path) -> dict[str, Path]:
    """What ``.plumbcheck/`` of ``root`` holds: each file, by its path there.

    ``FILES`` names them under ``general`` for every repository, and under a
    marker file, such as ``pyproject.toml``, for each repository that has
    it; a later entry wins, and a folder brings every file below it.
    """
    files = cast(dict[str, dict[str, str]], config.load(root).get("FILES", {}))
    wanted: dict[str, Path] = {}
    for name, entries in files.items():
        if name != GENERAL and not (root / name).exists():
            continue
        for target, source_name in entries.items():
            source = Path(source_name)
            below = sorted(p for p in source.rglob("*") if p.is_file())
            for path in below if source.is_dir() else [source]:
                inner = path.relative_to(source) if source.is_dir() else ""
                wanted[(Path(target) / inner).as_posix()] = path
    return wanted


def sync(root: Path, *, check: bool) -> list[str]:
    """Write ``.plumbcheck/`` of ``root``; with ``check``, only compare.

    The folder holds the base configurations of the repository, as
    ``base_files`` lists them, and nothing else.

    Returns:
        The files of ``.plumbcheck/`` that differ (``check``) or were
        written or removed.
    """
    folder = root / config.BASE
    wanted = base_files(root)
    found = folder.rglob("*") if folder.is_dir() else iter(list[Path]())
    present = {p.relative_to(folder).as_posix() for p in found if p.is_file()}
    present -= _ignored(folder, present) if present else set()
    differing = sorted(
        {n for n in present if n not in wanted}
        | {
            n
            for n, source in wanted.items()
            if n not in present
            or not filecmp.cmp(source, folder / n, shallow=False)
        }
    )
    if not check:
        for name in differing:
            target = folder / name
            target.unlink(missing_ok=True)
            if name in wanted:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(wanted[name], target)
    return [f"{config.BASE}/{n}" for n in differing]


def _ignored(folder: Path, names: set[str]) -> set[str]:
    """The ``names`` in ``folder`` that git ignores, such as a tool's cache."""
    checked = subprocess.run(
        ["git", "check-ignore", "--stdin"],
        cwd=folder,
        input="\n".join(sorted(names)),
        capture_output=True,
        text=True,
    )
    return set(checked.stdout.splitlines()) & names


# What a repository's own configuration needs to inherit the base; ``init``
# writes it when the repository has none.
INHERIT: Final = {
    "ruff.toml": f'extend = "{config.BASE}/ruff.toml"\n',
    "pyrightconfig.json": (
        f'{{\n  "extends": "{config.BASE}/pyrightconfig.json"\n}}\n'
    ),
    "eslint.config.mjs": (
        f'import base from "./{config.BASE}/eslint.config.mjs";\n\n'
        "export default [...base];\n"
    ),
}


def inherit(root: Path) -> list[str]:
    """Write the repository's own configurations that inherit the base.

    Only for a base file of ``.plumbcheck/`` whose tool can extend another
    file, and only where the repository has no configuration yet.
    """
    pyproject = root / "pyproject.toml"
    ruff_there = pyproject.is_file() and "[tool.ruff" in pyproject.read_text()
    written: list[str] = []
    for name, text in INHERIT.items():
        own = root / name
        if not (root / config.BASE / name).is_file() or own.is_file():
            continue
        if name == "ruff.toml" and ruff_there:
            continue
        own.write_text(text)
        written.append(name)
    return written


def _uv_add(root: Path, requirement: str) -> None:
    subprocess.run(["uv", "add", "--dev", requirement], cwd=root, check=True)


def _dev_dependency(root: Path, wanted: str | None, *, add: bool) -> str:
    """Pin ``plumbcheck==wanted`` in the dev group of ``root``.

    Without ``add``, only an existing pin moves. A generated pyproject.toml
    (see ``GENERATED``) gets none.

    Returns:
        What was done, or "" when ``root`` is no Python project.
    """
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file() or "pyproject.toml" in engine.generated(root):
        return ""
    if wanted is None:
        return "pyproject.toml: no dev pin, not installed from PyPI" * add
    requirement = f"{PACKAGE}=={wanted}"
    groups = tomllib.loads(pyproject.read_text()).get("dependency-groups", {})
    dev = [d for d in groups.get("dev", []) if isinstance(d, str)]
    if requirement in dev:
        return f"pyproject.toml: {requirement} already"
    if not add and not any(re.match(r"plumbcheck\b", d) for d in dev):
        return ""
    _uv_add(root, requirement)
    return f"pyproject.toml: {requirement} in the dev dependencies"


def _pin() -> str:
    """The version plumbcheck.yml pins: the release, else the commit."""
    return version() if released() else _commit()


def _ref(pin: str) -> str:
    """The ref of the action for ``pin``: a release's tag, else ``pin``."""
    return f"v{pin}" if re.match(r"\d+\.\d+", pin) else pin


def init(root: Path, extends: str | None = None) -> int:
    """Set ``root`` up: plumbcheck.yml, the CI workflow, configs, tools, pin."""
    own = root / config.NAME
    if own.is_file():
        print(f"{config.NAME}: exists")
    else:
        own.write_text(
            "# plumbcheck's configuration: "
            "https://github.com/drexlerd/plumbcheck#configuration\n"
            + (f"EXTENDS: {extends}\n" if extends else "")
            + f"PLUMBCHECK_VERSION: {_pin()}\n"
        )
        print(f"{config.NAME}: written")
    workflow = root / ".github" / "workflows" / "plumbcheck.yml"
    if workflow.is_file():
        print(".github/workflows/plumbcheck.yml: exists")
    else:
        private = (root / ".gitmodules").is_file()
        workflow.parent.mkdir(parents=True, exist_ok=True)
        token = (
            "        with:\n          token: ${{ secrets.SUBMODULES_TOKEN }}\n"
        )
        workflow.write_text(WORKFLOW.format(ref=_ref(_pin())) + token * private)
        print(".github/workflows/plumbcheck.yml: written")
    _sync(root, check_only=False)
    for name in inherit(root):
        print(f"{name}: written, extending {config.BASE}/{name}")
    install(root)
    pinned = _dev_dependency(root, version() if released() else None, add=True)
    if pinned:
        print(pinned)
    return 0


def _moved(root: Path, path: Path, pattern: re.Pattern[str], line: str) -> None:
    """Replace ``pattern``'s matches in ``path`` by ``line``, a replacement."""
    name = path.relative_to(root).as_posix()
    text = path.read_text()
    moved = pattern.sub(line, text)
    if moved != text:
        path.write_text(moved)
        print(f"{name}: moved")
    elif pattern.search(text):
        print(f"{name}: already")


def upgrade(root: Path, to: str | None) -> int:
    """Move the pins of plumbcheck to ``to`` (or the installed version).

    The pins are ``PLUMBCHECK_VERSION`` of plumbcheck.yml, the refs of the
    action and the reusable workflow in ``.github/workflows`` and the dev
    dependency.
    """
    own = root / config.NAME
    if not own.is_file():
        print(f"no {config.NAME}; run `plumbcheck init` first")
        return 1
    wanted = to.removeprefix("v") if to else None
    if wanted is None and released():
        wanted = version()
    pin = wanted or _commit()
    if not PIN.search(own.read_text()):
        own.write_text(own.read_text() + f"PLUMBCHECK_VERSION: {pin}\n")
    _moved(root, own, PIN, f"PLUMBCHECK_VERSION: {pin}")
    for workflow in sorted((root / ".github" / "workflows").glob("*.y*ml")):
        _moved(root, workflow, USES, f"\\g<1>{_ref(pin)}")
    pinned = _dev_dependency(root, wanted, add=False)
    if pinned:
        print(pinned)
    return _sync(root, check_only=False)


# The empty tree: diffing against it lists every tracked file.
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def base_of(root: Path) -> str:
    """The merge base of ``HEAD`` with the main branch, else ``HEAD~1``.

    A repository with neither diffs against the empty tree.

    The main branch is ``origin/MAIN_BRANCH``, else ``origin/HEAD``, which
    many clones lack, else ``origin/main``.
    """
    branches = (
        [f"origin/{engine.MAIN_BRANCH}"]
        if engine.MAIN_BRANCH
        else ["origin/HEAD", "origin/main"]
    )
    for branch in branches:
        found = subprocess.run(
            ["git", "merge-base", "HEAD", branch],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        if found.returncode == 0:
            return found.stdout.strip()
    previous = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", "HEAD~1"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    # Without a main branch or a parent commit every file counts as changed.
    return "HEAD~1" if previous.returncode == 0 else EMPTY_TREE


def check() -> int:
    """Lint the instance and its metrics."""
    if engine.instance() is None:
        print(f"no GUIDES in {config.NAME}; nothing to check")
        return 0
    standards = engine.load()
    for finding in engine.coherence(engine.graph()):
        print(f"warning ({finding.rule}): {finding.message}")
    errors = engine.lint_all() + engine.metric_tool_errors()
    errors += engine.source_errors()
    for extra in engine.CHECKS:
        errors += extra()
    print("\n".join(errors) or f"{len(standards)} standards, no problems.")
    return 1 if errors else 0


def index(names: Sequence[str]) -> int:
    """Generate the trees of the guides ``names``, or of every guide."""
    known = engine.guides()
    unknown = sorted(set(names) - set(known))
    if unknown:
        print(f"unknown guides: {', '.join(unknown)}")
        return 1
    for source in (known[n] for n in names or known):
        rules = engine.generate(source)
        leaves = [r for r in rules if r.leaf]
        checked = sum(bool(r.checked_by) for r in leaves)
        print(
            f"{source.id}: {len(leaves)} rules ({checked} checked, "
            f"{len(leaves) - checked} prose) in {len(rules) - len(leaves)} "
            f"sections, depth {sources.depth(rules)}, store {source.store}"
        )
    return 0


def lint(root: Path, options: argparse.Namespace) -> int:
    """Lint the given files, those changed since ``--base``, or ``--all``.

    Each file is linted with the repository it belongs to (see
    ``config.owner``); a file of another repository shows its path from
    ``root``.
    """
    if options.all:
        paths = [root / p for p in linting.all_files(root)]
    elif options.files:
        paths = [Path(f).resolve() for f in options.files]
    else:
        base = options.base or base_of(root)
        paths = [root / p for p in linting.changed_files(root, base)]
    messages: list[str] = []
    for repo, result in linting.lint_files(paths, fix=options.fix):
        prefix = os.path.relpath(repo, root)
        if prefix != ".":
            result.findings = [
                replace(f, path=f"{prefix}/{f.path}") for f in result.findings
            ]
        messages += linting.report(repo, result, limit=10**9)
    print("\n".join(messages) or f"{len(paths)} files, no findings")
    return 1 if messages else 0


def install(root: Path | None = None) -> int:
    """Download the pinned tools of ``VERSIONS``."""
    versions = config.load(root).get("VERSIONS", {})
    print("\n".join(linting.install(versions)))
    return 0


def install_hooks(options: argparse.Namespace) -> int:
    """Write or remove the user's Claude Code and Codex hooks; print the diff."""
    both = not options.claude and not options.codex
    remove = options.action == "uninstall"
    diff = hooks.install(
        Path.home(),
        claude=options.claude or both,
        codex=options.codex or both,
        remove=remove,
    )
    print(diff or f"the hooks are {'removed' if remove else 'installed'}")
    if (options.codex or both) and not remove:
        print("Trust the Codex hooks once with /hooks.")
    return 0


def show(root: Path, files: Sequence[str]) -> int:
    """What applies to ``files``, or the overview of every language."""
    standards = engine.load()
    if not files:
        print(engine.overview(standards))
        return 0
    paths = [str(Path(f).resolve().relative_to(root)) for f in files]
    for activation in engine.select(standards, paths, root):
        print(f"{activation.language}: {'; '.join(activation.reasons)}")
        for guideline in activation.guidelines:
            print(f"  {guideline['key']}: {guideline['text']}")
    return 0


def parser() -> argparse.ArgumentParser:
    """The command line; a consumer may add arguments before parsing."""
    result = argparse.ArgumentParser(prog="plumbcheck")
    result.add_argument("--root", type=Path, default=Path.cwd())
    result.add_argument(
        "--version", action="version", version=f"%(prog)s {version()}"
    )
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="lint the guides of GUIDES")
    commands.add_parser("install", help="download the pinned linters")
    linted = commands.add_parser("lint", help="run the linters")
    linted.add_argument("files", nargs="*")
    linted.add_argument("--all", action="store_true", help="every file")
    linted.add_argument(
        "--base", help="files changed since; default: the merge base"
    )
    linted.add_argument("--fix", action="store_true", help="fix first")
    hook = commands.add_parser("hook", help="answer a Claude or Codex hook")
    hook.add_argument("event", choices=["post-edit", "stop"])
    installing = commands.add_parser(
        "hooks", help="install or uninstall the agents' hooks"
    )
    installing.add_argument("action", choices=["install", "uninstall"])
    installing.add_argument("--claude", action="store_true")
    installing.add_argument("--codex", action="store_true")
    showing = commands.add_parser("show", help="what applies to files, or all")
    showing.add_argument("files", nargs="*")
    showing.add_argument(
        "--html", type=Path, help="write the browsable map here"
    )
    showing.add_argument(
        "--coverage",
        action="store_true",
        help="rules with a checker vs. prose, per guide and section",
    )
    indexing = commands.add_parser(
        "index", help="generate the guides' trees from their pinned sources"
    )
    indexing.add_argument("guides", nargs="*", help="default: every guide")
    syncing = commands.add_parser("sync", help="write the shared tool configs")
    syncing.add_argument("--check", action="store_true", help="only compare")
    initializing = commands.add_parser(
        "init", help="set the repository up: plumbcheck.yml, CI, configs"
    )
    initializing.add_argument(
        "--extends", help="the URL or path of the file plumbcheck.yml extends"
    )
    upgrading = commands.add_parser(
        "upgrade", help="move the pins of plumbcheck to a version"
    )
    upgrading.add_argument("--to", help="default: the installed version")
    return result


def _sync(root: Path, check_only: bool) -> int:
    differing = sync(root, check=check_only)
    verb = "differs; run `plumbcheck sync`" if check_only else "written"
    print("\n".join(f"{path}: {verb}" for path in differing) or "in sync")
    return 1 if differing and check_only else 0


def _show(root: Path, options: argparse.Namespace) -> int:
    if options.coverage:
        print(engine.coverage(), end="")
        return 0
    if options.html is None:
        return show(root, options.files)
    page = engine.graph_page(engine.graph(), root.name)
    options.html.write_text(f"{engine.PAGE_FRAME}\n{page}")
    print(options.html)
    return 0


def main(arguments: Sequence[str] | None = None) -> int:
    """Run one command of ``plumbcheck``."""
    options = parser().parse_args(arguments)
    root: Path = options.root.resolve()
    if options.command in ("check", "show", "index"):
        engine.use(root)
    commands: dict[str, Callable[[], int]] = {
        "check": check,
        "install": lambda: install(root),
        "lint": lambda: lint(root, options),
        "hook": lambda: hooks.run(options.event),
        "hooks": lambda: install_hooks(options),
        "index": lambda: index(options.guides),
        "sync": lambda: _sync(root, options.check),
        "init": lambda: init(root, options.extends),
        "upgrade": lambda: upgrade(root, options.to),
        "show": lambda: _show(root, options),
    }
    return commands[options.command]()


if __name__ == "__main__":
    sys.exit(main())
