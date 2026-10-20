"""The command line ``verdog-standards``: check, show, recognise, sync and run.

A repository's type is recognised from marker files; each type lives in
``templates/<type>/`` with ``type.toml`` (markers, the types it replaces and
its commands) and ``files/``, the root tool configurations every repository
of that type shares. ``sync`` writes them, ``sync --check`` fails on any
difference and ``run`` executes the types' commands, as the shared workflow
does.
"""

import argparse
import filecmp
import graphlib
import json
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, cast

from verdog_standards import engine, sources


def project_types() -> dict[str, dict[str, Any]]:
    """Every type of ``templates/``, by name."""
    return {
        folder.name: tomllib.loads((folder / "type.toml").read_text())
        for folder in sorted(engine.TEMPLATES.iterdir())
        if (folder / "type.toml").is_file()
    }


def types_of(root: Path) -> list[str]:
    """The types whose markers all exist in ``root``, minus replaced ones."""
    known = project_types()
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


def commands_of(root: Path, name: str) -> list[str]:
    """The commands of type ``name`` in ``root``.

    A ``standards`` preset of a kind in ``presets`` replaces the command at
    that kind's position.
    """
    spec = project_types()[name]
    commands = list(spec["commands"])
    found = _presets(root)
    for i, (kind, line) in enumerate(spec.get("presets", {}).items()):
        if kind in found:
            commands[i] = line
    return commands


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


def _files(name: str) -> list[Path]:
    folder = engine.TEMPLATES / name / "files"
    # A linter run in the repository leaves its cache next to a template config.
    return sorted(
        p
        for p in folder.rglob("*")
        if p.is_file() and ".ruff_cache" not in p.relative_to(folder).parts
    )


def sync(root: Path, *, check: bool) -> list[str]:
    """Write the types' configs into ``root``; with ``check``, only compare.

    Returns:
        The files that differ (``check``) or were written.
    """
    differing: list[str] = []
    for name in types_of(root):
        folder = engine.TEMPLATES / name / "files"
        for source in _files(name):
            target = root / source.relative_to(folder)
            if target.is_file() and filecmp.cmp(source, target, shallow=False):
                continue
            differing.append(str(target.relative_to(root)))
            if not check:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
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


def base_of(root: Path) -> str:
    """The merge base of ``HEAD`` with the main branch, else ``HEAD~1``."""
    branch = (
        f"origin/{engine.MAIN_BRANCH}" if engine.MAIN_BRANCH else "origin/HEAD"
    )
    found = subprocess.run(
        ["git", "merge-base", "HEAD", branch],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    return found.stdout.strip() if found.returncode == 0 else "HEAD~1"


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


def check_branch(root: Path, base: str) -> int:
    """Fail when files changed since ``base`` gain findings."""
    errors = engine.changed_errors(root, base)
    print("\n".join(errors) or "no new findings")
    return 1 if errors else 0


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
    branch = commands.add_parser(
        "check-branch", help="no new findings since a base"
    )
    branch.add_argument(
        "--base", help="default: the merge base with origin/HEAD"
    )
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
        "check-branch": lambda: check_branch(
            root, options.base or base_of(root)
        ),
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
