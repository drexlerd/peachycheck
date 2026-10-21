"""The command line ``verdog-standards``: lint, check, show, sync and run.

A repository's type is recognised from marker files; the ``PROJECT_TYPES`` of
its ``standards.yml`` name each type's markers, the types it replaces and its
commands, and ``FILES`` the tool configurations from ``rules/`` every
repository of that type shares. ``sync`` writes them, ``sync --check`` fails
on any difference and ``run`` executes the types' commands, as the shared
workflow does; ``lint`` runs the linters and ``hook`` answers the agents'
hooks.
"""

import argparse
import filecmp
import graphlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, cast

from verdog_standards import config, engine, hooks, linting, sources

ORGANIZATION_URL = "https://raw.githubusercontent.com/verdog-ai/standards/{commit}/standards.yml"


def project_types(root: Path | None = None) -> dict[str, dict[str, Any]]:
    """The ``PROJECT_TYPES`` of ``root``'s configuration, by name."""
    return cast(
        dict[str, dict[str, Any]], config.load(root).get("PROJECT_TYPES", {})
    )


def types_of(root: Path) -> list[str]:
    """The types whose markers all exist in ``root``, minus replaced ones."""
    known = project_types(root)
    found = [
        name
        for name, spec in known.items()
        if all((root / marker).exists() for marker in spec["markers"])
    ]
    replaced = {r for name in found for r in known[name].get("replaces", ())}
    return _ordered(known, [name for name in found if name not in replaced])


def _ordered(known: dict[str, dict[str, Any]], kept: list[str]) -> list[str]:
    """``kept``, each type after the types of its ``after`` that it has."""
    order = graphlib.TopologicalSorter(
        {n: [a for a in known[n].get("after", ()) if a in kept] for n in kept}
    )
    return list(order.static_order())


def _presets(root: Path) -> set[str]:
    """The kinds of CMake presets named ``standards``, e.g. ``testPresets``."""
    kinds: set[str] = set()
    for name in ("CMakePresets.json", "CMakeUserPresets.json"):
        if (root / name).is_file():
            presets: dict[str, Any] = json.loads((root / name).read_text())
            for kind, entries in presets.items():
                if not isinstance(entries, list):
                    continue  # ``version``, ``cmakeMinimumRequired``, ...
                named = cast(list[dict[str, Any]], entries)
                if any(e.get("name") == "standards" for e in named):
                    kinds.add(kind)
    return kinds


def commands_of(root: Path, name: str, *, setup: bool = True) -> list[str]:
    """The commands of type ``name`` in ``root``: its ``setup``, then checks.

    A ``standards`` preset of a kind in ``presets`` replaces the command at
    that kind's position. Without ``setup``, the setup commands are left out,
    unless the type ``creates`` a path that ``root`` lacks.
    """
    spec = project_types(root)[name]
    first = list(spec.get("setup", []))
    commands = first + list(spec["commands"])
    found = _presets(root)
    for i, (kind, line) in enumerate(spec.get("presets", {}).items()):
        if kind in found:
            commands[i] = line
    creates = spec.get("creates")
    if setup or (creates and not (root / creates).exists()):
        return commands
    return commands[len(first) :]


def _uv(pyproject: Path) -> dict[str, Any]:
    return tomllib.loads(pyproject.read_text()).get("tool", {}).get("uv", {})


def siblings(root: Path) -> list[str]:
    """The ``<name>`` of each uv path source ``../<name>`` of ``root``.

    The sources of the root project and of its workspace members count.
    """
    if not (root / "pyproject.toml").is_file():
        return []
    members = (
        _uv(root / "pyproject.toml").get("workspace", {}).get("members", [])
    )
    projects = [root, *(m for g in members for m in sorted(root.glob(g)))]
    names: set[str] = set()
    for project in projects:
        if not (project / "pyproject.toml").is_file():
            continue
        sources = _uv(project / "pyproject.toml").get("sources", {}).values()
        for source in sources:
            # ponytail: a list of sources (per platform marker) is skipped
            if isinstance(source, dict) and "path" in source:
                path = cast(str, source["path"])
                folder = (project / path).resolve()
                if folder.parent == root.resolve().parent:
                    names.add(folder.name)
    return sorted(names)


def _siblings(root: Path) -> int:
    print("\n".join(siblings(root)))
    return 0


def _commit() -> str:
    """The commit of verdog-standards this engine was installed from."""
    try:
        found = importlib.metadata.distribution("verdog-standards")
        direct = json.loads(found.read_text("direct_url.json") or "{}")
        commit = str(direct.get("vcs_info", {}).get("commit_id", ""))
    except importlib.metadata.PackageNotFoundError:
        commit = ""
    if commit:
        return commit
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=config.ORGANIZATION.parent,
        capture_output=True,
        text=True,
    )
    return head.stdout.strip() if head.returncode == 0 else "main"


def sync(root: Path, *, check: bool) -> list[str]:
    """Write the types' configs into ``root``; with ``check``, only compare.

    Without ``check``, a missing ``standards.yml`` is written too, extending
    the organization's file at this engine's commit.

    Returns:
        The files that differ (``check``) or were written.
    """
    files = cast(dict[str, dict[str, str]], config.load(root).get("FILES", {}))
    differing: list[str] = []
    for name in types_of(root):
        for target_name, source_name in files.get(name, {}).items():
            source = config.RULES.parent / source_name
            target = root / target_name
            if target.is_file() and filecmp.cmp(source, target, shallow=False):
                continue
            differing.append(target_name)
            if not check:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
    own = root / config.NAME
    if not check and not own.is_file():
        url = ORGANIZATION_URL.format(commit=_commit())
        own.write_text(
            "# The organization's standards; override keys below.\n"
            f"EXTENDS: {url}\n"
        )
        differing.append(config.NAME)
    return differing


def run(root: Path) -> int:
    """Run every command of the types of ``root``; the first failure stops."""
    for name in types_of(root):
        for line in commands_of(root, name):
            print(f"::group::{name}: {line}", flush=True)
            code = subprocess.run(line, shell=True, cwd=root).returncode
            print("::endgroup::", flush=True)
            if code:
                print(f"{name}: `{line}` failed ({code})")
                return code
    if (
        "configurePresets" in _presets(root)
        and not (root / "build/standards/compile_commands.json").is_file()
    ):
        print(
            "the `standards` configure preset must set binaryDir "
            "build/standards and CMAKE_EXPORT_COMPILE_COMMANDS=ON"
        )
        return 1
    return 0


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
    """Lint the given files, those changed since ``--base``, or ``--all``."""
    if options.all:
        paths = linting.all_files(root)
    elif options.files:
        paths = [
            os.path.relpath(Path(f).resolve(), root) for f in options.files
        ]
    else:
        paths = linting.changed_files(root, options.base or base_of(root))
    result = linting.lint(root, paths, fix=options.fix)
    messages = linting.report(root, result, limit=10**9)
    print("\n".join(messages) or f"{len(paths)} files, no findings")
    return 1 if messages else 0


def install() -> int:
    """Download the pinned tools of ``VERSIONS``."""
    versions = config.load().get("VERSIONS", {})
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
    result = argparse.ArgumentParser(prog="verdog-standards")
    result.add_argument("--root", type=Path, default=Path.cwd())
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="lint the standards themselves")
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
    commands.add_parser("types", help="the repository's recognised types")
    syncing = commands.add_parser("sync", help="write the types' tool configs")
    syncing.add_argument("--check", action="store_true", help="only compare")
    commands.add_parser("run", help="run the types' commands")
    commands.add_parser("siblings", help="the sibling repositories uv needs")
    return result


def _sync(root: Path, check_only: bool) -> int:
    differing = sync(root, check=check_only)
    verb = "differs from the standards" if check_only else "written"
    print("\n".join(f"{path}: {verb}" for path in differing) or "in sync")
    return 1 if differing and check_only else 0


def _types(root: Path) -> int:
    print("\n".join(types_of(root)) or "no known type")
    return 0


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
    """Run one command of ``verdog-standards``."""
    options = parser().parse_args(arguments)
    root: Path = options.root.resolve()
    commands: dict[str, Callable[[], int]] = {
        "check": check,
        "install": install,
        "lint": lambda: lint(root, options),
        "hook": lambda: hooks.run(options.event),
        "hooks": lambda: install_hooks(options),
        "index": lambda: index(options.guides),
        "types": lambda: _types(root),
        "sync": lambda: _sync(root, options.check),
        "run": lambda: run(root),
        "siblings": lambda: _siblings(root),
        "show": lambda: _show(root, options),
    }
    return commands[options.command]()


if __name__ == "__main__":
    sys.exit(main())
