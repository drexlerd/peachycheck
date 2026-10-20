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
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from verdog_standards import engine


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
    return [name for name in found if name not in replaced]


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
        for line in project_types()[name]["commands"]:
            print(f"::group::{name}: {line}", flush=True)
            code = subprocess.run(line, shell=True, cwd=root).returncode
            print("::endgroup::", flush=True)
            if code:
                print(f"{name}: `{line}` failed ({code})")
                return code
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


def check(with_tests: bool) -> int:
    """Lint the instance, its metrics and, with ``with_tests``, its examples."""
    standards = engine.load()
    for finding in engine.coherence(engine.graph()):
        print(f"warning ({finding.rule}): {finding.message}")
    errors = engine.lint_all() + engine.metric_tool_errors()
    for extra in engine.CHECKS:
        errors += extra()
    if with_tests:
        errors += engine.example_tests(standards)
    print("\n".join(errors) or f"{len(standards)} standards, no problems.")
    return 1 if errors else 0


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
    checking = commands.add_parser(
        "check", help="lint the standards themselves"
    )
    checking.add_argument(
        "--tests", action="store_true", help="run example tests"
    )
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
    commands.add_parser("types", help="the repository's recognised types")
    syncing = commands.add_parser("sync", help="write the types' tool configs")
    syncing.add_argument("--check", action="store_true", help="only compare")
    commands.add_parser("run", help="run the types' commands")
    return result


def main(arguments: Sequence[str] | None = None) -> int:
    """Run one command of ``verdog-standards``."""
    options = parser().parse_args(arguments)
    root: Path = options.root.resolve()
    if options.command == "check":
        return check(options.tests)
    if options.command == "check-branch":
        return check_branch(root, options.base or base_of(root))
    if options.command == "types":
        print("\n".join(types_of(root)) or "no known type")
        return 0
    if options.command == "sync":
        differing = sync(root, check=options.check)
        verb = "differs from the standards" if options.check else "written"
        print("\n".join(f"{path}: {verb}" for path in differing) or "in sync")
        return 1 if differing and options.check else 0
    if options.command == "run":
        return run(root)
    if options.html:
        page = engine.graph_page(engine.graph(), root.name)
        options.html.write_text(f"{engine.PAGE_FRAME}\n{page}")
        print(options.html)
        return 0
    return show(root, options.files)


if __name__ == "__main__":
    sys.exit(main())
