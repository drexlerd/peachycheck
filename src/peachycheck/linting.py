"""``peachycheck lint``: the linters of ``peachycheck.yml`` on a repository.

The files are the given ones, those changed since a base, or all; the
files ``GENERATED`` lists are never checked. Each linter runs on the files
of its extensions (every file without any), and every finding fails. A
finding names its linter, its rule and the rule's documentation.
"""

import hashlib
import io
import itertools
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, cast

import yaml

from peachycheck import config

BIN: Final = Path("~/.cache/peachycheck/bin")
CACHE: Final = Path("~/.cache/peachycheck/results")
SHOWN_FINDINGS: Final = 20  # of a file, in a repair message
CHUNK: Final = 200  # at most this many files per run of a linter
WORKERS: Final = os.cpu_count() or 4
DEFAULT_OK: Final = (0, 1)


@dataclass(frozen=True, slots=True, order=True)
class Finding:
    """One finding: where, which linter and rule, what it says."""

    path: str
    line: int
    linter: str
    rule: str
    message: str
    link: str = ""  # the rule's documentation


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
    return sorted(set(changed + untracked) - generated(root))


def all_files(root: Path) -> list[str]:
    """Every tracked or untracked, not ignored file of ``root``."""
    return sorted(
        set(
            _git(root, "ls-files", "--cached", "--others", "--exclude-standard")
        )
    )


def _selected(spec: dict[str, Any], paths: Iterable[str]) -> list[str]:
    """The files of ``paths`` the linter ``spec`` checks."""
    extensions = tuple(spec.get("extensions") or ())
    return [
        p
        for p in paths
        if (not extensions or p.endswith(extensions))
        and (not spec["include"] or re.search(spec["include"], p))
        and not any(re.search(r, p) for r in spec["exclude"])
    ]


def generated(root: Path) -> set[str]:
    """The generated files of ``root``, as its ``GENERATED`` lists them.

    ``GENERATED`` names a JSON ``file`` of the repository, the ``list`` of
    entries in it, the key of each entry's ``path`` and, in ``where``, the
    values an entry must have; none by default.
    """
    spec = cast(dict[str, Any], config.load(root).get("GENERATED") or {})
    manifest = root / str(spec.get("file", ""))
    if not spec or not manifest.is_file():
        return set()
    entries = cast(
        list[dict[str, Any]],
        json.loads(manifest.read_text()).get(spec["list"], []),
    )
    where = cast(dict[str, Any], spec.get("where", {}))
    return {
        str(e[spec.get("path", "path")])
        for e in entries
        if all(e.get(k) == v for k, v in where.items())
    }


def _config(root: Path, wanted: dict[str, Any]) -> Path | None:
    """The configuration file a linter gets in ``root``, if any.

    With ``merge: yaml``, the repository's own file is laid over the base
    in ``.peachycheck/``: a written copy holds both. Without a base, none:
    clang-tidy finds the repository's file itself, and the files in
    subfolders that adjust it (a given file would hide those).
    """
    chosen = config.rule_file(root, wanted["file"])
    base = root / config.BASE / wanted["file"]
    own = root / wanted["file"]
    if wanted.get("merge") == "yaml" and own.is_file():
        return merged_yaml(base, own) if base.is_file() else None
    return chosen


def merged_yaml(base: Path, own: Path) -> Path:
    """``own`` over ``base``, as clang-tidy reads them, in a temporary file.

    ``Checks`` lists join (later globs win), ``CheckOptions`` merge by key,
    and any other key of ``own`` replaces the base's.
    """
    found = [
        cast(dict[str, Any], yaml.safe_load(p.read_text()) or {})
        for p in (base, own)
    ]
    result = {**found[0], **found[1]}
    checks = [str(f["Checks"]) for f in found if f.get("Checks")]
    if checks:
        result["Checks"] = ",".join(checks)
    options: dict[str, Any] = {}
    for f in found:
        given: Any = f.get("CheckOptions") or {}
        if isinstance(given, list):  # the older form: [{key:, value:}]
            pairs = cast(list[dict[str, Any]], given)
            given = {o["key"]: o["value"] for o in pairs}
        options.update(cast(dict[str, Any], given))
    if options:
        result["CheckOptions"] = options
    text = yaml.safe_dump(result, sort_keys=False)
    digest = hashlib.sha256(text.encode()).hexdigest()[:16]
    path = Path(tempfile.gettempdir()) / "peachycheck" / f"{digest}-{own.name}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _candidates(spec: dict[str, Any]) -> list[str]:
    """The linter's ``requires``: one path or a list of them."""
    wanted: Any = spec.get("requires") or []
    return [wanted] if isinstance(wanted, str) else list(map(str, wanted))


def required(root: Path, spec: dict[str, Any]) -> Path | None:
    """The first of the linter's ``requires`` files that ``root`` has.

    Without ``requires``, ``root`` itself.
    """
    candidates = _candidates(spec)
    if not candidates:
        return root
    return next((root / c for c in candidates if (root / c).exists()), None)


def _command(root: Path, spec: dict[str, Any], key: str) -> list[str]:
    """The linter's command ``key`` (``command`` or ``fix``) as arguments.

    ``{required}`` is the file ``required`` found, ``{requires}`` its folder.
    """
    there = required(root, spec) or root
    values = {
        "python": sys.executable,
        "required": str(there),
        "requires": str(there if there == root else there.parent),
    }
    line = shlex.split(str(spec[key]).format_map(values))
    wanted = spec.get("config")
    chosen = _config(root, wanted) if wanted else None
    if wanted and chosen:
        line += [wanted["flag"], str(chosen)]
    return line + [str(a) for a in spec["arguments"]]


def _relative(root: Path, path: str) -> str:
    """``path`` as a linter printed it, relative to ``root``, or ""."""
    absolute = Path(path) if Path(path).is_absolute() else root / path
    try:
        return absolute.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return ""


type Raw = tuple[str, int, str, str, str]  # path, line, rule, message, link


def _ruff(text: str) -> list[Raw]:
    items = cast(list[dict[str, Any]], json.loads(text or "[]"))
    return [
        (
            i["filename"],
            int(i["location"]["row"]),
            i["code"] or "syntax",
            i["message"],
            i.get("url") or "",
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
            "",
        )
        for f in files
        for m in cast(list[dict[str, Any]], f["messages"])
    ]


def _vale(text: str) -> list[Raw]:
    files = cast(dict[str, list[dict[str, Any]]], json.loads(text or "{}"))
    return [
        (path, int(a["Line"]), a["Check"], a["Message"], "")
        for path, alerts in files.items()
        for a in alerts
    ]


def _j2lint(text: str) -> list[Raw]:
    found = cast(dict[str, list[dict[str, Any]]], json.loads(text or "{}"))
    return [
        (e["filename"], int(e["line_number"]), e["id"], e["message"], "")
        for kind in ("ERRORS", "WARNINGS")
        for e in found.get(kind, [])
    ]


def _gitleaks(text: str) -> list[Raw]:
    leaks = cast(list[dict[str, Any]], json.loads(text or "[]"))
    return [
        (s["File"], int(s["StartLine"]), s["RuleID"], s["Description"], "")
        for s in leaks
    ]


def _pyright(text: str) -> list[Raw]:
    found = cast(dict[str, Any], json.loads(text or "{}"))
    return [
        (
            d["file"],
            int(d["range"]["start"]["line"]) + 1,
            d.get("rule") or "pyright",
            d["message"],
            "",
        )
        for d in cast(list[dict[str, Any]], found.get("generalDiagnostics", []))
        if d["severity"] in ("error", "warning")
    ]


PARSERS: Final[dict[str, Callable[[str], list[Raw]]]] = {
    "json-ruff": _ruff,
    "json-eslint": _eslint,
    "json-vale": _vale,
    "json-gitleaks": _gitleaks,
    "json-j2lint": _j2lint,
    "json-pyright": _pyright,
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
            "",
        )
        for m in re.finditer(kind.removeprefix("regex:"), text, re.M)
    ]


def _run(
    root: Path, name: str, spec: dict[str, Any], paths: list[str]
) -> Result:
    """One run of linter ``name`` on ``paths``: all its findings.

    They may lie in other files than ``paths``: clang-tidy reports a
    header's findings while checking a source that includes it.
    """
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
                f"{name}: {missing.filename} is not installed; run `peachycheck install`"
            ]
        )
    if result.returncode not in tuple(spec.get("ok", DEFAULT_OK)):
        tail = (result.stderr or result.stdout)[-1500:]
        return Result(
            problems=[f"{name} failed (exit {result.returncode}):\n{tail}"]
        )
    text = result.stderr if spec.get("stream") == "stderr" else result.stdout
    try:
        raw = parse(spec, text)
    except (ValueError, KeyError) as error:
        return Result(
            problems=[f"{name}: unreadable output ({error}):\n{text[-1500:]}"]
        )
    found = [
        Finding(path, line, name, rule, message.strip(), u or link(spec, rule))
        for path, line, rule, message, u in (
            (_relative(root, p), n, r, m, u) for p, n, r, m, u in raw
        )
        if path
    ]
    return Result(findings=found)


INCLUDE: Final = re.compile(r'^\s*#\s*include\s*[<"]([^>"]+)[>"]', re.M)
FOLDER_FLAGS: Final = ("-isystem", "-iquote", "-I")


def _include_folders(arguments: list[str], folder: Path) -> list[Path]:
    """The include folders of a compile command run in ``folder``."""
    found: list[Path] = []
    for flag, value in itertools.pairwise([*arguments, ""]):
        prefix = next((f for f in FOLDER_FLAGS if flag.startswith(f)), "")
        if prefix:
            found.append(folder / (flag.removeprefix(prefix) or value))
    return found


class _Cache:
    """Results of a ``cache`` linter's runs, one file per run, by inputs.

    The key covers the file, the repository files it includes (scanning
    ``#include`` against the compile commands' include folders), the files
    ``cache.inputs`` names (such as ``.clang-tidy``) from the file's folder
    up, its compile command, the linter's settings and the tool's version.
    Headers outside the repository are not scanned: after rebuilding
    dependencies, clear the cache.
    """

    def __init__(self, root: Path, spec: dict[str, Any]) -> None:
        self.root = root.resolve()
        self.spec = spec
        self.commands: dict[str, str] = {}
        folders: list[Path] = []
        database = required(root, spec)
        self.database = ""
        if database and database.suffix == ".json" and database.is_file():
            text = database.read_text()
            self.database = hashlib.sha256(text.encode()).hexdigest()
            for entry in cast(list[dict[str, Any]], json.loads(text)):
                arguments = entry.get("arguments") or shlex.split(
                    entry["command"]
                )
                folder = Path(entry.get("directory", "."))
                file = _relative(root, str(folder / entry["file"]))
                self.commands[file] = shlex.join(map(str, arguments))
                folders += _include_folders(arguments, folder)
        self.folders = [
            f
            for f in dict.fromkeys(f.resolve() for f in folders)
            if f.is_relative_to(self.root)
        ]
        tool = shlex.split(str(spec["command"]))[0]
        try:  # the version, not the binary: CI installs a fresh copy each run
            self.tool = subprocess.run(
                [tool, "--version"],
                capture_output=True,
                text=True,
                env={**os.environ, "PATH": search_path()},
            ).stdout
        except OSError:
            self.tool = tool
        self.included: dict[Path, list[Path]] = {}

    def _includes(self, path: Path) -> list[Path]:
        """The repository files ``path`` includes directly."""
        if path not in self.included:
            found: list[Path] = []
            for name in INCLUDE.findall(path.read_text(errors="replace")):
                for folder in (path.parent, *self.folders):
                    candidate = (folder / name).resolve()
                    if candidate.is_file() and candidate.is_relative_to(
                        self.root
                    ):
                        found.append(candidate)
                        break
            self.included[path] = found
        return self.included[path]

    def key(self, path: str) -> str:
        """The key of ``path``'s results: a digest of everything they depend on."""
        digest = hashlib.sha256()
        digest.update(
            json.dumps(self.spec, sort_keys=True, default=str).encode()
        )
        digest.update(self.tool.encode())
        digest.update(self.commands.get(path, self.database).encode())
        start = (self.root / path).resolve()
        seen: set[Path] = set()
        pending = [start]
        while pending:
            current = pending.pop()
            if current not in seen:
                seen.add(current)
                if self.spec["cache"].get("includes"):
                    pending += self._includes(current)
        for file in sorted(seen):
            digest.update(str(file.relative_to(self.root)).encode() + b"\0")
            digest.update(file.read_bytes())
        for folder in (start.parent, *start.parent.parents):
            if not folder.is_relative_to(self.root):
                break
            for name in map(str, self.spec["cache"].get("inputs", [])):
                if (folder / name).is_file():
                    digest.update(name.encode() + b"\0")
                    digest.update((folder / name).read_bytes())
        return digest.hexdigest()

    @staticmethod
    def _file(key: str) -> Path:
        return CACHE.expanduser() / key[:2] / f"{key}.json"

    @classmethod
    def load(cls, key: str) -> list[Finding] | None:
        """The kept findings of ``key``, if any."""
        try:
            rows = cast(list[list[Any]], json.loads(cls._file(key).read_text()))
        except (OSError, ValueError):
            return None
        return [
            Finding(str(p), int(n), str(lt), str(r), str(m), str(u))
            for p, n, lt, r, m, u in rows
        ]

    @classmethod
    def store(cls, key: str, findings: list[Finding]) -> None:
        """Keep ``findings`` under ``key``."""
        file = cls._file(key)
        file.parent.mkdir(parents=True, exist_ok=True)
        rows = [
            [f.path, f.line, f.linter, f.rule, f.message, f.link]
            for f in findings
        ]
        file.write_text(json.dumps(rows))


def _jobs(spec: dict[str, Any], paths: list[str]) -> list[list[str]]:
    """``paths`` in runs of a linter, spread over the workers.

    A linter like clang-tidy works through its files one after another, so
    one run of all of them would use a single core. A linter with
    ``one_run``, like pyright, analyzes the imports its files share once
    and spreads the work itself.
    """
    if spec.get("one_run"):
        return [paths]
    spread = -(-len(paths) // WORKERS)
    size = 1 if spec.get("per_file") else max(1, min(CHUNK, spread))
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


def lint(
    root: Path, paths: Sequence[str], *, fix: bool = False, edit: bool = False
) -> Result:
    """The findings of the linters on ``paths`` of ``root``.

    ``paths`` are relative to ``root``; deleted and generated files
    are skipped. A linter that finds none of its ``requires`` files is a
    problem. With ``edit`` (the hook after an edit), linters with
    ``stage: stop`` wait for the stop hook.
    """
    present = sorted(
        {p for p in paths if (root / p).is_file()} - generated(root)
    )
    linters = config.linters(config.load(root))
    result = Result()
    work: dict[str, list[str]] = {}
    for name, spec in linters.items():
        if edit and spec.get("stage") == "stop":
            continue
        selected = _selected(spec, present)
        if not selected:
            continue
        wanted = spec.get("config")
        if wanted and wanted.get("required") and not _config(root, wanted):
            continue  # ponytail: the tool cannot run without a configuration
        if required(root, spec) is None:
            result.problems.append(
                f"{name} needs {' or '.join(_candidates(spec))}: "
                f"{spec.get('missing', 'missing')}"
            )
            continue
        work[name] = selected
    if fix:
        _fix(root, linters, work)
    jobs: list[tuple[str, list[str], str]] = []  # linter, files, cache key
    for name, paths in work.items():
        spec = linters[name]
        if not spec.get("cache"):
            jobs += [(name, chunk, "") for chunk in _jobs(spec, paths)]
            continue
        cache = _Cache(root, spec)
        for path in paths:  # one file per run, so its results can be kept
            key = cache.key(path)
            kept = _Cache.load(key)
            if kept is None:
                jobs.append((name, [path], key))
            else:
                result.findings += kept

    def run(job: tuple[str, list[str], str]) -> Result:
        return _run(root, job[0], linters[job[0]], job[1])

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for (_, _, key), part in zip(jobs, pool.map(run, jobs), strict=True):
            if key and not part.problems:
                _Cache.store(key, part.findings)
            result.findings += part.findings
            result.problems += part.problems
    result.findings = sorted(
        {f for f in result.findings if f.path in work.get(f.linter, ())}
    )
    return result


def link(spec: dict[str, Any], rule: str) -> str:
    """The documentation of ``rule``: the linter's ``rule_link``, else ``link``.

    ``rule_link`` is a template, or templates by rule prefix (the longest
    that matches wins). ``{rule}`` is the rule; ``{name}`` the rule after a
    matched prefix, else after its group; ``{group}`` the rule before its
    first ``/`` or, without one, its first ``-``.
    """
    templates: Any = spec.get("rule_link") or {}
    if isinstance(templates, str):
        templates = {"": templates}
    found = cast(dict[str, str], templates)
    rule = rule.split(",")[0]  # clang-tidy names a check's aliases too
    prefix = max(
        (p for p in found if rule.startswith(p)), key=len, default=None
    )
    if prefix is None or not rule:
        return str(spec.get("link", ""))
    group, _, name = rule.partition("/" if "/" in rule else "-")
    if prefix:
        name = rule.removeprefix(prefix)
    return found[prefix].format(rule=rule, group=group, name=name)


def repair(
    path: str, found: Sequence[Finding], limit: int = SHOWN_FINDINGS
) -> str:
    """A file's repair message: its findings, each with its documentation."""
    lines = [f"{path} has {len(found)} findings; fix each one:"]
    for f in found[:limit]:
        where = f" → {f.link}" if f.link else ""
        lines.append(
            f"  {f.path}:{f.line}: {f.linter}:{f.rule} {f.message}{where}"
        )
    if len(found) > limit:
        lines.append(f"  ... and {len(found) - limit} more")
    return "\n".join(lines)


def report(result: Result, limit: int = SHOWN_FINDINGS) -> list[str]:
    """The problems, then one repair message per file with findings."""
    by_file: dict[str, list[Finding]] = {}
    for f in result.findings:
        by_file.setdefault(f.path, []).append(f)
    return result.problems + [
        repair(path, found, limit) for path, found in by_file.items()
    ]


def lint_files(
    paths: Sequence[Path], *, fix: bool = False
) -> list[tuple[Path, Result]]:
    """Lint absolute ``paths``, each with the repository it belongs to.

    A file belongs to the nearest folder above it with a peachycheck.yml (see
    ``config.owner``); files of no such repository are skipped.
    """
    return [
        (root, lint(root, found, fix=fix))
        for root, found in config.grouped(paths).items()
    ]


def lint_errors(root: Path, base: str) -> list[str]:
    """The repair messages of the files changed since ``base``; [] if clean."""
    return report(lint(root, changed_files(root, base)))


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
    for tool in (
        "ruff",
        "codespell",
        "doc8",
        "clang-tidy",
        "clang-format",
        "j2lint",
        "pyright",
    ):
        where = shutil.which(tool, path=search_path())
        done.append(f"{tool}: {where or 'missing; reinstall peachycheck'}")
    return done
