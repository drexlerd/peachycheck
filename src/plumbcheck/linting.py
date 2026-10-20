"""``verdog-standards lint``: the linters of ``standards.yml`` on a repository.

The files are the given ones, those changed since a base, or all; generated
files of a Verdog project and the pinned guides are never checked. Each
linter runs on the files of its extensions, and every finding fails. A
finding names its linter and rule; the report adds the guidelines the rule
maps to, with the leaf of each.
"""

import hashlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, cast

from verdog_standards import config, engine

BIN: Final = Path("~/.cache/verdog-standards/bin")
SHOWN_FINDINGS: Final = 20  # of a file, in a repair message
CHUNK: Final = 200  # files per run of a linter
DEFAULT_OK: Final = (0, 1)


@dataclass(frozen=True, slots=True, order=True)
class Finding:
    """One finding: where, which linter and rule, what it says."""

    path: str
    line: int
    linter: str
    rule: str
    message: str


@dataclass
class Result:
    """What a lint found, and what kept a linter from running."""

    findings: list[Finding] = field(default_factory=list[Finding])
    problems: list[str] = field(default_factory=list[str])


def search_path() -> str:
    """``PATH`` with the engine's environment and the downloaded tools first."""
    return os.pathsep.join(
        (
            str(Path(sys.executable).parent),
            str(BIN.expanduser()),
            os.environ.get("PATH", ""),
        )
    )


def _git(root: Path, *args: str) -> list[str]:
    listed = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True
    )
    return listed.stdout.splitlines()


def changed_files(root: Path, base: str) -> list[str]:
    """The files changed since ``base``, untracked ones too, nothing staged."""
    changed = _git(root, "diff", "--name-only", base, "--")
    untracked = _git(root, "ls-files", "--others", "--exclude-standard")
    return sorted(set(changed + untracked) - engine.generated(root))


def all_files(root: Path) -> list[str]:
    """Every tracked or untracked, not ignored file of ``root``."""
    return sorted(
        set(
            _git(root, "ls-files", "--cached", "--others", "--exclude-standard")
        )
    )


def _extensions(
    spec: dict[str, Any], standards: engine.Standards
) -> tuple[str, ...]:
    if "extensions" in spec:
        return tuple(spec["extensions"])
    language = standards.get(str(spec.get("language", "")))
    return language.extensions if language else ()


def _selected(
    spec: dict[str, Any], paths: Iterable[str], standards: engine.Standards
) -> list[str]:
    """The files of ``paths`` the linter ``spec`` checks."""
    extensions = _extensions(spec, standards)
    general = spec.get("language") == engine.GENERAL and not extensions
    return [
        p
        for p in paths
        if (general or p.endswith(extensions))
        and (not spec["include"] or re.search(spec["include"], p))
        and not any(re.search(r, p) for r in spec["exclude"])
    ]


def _command(root: Path, spec: dict[str, Any], key: str) -> list[str]:
    """The linter's command ``key`` (``command`` or ``fix``) as arguments."""
    values = {"python": sys.executable, "rules": str(config.RULES)}
    line = shlex.split(str(spec[key]).format_map(values))
    found = spec.get("config")
    if found:
        own = root / found["file"]
        line += [
            found["flag"],
            str(own if own.is_file() else config.RULES / found["file"]),
        ]
    return line + [str(a) for a in spec["arguments"]]


def _relative(root: Path, path: str) -> str:
    """``path`` as a linter printed it, relative to ``root``, or ""."""
    absolute = Path(path) if Path(path).is_absolute() else root / path
    try:
        return absolute.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return ""


type Raw = tuple[str, int, str, str]  # path, line, rule, message


def _ruff(text: str) -> list[Raw]:
    items = cast(list[dict[str, Any]], json.loads(text or "[]"))
    return [
        (
            i["filename"],
            int(i["location"]["row"]),
            i["code"] or "syntax",
            i["message"],
        )
        for i in items
    ]


def _eslint(text: str) -> list[Raw]:
    files = cast(list[dict[str, Any]], json.loads(text or "[]"))
    return [
        (
            f["filePath"],
            int(m.get("line", 1)),
            m.get("ruleId") or "parse",
            m["message"],
        )
        for f in files
        for m in cast(list[dict[str, Any]], f["messages"])
    ]


def _vale(text: str) -> list[Raw]:
    files = cast(dict[str, list[dict[str, Any]]], json.loads(text or "{}"))
    return [
        (path, int(a["Line"]), a["Check"], a["Message"])
        for path, alerts in files.items()
        for a in alerts
    ]


def _gitleaks(text: str) -> list[Raw]:
    leaks = cast(list[dict[str, Any]], json.loads(text or "[]"))
    return [
        (s["File"], int(s["StartLine"]), s["RuleID"], s["Description"])
        for s in leaks
    ]


PARSERS: Final[dict[str, Callable[[str], list[Raw]]]] = {
    "json-ruff": _ruff,
    "json-eslint": _eslint,
    "json-vale": _vale,
    "json-gitleaks": _gitleaks,
}


def parse(spec: dict[str, Any], text: str) -> list[Raw]:
    """The findings in a linter's output, by its ``output`` kind."""
    kind = str(spec["output"])
    if not kind.startswith("regex:"):
        return PARSERS[kind](text)
    return [
        (
            m["path"],
            int(m.groupdict().get("line") or 1),
            m.groupdict().get("rule") or str(spec.get("rule", "")),
            m.groupdict().get("message") or str(spec.get("message", m[0])),
        )
        for m in re.finditer(kind.removeprefix("regex:"), text, re.M)
    ]


def _run(
    root: Path, name: str, spec: dict[str, Any], paths: list[str]
) -> Result:
    """One run of linter ``name`` on ``paths``."""
    try:
        result = subprocess.run(
            _command(root, spec, "command") + paths,
            cwd=root,
            capture_output=True,
            text=True,
            env={**os.environ, "PATH": search_path()},
        )
    except FileNotFoundError as missing:
        return Result(
            problems=[
                f"{name}: {missing.filename} is not installed; run `verdog-standards install`"
            ]
        )
    if result.returncode not in tuple(spec.get("ok", DEFAULT_OK)):
        tail = (result.stderr or result.stdout)[-1500:]
        return Result(
            problems=[f"{name} failed (exit {result.returncode}):\n{tail}"]
        )
    try:
        raw = parse(spec, result.stdout)
    except (ValueError, KeyError) as error:
        return Result(
            problems=[
                f"{name}: unreadable output ({error}):\n{result.stdout[-1500:]}"
            ]
        )
    wanted = set(paths)
    found = [
        Finding(path, line, name, rule, message.strip())
        for path, line, rule, message in (
            (_relative(root, p), n, r, m) for p, n, r, m in raw
        )
        if path in wanted
    ]
    return Result(findings=found)


def _jobs(spec: dict[str, Any], paths: list[str]) -> list[list[str]]:
    size = 1 if spec.get("per_file") else CHUNK
    return [paths[i : i + size] for i in range(0, len(paths), size)]


def _fix(
    root: Path, linters: dict[str, dict[str, Any]], work: dict[str, list[str]]
) -> None:
    """Run each linter's ``fix`` in order; they change the files."""
    for name, paths in work.items():
        spec = linters[name]
        if spec.get("fix"):
            for chunk in _jobs(spec, paths):
                subprocess.run(
                    _command(root, spec, "fix") + chunk,
                    cwd=root,
                    capture_output=True,
                    env={**os.environ, "PATH": search_path()},
                )


def lint(root: Path, paths: Sequence[str], *, fix: bool = False) -> Result:
    """The findings of the linters on ``paths`` of ``root``.

    ``paths`` are relative to ``root``; deleted, generated and guide files
    are skipped. A linter whose ``requires`` file is missing is a problem.
    """
    present = sorted(engine.authored(root, paths))
    linters = config.linters(config.load(root))
    standards = engine.load()
    result = Result()
    work: dict[str, list[str]] = {}
    for name, spec in linters.items():
        selected = _selected(spec, present, standards)
        if not selected:
            continue
        if spec.get("requires") and not (root / spec["requires"]).exists():
            result.problems.append(
                f"{name} needs {spec['requires']}: {spec.get('missing', 'missing')}"
            )
            continue
        work[name] = selected
    if fix:
        _fix(root, linters, work)
    jobs = [
        (name, chunk)
        for name, paths in work.items()
        for chunk in _jobs(linters[name], paths)
    ]
    with ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as pool:
        names = [name for name, _ in jobs]
        chunks = [chunk for _, chunk in jobs]
        specs = [linters[name] for name in names]
        for part in pool.map(_run, [root] * len(jobs), names, specs, chunks):
            result.findings += part.findings
            result.problems += part.problems
    result.findings = sorted(set(result.findings))
    return result


def _rules_of(
    finding: Finding,
    spec: dict[str, Any],
    root: Path,
    guidelines: Sequence[engine.Guideline],
) -> list[str]:
    """The index lines of the guidelines ``finding``'s rule maps to."""
    aliases = spec.get("aliases", [finding.linter])
    mapped = [
        g
        for g in guidelines
        if any(
            engine.matches(e, alias, finding.rule)
            for e in g.checked_by
            for alias in aliases
        )
    ]
    if not mapped:
        what = f"{aliases[0]} {finding.rule}".strip()
        return [
            f"- no guideline maps {what}; see {spec.get('link', finding.linter)}"
        ]
    return [engine.rule_line(g, root) for g in mapped]


def repair(
    root: Path, path: str, found: Sequence[Finding], limit: int = SHOWN_FINDINGS
) -> str:
    """A file's repair message: its findings, each with its guidelines."""
    specs = cast(
        dict[str, dict[str, Any]], config.load(root).get("LINTERS", {})
    )
    guidelines = [g for lang in engine.load().values() for g in lang.guidelines]
    lines = [
        f"{path} has {len(found)} findings; fix each one and follow the "
        "guideline it breaks:"
    ]
    for f in found[:limit]:
        lines.append(f"  {f.path}:{f.line}: {f.linter}:{f.rule} {f.message}")
        lines += [
            f"    {line}"
            for line in _rules_of(f, specs.get(f.linter, {}), root, guidelines)
        ]
    if len(found) > limit:
        lines.append(f"  ... and {len(found) - limit} more")
    return "\n".join(lines)


def report(
    root: Path, result: Result, limit: int = SHOWN_FINDINGS
) -> list[str]:
    """The problems, then one repair message per file with findings."""
    by_file: dict[str, list[Finding]] = {}
    for f in result.findings:
        by_file.setdefault(f.path, []).append(f)
    return result.problems + [
        repair(root, path, found, limit) for path, found in by_file.items()
    ]


def lint_errors(root: Path, base: str) -> list[str]:
    """The repair messages of the files changed since ``base``; [] if clean."""
    return report(root, lint(root, changed_files(root, base)))


def install(versions: dict[str, dict[str, str]]) -> list[str]:
    """Download the pinned tools into ``BIN``, verified; return what was done."""
    done: list[str] = []
    folder = BIN.expanduser()
    folder.mkdir(parents=True, exist_ok=True)
    for name, pin in versions.items():
        stamp = folder / f".{name}-{pin['version']}"
        if stamp.is_file() and (folder / name).is_file():
            done.append(f"{name} {pin['version']}: installed")
            continue
        with urllib.request.urlopen(pin["url"], timeout=120) as response:
            data = cast(bytes, response.read())
        digest = hashlib.sha256(data).hexdigest()
        if digest != pin["sha256"]:
            raise ValueError(f"{name}: sha256 {digest} != {pin['sha256']}")
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            member = archive.extractfile(name)
            if member is None:
                raise ValueError(f"{name}: not in {pin['url']}")
            (folder / name).write_bytes(member.read())
        (folder / name).chmod(0o755)
        stamp.touch()
        done.append(f"{name} {pin['version']}: downloaded")
    for tool in ("ruff", "codespell", "doc8", "clang-tidy"):
        where = shutil.which(tool, path=search_path())
        done.append(f"{tool}: {where or 'missing; reinstall verdog-standards'}")
    return done
