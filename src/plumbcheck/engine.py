"""The engine of the organization's standards: load, select, check, map.

The instance is the folder ``standards/`` of ``verdog-ai/standards``, which the
user selects and no loop changes; this module only interprets it. Each
language has a folder ``languages/<language>/`` with ``language.toml`` (title,
extensions, project marker, test globs), ``guidelines.toml`` (one table per
guideline) and ``tools.toml`` (its checkers). ``general/`` holds the
guidelines and tools for every repository, ``metrics.toml`` the code metrics
and ``literature.bib`` the literature guidelines and metrics may cite.

A consumer such as auto-se adds what concerns only itself through ``LAYERS``
(extra ``<kind>.toml`` and ``general/tools.toml`` roots), ``LOOPS``,
``CATEGORIES`` and ``CHECKS``; it never changes the organization's rules.

Checks count findings per file and allow no regression: a new file must have
none, and a changed file may not gain any compared with the base.
"""

import ast
import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
import urllib.request
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Final


def _data(name: str) -> Path:
    """A data folder: packaged next to this module, or at the repo's root."""
    packaged = Path(__file__).parent / name
    return (
        packaged
        if packaged.is_dir()
        else Path(__file__).resolve().parents[2] / name
    )


# The instance: everything repositories follow and are judged by.
STANDARDS: Path = Path(os.environ.get("VERDOG_STANDARDS") or _data("standards"))
TEMPLATES: Path = _data("templates")
# What a consumer adds on top, never changing the rules: roots with extra
# ``<kind>.toml`` tables and ``general/tools.toml`` tools, its loop names, the
# names of its literature categories and extra ``check`` errors.
LAYERS: list[Path] = []
LOOPS: list[str] = []
CATEGORIES: dict[str, str] = {}
CHECKS: list[Callable[[], list[str]]] = []
CACHE: Final = Path("~/.cache/verdog-standards")
LANGUAGES: Final = "languages"
GENERAL: Final = "general"
LITERATURE: Final = "literature.bib"
MAIN_BRANCH: str = ""  # empty: the remote's default branch (origin/HEAD)
GUIDELINE_ID: Final = re.compile(r"[a-z][a-z0-9-]*")
KINDS: Final = ("coding", "documentation")
ROLES: Final = ("checker", "retriever", "measurer")
# The fields each file allows, with their types.
LANGUAGE_FIELDS: Final = {
    "title": str,
    "extensions": list,
    "marker": str,
    "tests": list,
    "activates": str,
}
# A language applies to files with its extensions, or, by ``marker``, to every
# repository that has its marker file (Verdog: ``project.json``).
ACTIVATIONS: Final = ("extension", "marker")
GUIDELINE_FIELDS: Final = {
    "text": str,
    "kind": str,
    "advisory": bool,
    "link": str,
    "cite": str,
    "good": str,
    "bad": str,
    "details": str,
    "checked_by": str,
    "judged_by": str,
    "reference": str,
}
# A pinned guide's sections: Markdown and HTML headings, ``<a name|id>``
# anchors and ``{#id}`` attributes, and reStructuredText titles and labels.
MD_HEADING: Final = re.compile(r"^(#{1,6})\s+(.*?)\s*(?:\{#([\w-]+)\})?\s*$")
HTML_HEADING: Final = re.compile(r"<h([1-6])\b([^>]*)>(.*?)</h\1>")
ANCHOR: Final = re.compile(r'<a (?:name|id)="([^"]+)"')
RST_UNDERLINE: Final = re.compile(r'^([=\-~^"`+*#])\1{2,}\s*$')
RST_LABEL: Final = re.compile(r"^\.\. _([\w-]+):\s*$")
REFERENCES: Final = "references"
SECTION_CHARS: Final = 4000  # of a referenced section, shown on the map
# Where a target repository's worktree gets the pinned guides it may read.
WORKTREE_REFERENCES: str = (
    ".verdog-standards/references"  # a consumer may move it
)
TOOL_FIELDS: Final = {
    "role": str,
    "run": str,
    "install": str,
    "env": dict,
    "ok": list,
    "link": str,
    "cite": str,
    "exclude": list,
}
COMPLEXITY_LIMIT: Final = 10  # McCabe's threshold, as the gate's rule
# A finding of a checker: ``path:line[:column]: ...``, except clang's notes.
FINDING: Final = re.compile(
    r"^(?P<path>[^\s:][^:\n]*):\d+(?::\d+)?: (?!note\b)", re.M
)
GTS: Final = ("gts@5.3.1", "typescript@5.4.5")
TOOLS: Final = CACHE / "gts"
BUILD_DIRECTORIES: list[str] = ["build", "build/standards"]


class ToolMissingError(RuntimeError):
    """A check needs a tool that is not installed."""


@dataclass(frozen=True, slots=True)
class Check:
    """A checker's command and the guidelines it checks."""

    tool: str
    run: str
    install: str = ""
    env: dict[str, str] = field(default_factory=dict[str, str])
    ok: tuple[int, ...] = (0, 1)
    guidelines: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Guideline:
    """One guideline: a table of a language's ``guidelines.toml``."""

    language: str
    id: str
    text: str
    path: str = ""
    line: int = 1
    kind: str = "coding"
    advisory: bool = False
    link: str = ""
    cite: str = ""
    good: str = ""
    bad: str = ""
    details: str = ""
    checked_by: str = ""
    judged_by: str = ""
    reference: str = ""
    errors: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        """The guideline's unique name, ``<language>/<id>``."""
        return f"{self.language}/{self.id}"


@dataclass(frozen=True, slots=True)
class Tool:
    """One tool: a checker, a retriever of context or a measurer of metrics.

    A checker counts findings; measurers are declared here and run by their
    consumer's own code, such as auto-se's gate.
    """

    language: str
    name: str
    role: str = "checker"
    run: str = ""
    install: str = ""
    env: dict[str, str] = field(default_factory=dict[str, str])
    ok: tuple[int, ...] = (0, 1)
    link: str = ""
    cite: str = ""
    exclude: tuple[str, ...] = ()
    retrieval: dict[str, dict[str, Any]] = field(
        default_factory=dict[str, dict[str, Any]]
    )
    path: str = ""
    line: int = 1
    errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Language:
    """One folder of the instance: a language, or ``general``."""

    id: str
    title: str
    path: str
    extensions: tuple[str, ...] = ()
    marker: str = ""
    tests: tuple[str, ...] = ()
    activates: str = "extension"
    guidelines: tuple[Guideline, ...] = ()
    tools: tuple[Tool, ...] = ()
    errors: tuple[tuple[str, int, str], ...] = ()
    text: str = ""

    def checkers(self) -> list[Check]:
        """The language's checkers, each with the guidelines it checks."""
        return [
            Check(
                tool=tool.name,
                run=tool.run,
                install=tool.install,
                env=tool.env,
                ok=tool.ok,
                exclude=tool.exclude,
                guidelines=tuple(
                    g.key for g in self.guidelines if g.checked_by == tool.name
                ),
            )
            for tool in self.tools
            if tool.role == "checker"
        ]


@dataclass(frozen=True, slots=True)
class Activation:
    """A language active for a task: why, on which files, and its guidelines."""

    language: str
    title: str
    reasons: tuple[str, ...]
    files: tuple[str, ...]
    cite: tuple[str, ...] = ()
    checks: tuple[Check, ...] = ()
    guidelines: tuple[dict[str, Any], ...] = ()
    sha256: str = ""


type Standards = dict[str, Language]


def _read(path: Path) -> tuple[dict[str, Any], str]:
    """A TOML file's tables and text; raise ``ValueError`` with the line."""
    if not path.is_file():
        return {}, ""
    text = path.read_text()
    try:
        return tomllib.loads(text), text
    except tomllib.TOMLDecodeError as error:
        found = re.search(r"line (\d+)", str(error))
        raise ValueError(
            f"{path}:{found.group(1) if found else 1}: {error}"
        ) from error


def _line(text: str, name: str) -> int:
    """The line of the table ``[name]`` in ``text``."""
    found = re.search(rf"^\[\"?{re.escape(name)}\"?\]", text, re.M)
    return text.count("\n", 0, found.start()) + 1 if found else 1


def _field_errors(
    table: Mapping[str, Any], fields: Mapping[str, type], subject: str
) -> list[str]:
    """Unknown fields and fields of the wrong type."""
    errors = [
        f"{subject}: unknown field {k!r}" for k in table if k not in fields
    ]
    return errors + [
        f"{subject}: {k} must be a {fields[k].__name__}"
        for k, v in table.items()
        if k in fields and not isinstance(v, fields[k])
    ]


def _guideline(
    language: str, name: str, table: dict[str, Any], where: str, text: str
) -> Guideline:
    errors = _field_errors(table, GUIDELINE_FIELDS, f"{language}/{name}")
    if not GUIDELINE_ID.fullmatch(name):
        errors.append(
            f"{language}/{name}: the id must match {GUIDELINE_ID.pattern}"
        )
    if not isinstance(table.get("text"), str) or not table["text"].strip():
        errors.append(f"{language}/{name}: a guideline needs a text")
    if table.get("kind", "coding") not in KINDS:
        errors.append(f"{language}/{name}: kind must be one of {KINDS}")
    known = {
        k: v for k, v in table.items() if k in GUIDELINE_FIELDS and not errors
    }
    return Guideline(
        language=language,
        id=name,
        text=str(table.get("text", "")),
        path=where,
        line=_line(text, name),
        errors=tuple(errors),
        **{k: v for k, v in known.items() if k != "text"},
    )


def _tool(
    language: str, name: str, table: dict[str, Any], where: str, text: str
) -> Tool:
    # A retriever's sub-tables are its retrievals; other unknown fields err.
    plain = {
        k: v
        for k, v in table.items()
        if k in TOOL_FIELDS or not isinstance(v, dict)
    }
    errors = _field_errors(plain, TOOL_FIELDS, f"{language}/{name}")
    role = str(table.get("role", "checker"))
    errors += _role_errors(f"{language}/{name}", role, bool(table.get("run")))
    return Tool(
        language=language,
        name=name,
        role=role,
        run=str(table.get("run", "")),
        install=str(table.get("install", "")),
        env={str(k): str(v) for k, v in dict(table.get("env", {})).items()},
        ok=tuple(int(c) for c in table.get("ok", (0, 1))),
        link=str(table.get("link", "")),
        cite=str(table.get("cite", "")),
        exclude=tuple(str(g) for g in table.get("exclude", ())),
        retrieval={
            k: dict(v) for k, v in table.items() if k not in TOOL_FIELDS
        },
        path=where,
        line=_line(text, name),
        errors=tuple(errors),
    )


def _role_errors(key: str, role: str, runs: bool) -> list[str]:
    errors = [] if role in ROLES else [f"{key}: role must be one of {ROLES}"]
    if role == "checker" and not runs:
        errors.append(f"{key}: a checker needs a run command")
    return errors


def _header_errors(
    name: str, header: dict[str, Any], activates: str
) -> list[str]:
    """What ``language.toml`` of language ``name`` lacks or gets wrong."""
    errors = _field_errors(header, LANGUAGE_FIELDS, name)
    if not header.get("title"):
        errors.append(f"{name}: the language needs a title")
    if activates not in ACTIVATIONS:
        errors.append(f"{name}: activates must be one of {ACTIVATIONS}")
    if activates == "marker" and not header.get("marker"):
        errors.append(f"{name}: a language activated by marker needs one")
    needs_extensions = name != GENERAL and activates == "extension"
    if needs_extensions and not header.get("extensions"):
        errors.append(f"{name}: the language needs file extensions")
    return errors


def parse(folder: Path, name: str) -> Language:
    """The language in ``folder``; ``ValueError`` on a TOML syntax error."""
    header, header_text = _read(folder / "language.toml")
    found, text = _read(folder / "guidelines.toml")
    tools, tools_text = _read(folder / "tools.toml")
    where = str(folder / "language.toml")
    activates = str(header.get("activates", "extension"))
    errors = [(where, 1, e) for e in _header_errors(name, header, activates)]
    return Language(
        id=name,
        title=str(header.get("title", name)),
        path=str(folder),
        extensions=tuple(header.get("extensions", ())),
        marker=str(header.get("marker", "")),
        tests=tuple(header.get("tests", ())),
        activates=activates,
        guidelines=tuple(
            _guideline(
                name, key, dict(table), str(folder / "guidelines.toml"), text
            )
            for key, table in found.items()
        ),
        tools=tuple(
            _tool(
                name, key, dict(table), str(folder / "tools.toml"), tools_text
            )
            for key, table in tools.items()
        ),
        errors=tuple(errors),
        text=header_text + text + tools_text,
    )


def load() -> Standards:
    """Every language of the instance, and ``general``."""
    folders = sorted(p for p in (STANDARDS / LANGUAGES).glob("*") if p.is_dir())
    found = {p.name: parse(p, p.name) for p in folders}
    general = parse(STANDARDS / GENERAL, GENERAL)
    for layer in LAYERS:
        extra = parse(layer / GENERAL, GENERAL)
        general = replace(general, tools=general.tools + extra.tools)
    found[GENERAL] = general
    return found


def declarations(kind: str) -> dict[str, dict[str, Any]]:
    """The tables of ``<kind>.toml`` at the instance's root and the layers'."""
    found: dict[str, dict[str, Any]] = {}
    for root in (STANDARDS, *LAYERS):
        path = root / f"{kind}.toml"
        if path.is_file():
            found.update(tomllib.loads(path.read_text()))
    return found


def judging(loop: str) -> list[dict[str, Any]]:
    """The metrics that judge ``loop``'s work, for its prompt.

    A metric judges a loop when the gate uses it (required, objective or
    documentation), it is measured (not planned) and its ``loops`` name the
    loop or are empty, which means every loop.

    Args:
        loop: The loop being prompted.

    Returns:
        One entry per metric with its ``name``, ``role``, ``better`` and
        ``definition``, in the order of ``metrics.toml``.
    """
    return [
        {
            "name": name,
            "role": m["role"],
            "better": m["better"],
            "definition": re.sub(
                r"\\code\{([^}]*)\}", r"`\1`", m["definition"]
            ),
        }
        for name, m in declarations("metrics").items()
        if m["role"] in ("required", "objective", "documentation")
        and m.get("status", "done") == "done"
        and loop in m.get("loops", (loop,))
    ]


def languages() -> dict[str, dict[str, Any]]:
    """Extensions, marker and test globs of every language, for toolchains."""
    return {
        lang.id: {
            "extensions": list(lang.extensions),
            "marker": lang.marker,
            "tests": list(lang.tests),
        }
        for lang in load().values()
        if lang.id != GENERAL
    }


def retrieval() -> dict[str, dict[str, Any]]:
    """What the retrievers of ``general`` fetch: ``code_index`` and corpora."""
    return {
        name: settings
        for tool in load()[GENERAL].tools
        if tool.role == "retriever"
        for name, settings in tool.retrieval.items()
    }


def language_of(standards: Standards, path: str) -> str:
    """The language whose extension ``path`` has, or ""."""
    return next(
        (
            lang.id
            for lang in standards.values()
            if lang.extensions and path.endswith(lang.extensions)
        ),
        "",
    )


def select(
    standards: Standards, changed: Iterable[str], root: Path, base: str = ""
) -> list[Activation]:
    """The languages of ``changed`` files, after ``general`` (always).

    ``root`` and ``base`` locate the files; a language applies by extension.
    """
    reasons: dict[str, list[str]] = {GENERAL: ["always"]}
    # The general checkers (secrets) look at every changed file.
    active: dict[str, set[str]] = {GENERAL: set(changed)}
    for path in sorted(set(changed)):
        language = language_of(standards, path)
        if language:
            suffix = next(
                e for e in standards[language].extensions if path.endswith(e)
            )
            reasons.setdefault(language, []).append(
                f"{path} ends with {suffix}"
            )
            active.setdefault(language, set()).add(path)
    for key in _marked(standards, root):
        reasons.setdefault(key, []).append(
            f"the repository has {standards[key].marker}"
        )
        active.setdefault(key, set())
    order = [GENERAL, *sorted(set(active) - {GENERAL})]
    return [
        _activation(standards[key], reasons[key], active[key], root)
        for key in order
        if key in standards
    ]


def sections(path: Path) -> dict[str, str]:
    """The sections of a pinned guide by anchor.

    A section runs to the next heading of the same or a higher level; a
    heading also answers to its title's slug.
    """
    lines = path.read_text().splitlines()
    heads: list[tuple[int, int, set[str]]] = []
    pending: list[str] = []
    order: list[str] = []
    rst = path.suffix == ".rst"
    for i, line in enumerate(lines):
        pending += ANCHOR.findall(line)
        if rst and (label := RST_LABEL.match(line)):
            pending.append(label[1])
            continue
        start, level, title, names = (
            _rst_heading(lines, i, order) if rst else _heading(line, i)
        )
        if level:
            heads.append((start, level, {*pending, *names, _slug(title)}))
            pending = []
    found: dict[str, str] = {}
    for n, (start, level, names) in enumerate(heads):
        end = next(
            (s for s, deeper, _ in heads[n + 1 :] if deeper <= level), None
        )
        for name in names:
            found.setdefault(name, "\n".join(lines[start:end]))
    return found


Heading = tuple[int, int, str, list[str]]  # start line, level, title, anchors


def _heading(line: str, i: int) -> Heading:
    """A Markdown or HTML heading at line ``i``; level 0 if none."""
    if md := MD_HEADING.match(line):
        return i, len(md[1]), md[2], [md[3]] if md[3] else []
    if html := HTML_HEADING.search(line):
        return i, int(html[1]), html[3], re.findall(r'id="([^"]+)"', html[2])
    return i, 0, "", []


def _rst_heading(lines: list[str], i: int, order: list[str]) -> Heading:
    """A reST heading underlined at line ``i``; ``order`` ranks underlines."""
    line = lines[i]
    above = lines[i - 1].strip() if i else ""
    if not (RST_UNDERLINE.match(line) and above) or above.startswith(".."):
        return i, 0, "", []
    if RST_UNDERLINE.match(above):
        return i, 0, "", []
    if line[0] not in order:
        order.append(line[0])
    return i - 1, order.index(line[0]) + 1, above, []


def _slug(title: str) -> str:
    plain = re.sub(r"<[^>]+>|\{#[^}]*\}|`", "", title).lower()
    return re.sub(r"[^a-z0-9]+", "-", plain).strip("-")


def guide(language: Language, file: str) -> Path | None:
    """A pinned guide of ``language``: committed, or fetched into the cache.

    A guide whose license forbids a public copy is named in
    ``references/downloads.toml`` with its URL and sha256 and is fetched once.
    """
    path = Path(language.path) / file
    if path.is_file():
        return path
    folder = Path(language.path) / REFERENCES
    pinned, _ = _read(folder / "downloads.toml")
    name = (
        Path(file).relative_to(REFERENCES).as_posix()
        if file.startswith(REFERENCES)
        else file
    )
    entry = pinned.get(name)
    if entry is None:
        return None
    cached = CACHE.expanduser() / language.id / name
    if not cached.is_file():
        with urllib.request.urlopen(str(entry["url"]), timeout=60) as response:
            data = response.read()
        if hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise ValueError(f"{file}: download does not match its sha256")
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(data)
    return cached


def section(language: Language, reference: str) -> str | None:
    """The text of ``reference`` (``<file>#<anchor>``) of ``language``."""
    file, _, anchor = reference.partition("#")
    try:
        path = guide(language, file)
    except OSError:
        return (
            None  # ponytail: offline and not cached yet; the map shows no text
        )
    return sections(path).get(anchor) if path is not None else None


def pointer(language: Language, guideline: Guideline, root: Path) -> str:
    """Where the agent working in ``root`` reads ``guideline``'s rule, or "".

    The standards repository reads its own ``standards/``; another repository
    reads the copies ``copy_references`` put into its worktree.
    """
    if not guideline.reference:
        return ""
    file, _, anchor = guideline.reference.partition("#")
    folder = Path(language.path).relative_to(STANDARDS.parent)
    where = (
        folder / file
        if (root / folder).is_dir()
        else Path(
            WORKTREE_REFERENCES, language.id, Path(file).relative_to(REFERENCES)
        )
    )
    text = section(language, guideline.reference) or ""
    title = re.sub(r"<[^>]+>|^#+\s*", "", text.splitlines()[0] if text else "")
    rule = title.split(":")[0].strip() if ":" in title[:12] else ""
    return f"{rule} in {where}#{anchor}" if rule else f"{where}#{anchor}"


def copy_references(root: Path) -> None:
    """Copy the pinned guides into ``root``'s ignored ``WORKTREE_REFERENCES``.

    An agent in another repository then reads them offline, downloads included.
    """
    for language in load().values():
        source = Path(language.path) / REFERENCES
        if source.is_dir():
            target = root / WORKTREE_REFERENCES / language.id
            shutil.copytree(source, target, dirs_exist_ok=True)
            for g in language.guidelines:
                found = (
                    guide(language, g.reference.partition("#")[0])
                    if g.reference
                    else None
                )
                if found is not None and not found.is_relative_to(source):
                    shutil.copy2(found, target / found.name)


def _marked(standards: Standards, root: Path) -> list[str]:
    """The languages activated by a marker file that ``root`` has."""
    return [
        key
        for key, language in standards.items()
        if language.activates == "marker" and (root / language.marker).is_file()
    ]


def _activation(
    language: Language, reasons: list[str], changed: set[str], root: Path
) -> Activation:
    return Activation(
        language=language.id,
        title=language.title,
        reasons=tuple(reasons),
        files=tuple(sorted(changed)),
        cite=tuple(sorted({g.cite for g in language.guidelines if g.cite})),
        checks=tuple(language.checkers()),
        guidelines=tuple(
            {
                **asdict(g),
                "key": g.key,
                "example": _example(language, g),
                "pointer": pointer(language, g, root),
            }
            for g in language.guidelines
        ),
        sha256=hashlib.sha256(language.text.encode()).hexdigest(),
    )


def _example(language: Language, guideline: Guideline) -> str:
    """The good example the prompt shows with ``guideline``, or "".

    Only a guideline no guide covers and no checker enforces shows one (the
    Verdog JSON); checked examples are checker tests, guides carry their own.
    """
    if guideline.checked_by or guideline.reference:
        return ""
    path = Path(language.path) / guideline.good
    return path.read_text().strip() if guideline.good and path.is_file() else ""


def for_loop(
    activations: Sequence[Activation], loop: str
) -> list[dict[str, Any]]:
    """The guidelines ``loop`` sees: per language, coding then documentation.

    Every loop working on a language's files sees all its guidelines.
    """
    del loop
    groups = [
        {
            "language": a.language,
            "title": a.title,
            "reasons": list(a.reasons),
            "cite": list(a.cite),
            **{
                kind: [g for g in a.guidelines if g["kind"] == kind]
                for kind in KINDS
            },
        }
        for a in activations
    ]
    return [g for g in groups if any(g[kind] for kind in KINDS)]


def dump(activations: Sequence[Activation]) -> str:
    """Activations as JSON, for the dossier and the task's audit record."""
    return json.dumps([asdict(activation) for activation in activations])


def undump(record: str) -> list[Activation]:
    """The activations of ``dump``'s JSON."""
    items: list[dict[str, Any]] = json.loads(record)
    return [
        Activation(
            language=str(item["language"]),
            title=str(item["title"]),
            reasons=tuple(item["reasons"]),
            files=tuple(item["files"]),
            cite=tuple(item["cite"]),
            checks=tuple(
                Check(
                    tool=str(c["tool"]),
                    run=str(c["run"]),
                    install=str(c["install"]),
                    env=dict(c["env"]),
                    ok=tuple(c["ok"]),
                    guidelines=tuple(c["guidelines"]),
                    exclude=tuple(c.get("exclude", ())),
                )
                for c in item["checks"]
            ),
            guidelines=tuple(dict(g) for g in item["guidelines"]),
            sha256=str(item["sha256"]),
        )
        for item in items
    ]


def repository_titles(standards: Standards, root: Path) -> list[str]:
    """Titles of the languages with a tracked file in ``root``."""
    listed = subprocess.run(
        ["git", "-C", str(root), "ls-files"], capture_output=True, text=True
    )
    tracked = listed.stdout.split() if not listed.returncode else []
    found = {language_of(standards, path) for path in tracked} - {""}
    found |= set(_marked(standards, root))
    return [standards[key].title for key in sorted(found)]


def _gts() -> Path:
    """Google's ESLint configuration, installed once into the cache."""
    cache = TOOLS.expanduser()
    if not (cache / "node_modules" / ".bin" / "eslint").is_file():
        if shutil.which("npm") is None:
            raise ToolMissingError(
                "Google TypeScript style needs npm to install gts."
            )
        cache.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["npm", "init", "-y"], cwd=cache, capture_output=True, check=True
        )
        subprocess.run(
            ["npm", "install", "--silent", *GTS],
            cwd=cache,
            capture_output=True,
            check=True,
        )
    return cache / "node_modules"


def _compile_commands(build: Path) -> str:
    for directory in BUILD_DIRECTORIES:
        if (build / directory / "compile_commands.json").is_file():
            return str(build / directory)
    raise ToolMissingError(
        "C++ checks need compile_commands.json from the CMake setup."
    )


def command(
    check: Check, title: str, paths: Sequence[str], build: Path
) -> list[str]:
    """``check.run`` with its placeholders expanded, as an argument list."""
    tool = shlex.split(check.run)[0]
    if "{" not in tool and shutil.which(tool, path=_search_path()) is None:
        hint = f"; install it with {check.install}" if check.install else ""
        raise ToolMissingError(f"{title} needs {tool}{hint}.")
    values: dict[str, str] = {
        "files": shlex.join(paths),
        "python": sys.executable,
        "standards": str(STANDARDS),
    }
    if "{compile_commands}" in check.run:
        values["compile_commands"] = _compile_commands(build)
    if any(f"{{{name}}}" in check.run for name in ("gts", "eslint", "tsc")):
        modules = _gts()
        values["gts"] = str(modules / "gts")
        values["eslint"] = str(modules / ".bin" / "eslint")
        values["tsc"] = str(modules / ".bin" / "tsc")
    return shlex.split(check.run.format_map(values))


def _search_path() -> str:
    """``PATH`` with this Python's environment, holding its tools, first."""
    return os.pathsep.join(
        (str(Path(sys.executable).parent), os.environ["PATH"])
    )


def count(
    check: Check, title: str, root: Path, paths: Sequence[str], build: Path
) -> Counter[str]:
    """Findings of ``check`` per file of ``paths`` in ``root``."""
    present = [path for path in paths if (root / path).is_file()]
    if not present:
        return Counter()
    result = subprocess.run(
        command(check, title, present, build),
        cwd=root,
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": _search_path(), **check.env},
    )
    if result.returncode not in check.ok:
        raise ToolMissingError(
            f"{title} check {check.tool} failed (exit {result.returncode}):\n"
            + (result.stderr or result.stdout)[-1500:]
        )
    found: Counter[str] = Counter()
    wanted = set(present)
    for match in FINDING.finditer(result.stdout):
        path = Path(match["path"])
        absolute = path if path.is_absolute() else root / path
        try:
            relative = absolute.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            continue
        if relative in wanted:
            found[relative] += 1
    return found


def _generated(root: Path) -> set[str]:
    project_json = root / "project.json"
    if not project_json.is_file():
        return set()
    sources = json.loads(project_json.read_text()).get("sources", [])
    return {s["path"] for s in sources if s.get("ownership") == "generated"}


def _examples(paths: Iterable[str]) -> set[str]:
    """The example files and pinned guides of the instance.

    Examples violate the rules on purpose; guides keep their authors' style.
    """
    return {
        path
        for path in paths
        if Path(path).parts[:1] == ("standards",)
        and {"examples", REFERENCES} & set(Path(path).parts)
    }


def check_errors(
    root: Path,
    base: str,
    activations: Sequence[Activation],
    changed: Iterable[str],
) -> list[str]:
    """Changed files that gain findings of an active language's checkers.

    ``changed`` are paths relative to ``root``. Generated files of a Verdog
    project keep the compiler's style. A missing tool is an error.
    """
    # check: style.google_python
    # check: style.google_typescript
    # check: style.cpp_core
    paths = set(changed) - _generated(root) - _examples(changed)
    jobs = [
        (
            activation,
            check,
            sorted(
                p
                for p in paths & set(activation.files)
                if not any(fnmatch.fnmatch(p, glob) for glob in check.exclude)
            ),
        )
        for activation in activations
        for check in activation.checks
    ]
    jobs = [job for job in jobs if job[2]]
    if not jobs:
        return []
    errors: list[str] = []
    with tempfile.TemporaryDirectory() as checkout:
        subprocess.run(
            ["git", "worktree", "add", "-q", "--detach", checkout, base],
            cwd=root,
            capture_output=True,
            check=True,
        )
        try:
            for activation, check, job_paths in jobs:
                errors += _regressions(
                    activation, check, root, Path(checkout), job_paths
                )
        finally:
            subprocess.run(
                ["git", "worktree", "remove", "--force", checkout],
                cwd=root,
                capture_output=True,
            )
    return errors


def _regressions(
    activation: Activation,
    check: Check,
    root: Path,
    before: Path,
    paths: list[str],
) -> list[str]:
    try:
        after = count(check, activation.title, root, paths, root)
        earlier = count(check, activation.title, before, paths, root)
    except ToolMissingError as missing:
        return [str(missing)]
    follow = (
        f"follow the {activation.title} guidelines it checks: "
        + ", ".join(check.guidelines)
        if check.guidelines
        else f"follow the {activation.title} guidelines"
    )
    return [
        f"{path} gains {check.tool} findings "
        f"({earlier[path]} -> {after[path]}); "
        f"{follow}."
        for path in paths
        if after[path] > earlier[path]
    ]


def changed_files(root: Path, base: str) -> list[str]:
    """The authored files changed since ``base``, staging untracked ones."""
    subprocess.run(
        ["git", "add", "-A"], cwd=root, capture_output=True, check=True
    )
    listed = subprocess.run(
        ["git", "diff", "--cached", "--name-only", base],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return sorted(set(listed.stdout.split()) - _generated(root))


def style_errors(root: Path, base: str, changed: Iterable[str]) -> list[str]:
    """Check ``changed`` against the checkers of their languages."""
    paths = sorted(set(changed))
    return check_errors(root, base, select(load(), paths, root, base), paths)


def changed_errors(root: Path, base: str) -> list[str]:
    """Style and complexity errors of the authored files changed since base."""
    changed = changed_files(root, base)
    return style_errors(root, base, changed) + complexity_errors(
        root, base, changed
    )


def _functions(source: str) -> dict[str, tuple[int, str]]:
    """Complexity and source of each function in ``source``, by its name."""
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder, "module.py")
        path.write_text(source)
        found = subprocess.run(
            [sys.executable, "-m", "radon", "cc", "-j", str(path)],
            capture_output=True,
            text=True,
            check=True,
        )
    blocks: list[dict[str, Any]] = json.loads(found.stdout).get(str(path), [])
    if isinstance(blocks, dict):
        raise SyntaxError(str(blocks))  # radon reports {"error": ...}
    shapes = _shapes(ast.parse(source))
    functions: dict[str, tuple[int, str]] = {}
    for b in blocks:
        if b["type"] in ("function", "method"):
            name = (
                f"{b['classname']}.{b['name']}"
                if b.get("classname")
                else b["name"]
            )
            functions[name] = (int(b["complexity"]), shapes.get(name, ""))
    return functions


def _shapes(tree: ast.Module) -> dict[str, str]:
    """The syntax of each function and method without docstrings, by name.

    Reformatting or documenting a function does not change it.
    """
    documented = (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.walk(tree):
        if isinstance(node, documented) and ast.get_docstring(node) is not None:
            node.body = node.body[1:]
    functions = (ast.FunctionDef, ast.AsyncFunctionDef)
    found: dict[str, str] = {}
    for node in tree.body:
        members = node.body if isinstance(node, ast.ClassDef) else []
        prefix = f"{node.name}." if isinstance(node, ast.ClassDef) else ""
        for f in [node, *members]:
            if isinstance(f, functions):
                found[prefix + f.name if f is not node else f.name] = ast.dump(
                    f
                )
    return found


def complexity_errors(
    root: Path, base: str, changed: Iterable[str]
) -> list[str]:
    """New or changed Python functions above ``COMPLEXITY_LIMIT`` (radon).

    A function counts as changed when its source differs from the base's; an
    untouched complex function in a changed file is not this change's. Tests
    are exempt: every assertion counts as a branch.
    """
    tests = load()["python"].tests
    errors: list[str] = []
    for path in sorted(set(changed) - _examples(changed)):
        is_test = any(fnmatch.fnmatch(path, glob) for glob in tests)
        if not is_test and path.endswith(".py") and (root / path).is_file():
            errors += _complex_functions(root, base, path)
    return errors


def _complex_functions(root: Path, base: str, path: str) -> list[str]:
    earlier = subprocess.run(
        ["git", "show", f"{base}:{path}"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    try:
        before = _functions(earlier.stdout) if earlier.returncode == 0 else {}
        after = _functions((root / path).read_text())
    except SyntaxError:
        return []  # ponytail: the type checker and tests report broken files
    return [
        f"{path}: {name} has cyclomatic complexity {cc} > "
        f"{COMPLEXITY_LIMIT}; split it into smaller functions."
        for name, (cc, text) in after.items()
        if cc > COMPLEXITY_LIMIT and before.get(name, (0, ""))[1] != text
    ]


def literature() -> dict[str, dict[str, str]]:
    """Title, url and category (``keywords``) of each literature entry."""
    text = (STANDARDS / LITERATURE).read_text()
    return {
        key: dict(
            re.findall(r"^  (title|url|keywords) = \{(.*)\},?$", body, re.M)
        )
        for key, body in re.findall(
            r"^@\w+\{([^,\s]+),\n(.*?)^\}", text, re.M | re.S
        )
    }


def citations() -> set[str]:
    """The keys of ``literature.bib``, the only literature cited."""
    text = (STANDARDS / LITERATURE).read_text()
    return set(re.findall(r"^@\w+\{([^,\s]+),", text, re.M))


def _guideline_errors(
    language: Language,
    g: Guideline,
    known: set[str],
    metrics: Mapping[str, Any],
    checkers: set[str],
) -> list[str]:
    """What is wrong with guideline ``g`` beyond its fields' types."""
    found = list(g.errors)
    if g.cite and g.cite not in known:
        found.append(f"{g.key}: unknown cite key {g.cite!r}")
    if g.checked_by and g.checked_by not in checkers:
        found.append(
            f"{g.key}: {g.checked_by!r} is not a checker of {language.id}"
        )
    if g.judged_by and g.judged_by not in metrics:
        found.append(f"{g.key}: {g.judged_by!r} is not a metric")
    if g.reference and section(language, g.reference) is None:
        found.append(f"{g.key}: reference {g.reference} is not a section")
    return found + _missing_files(language, g)


def _missing_files(language: Language, g: Guideline) -> list[str]:
    return [
        f"{g.key}: {name} file {getattr(g, name)} does not exist"
        for name in ("good", "bad", "details")
        if getattr(g, name)
        and not (Path(language.path) / getattr(g, name)).is_file()
    ]


def lint(language: Language) -> list[str]:
    """Format errors of one language folder, each naming its file and line."""
    found = list(language.errors)
    known = citations()
    metrics = declarations("metrics")
    checkers = {t.name for t in language.tools if t.role == "checker"}
    for g in language.guidelines:
        found += [
            (g.path, g.line, error)
            for error in _guideline_errors(
                language, g, known, metrics, checkers
            )
        ]
    for tool in language.tools:
        found += [(tool.path, tool.line, error) for error in tool.errors]
    return [f"{path}:{line}: {message}." for path, line, message in found]


def lint_all() -> list[str]:
    """The format errors of every language folder of the instance."""
    return [error for language in load().values() for error in lint(language)]


def _example_build(folder: Path, build: Path) -> Path:
    """A compile database for the examples of ``folder``, for C++ checkers.

    Clang needs absolute paths, so the database is written per run, not kept.
    """
    examples = sorted((folder / "examples").glob("*.c*"))
    (build / "build").mkdir(parents=True, exist_ok=True)
    (build / "build" / "compile_commands.json").write_text(
        json.dumps(
            [
                {
                    "directory": str(folder),
                    "file": str(path),
                    "command": f"c++ -std=c++20 -c {path}",
                }
                for path in examples
            ]
        )
    )
    return build


def _example_failures(
    language: Language, g: Guideline, check: Check, folder: Path
) -> list[str]:
    """Whether ``check`` flags ``g``'s bad example and spares its good one."""
    try:
        with tempfile.TemporaryDirectory() as scratch:
            build = _example_build(folder, Path(scratch))
            found = {
                name: count(check, language.title, folder, [path], build)[path]
                for name, path in (("bad", g.bad), ("good", g.good))
                if path
            }
    except ToolMissingError as missing:
        return [f"{g.key}: {missing}"]
    failures: list[str] = []
    if g.bad and not found["bad"]:
        failures.append(f"{g.key}: {g.checked_by} does not flag {g.bad}.")
    if g.good and found["good"]:
        failures.append(f"{g.key}: {g.checked_by} flags {g.good}.")
    return failures


def example_tests(standards: Standards) -> list[str]:
    """Guidelines whose tool misses the bad example or flags the good one."""
    failures: list[str] = []
    for language in standards.values():
        checks = {c.tool: c for c in language.checkers()}
        folder = Path(language.path).resolve()
        for g in language.guidelines:
            check = checks.get(g.checked_by)
            if check is not None and (g.good or g.bad):
                failures += _example_failures(language, g, check, folder)
    return failures


def loops() -> list[str]:
    """The consumer's loops (``LOOPS``); the organization itself has none."""
    return list(LOOPS)


def overview(standards: Standards) -> str:
    """A Markdown table of the languages of ``standards``."""
    lines = [
        "| language | title | extensions | guidelines | tools |",
        "|---|---|---|---|---|",
    ]
    lines += [
        f"| {lang.id} | {lang.title} | "
        f"{' '.join(lang.extensions) or 'all files'} | "
        f"{len(lang.guidelines)} | {', '.join(t.name for t in lang.tools)} |"
        for _, lang in sorted(standards.items())
    ]
    return "\n".join(lines) + "\n"


# The graph of the standards: nodes and edges derived from the files only.
GRAPH_PAGE: Final = Path(__file__).with_name("graph.html")
# The page itself is a fragment; a local file needs the document frame.
PAGE_FRAME: Final = (
    '<!doctype html><html lang="en"><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1, '
    'viewport-fit=cover">'
    "<style>body{margin:0}[hidden]{display:none!important}</style>"
)


@dataclass(frozen=True, slots=True)
class Node:
    """A node of the standards graph; ``parent`` gives the hierarchy."""

    id: str
    type: str
    label: str
    parent: str = ""
    data: dict[str, Any] = field(default_factory=dict[str, Any])


@dataclass(frozen=True, slots=True)
class Edge:
    """A typed, directed edge of the standards graph."""

    source: str
    target: str
    type: str


@dataclass(frozen=True, slots=True)
class Finding:
    """A coherence warning: its rule, why, and the nodes involved."""

    rule: str
    message: str
    nodes: tuple[str, ...]


@dataclass
class Graph:
    """Nodes and edges of the standards of a repository."""

    nodes: dict[str, Node] = field(default_factory=dict[str, Node])
    edges: list[Edge] = field(default_factory=list[Edge])

    def add(self, node: Node) -> str:
        """Add ``node`` once and return its id."""
        self.nodes.setdefault(node.id, node)
        return node.id

    def link(self, source: str, target: str, kind: str) -> None:
        """Add an edge."""
        self.edges.append(Edge(source, target, kind))

    def shared(self, kind: str, name: str, /, **data: Any) -> str:
        """The id of a node outside the hierarchy: metric, loop, source."""
        return self.add(Node(f"{kind}:{name}", kind, name, "", dict(data)))


def _language_node(graph: Graph, language: Language) -> None:
    """Add a language with its tools and guidelines."""
    node = graph.add(
        Node(
            f"language:{language.id}",
            "language",
            language.id,
            "",
            {
                "title": language.title,
                "extensions": list(language.extensions),
                "path": language.path,
            },
        )
    )
    for tool in language.tools:
        _tool_node(graph, node, tool)
    for g in language.guidelines:
        _guideline_node(graph, node, language, g)


def _tool_node(graph: Graph, parent: str, tool: Tool) -> None:
    tool_id = graph.add(
        Node(
            f"tool:{tool.language}/{tool.name}",
            "tool",
            tool.name,
            parent,
            {**asdict(tool), "errors": None},
        )
    )
    if tool.role == "retriever":
        wanted = [s.get("loops") for s in tool.retrieval.values()]
        reached = (
            loops()
            if not all(wanted)
            else sorted({loop for w in wanted for loop in w or ()})
        )
        for loop in reached:
            graph.link(graph.shared("loop", loop), tool_id, "retrieves")
    if tool.cite:
        source = graph.shared("source", tool.cite, kind="literature")
        graph.link(tool_id, source, "cites")


def _guideline_node(
    graph: Graph, parent: str, language: Language, g: Guideline
) -> None:
    text = section(language, g.reference) if g.reference else ""
    guideline = graph.add(
        Node(
            f"guideline:{g.key}",
            "guideline",
            g.id,
            parent,
            {
                **asdict(g),
                "key": g.key,
                "errors": None,
                "section": (text or "")[:SECTION_CHARS],
            },
        )
    )
    tool = f"tool:{language.id}/{g.checked_by}"
    if g.checked_by and tool in graph.nodes:
        graph.link(guideline, tool, "checked_by")
    if g.cite:
        source = graph.shared("source", g.cite, kind="literature")
        graph.link(guideline, source, "cites")


def _metric_node(graph: Graph, name: str, metric: dict[str, Any]) -> None:
    node = graph.shared("metric", name, **metric)
    if metric.get("status") == "planned":
        return  # Nothing measures it yet, so it judges nothing.
    # An empty ``loops`` means every loop.
    for loop in metric.get("loops") or loops():
        graph.link(node, graph.shared("loop", loop), "judges")
    for key in metric.get("cite", ()):
        source = graph.shared("source", key, kind="literature")
        graph.link(node, source, "cites")
    for tool in metric.get("measured_by", ()):
        if f"tool:{GENERAL}/{tool}" in graph.nodes:
            graph.link(node, f"tool:{GENERAL}/{tool}", "measured_by")


def _judged_by(graph: Graph) -> None:
    """Link each guideline a metric enforces to that metric."""
    for guideline in [n for n in graph.nodes.values() if n.type == "guideline"]:
        metric = f"metric:{guideline.data['judged_by']}"
        if guideline.data["judged_by"] and metric in graph.nodes:
            graph.link(guideline.id, metric, "judged_by")


def graph() -> Graph:
    """The instance's graph: languages, guidelines, tools, metrics, loops."""
    result = Graph()
    for language in load().values():
        _language_node(result, language)
    for name, metric in declarations("metrics").items():
        _metric_node(result, name, metric)
    _judged_by(result)
    entries = literature()
    for node in result.nodes.values():
        entry = entries.get(node.label, {}) if node.type == "source" else None
        if entry is not None:
            category = CATEGORIES.get(entry.get("keywords", ""), "")
            node.data.update(entry, category=category)
    return result


def metric_tool_errors() -> list[str]:
    """Metrics naming a measurer ``general/tools.toml`` does not declare."""
    measurers = {t.name for t in load()[GENERAL].tools if t.role == "measurer"}
    return [
        f"metrics.toml: {name} is measured by {tool!r}, which "
        f"{GENERAL}/tools.toml does not declare as a measurer."
        for name, metric in declarations("metrics").items()
        for tool in metric.get("measured_by", ())
        if tool not in measurers
    ]


def coherence(graph: Graph) -> list[Finding]:
    """The structural warnings of ``graph``.

    Every checker checks a guideline, every language has guidelines, and every
    source and metric in force has an edge.
    """
    incoming: dict[str, set[str]] = {}
    for edge in graph.edges:
        incoming.setdefault(edge.target, set()).add(edge.type)
    return [
        *_unused_checkers(graph, incoming),
        *_empty_languages(graph),
        *_unlinked(graph),
    ]


def _unused_checkers(
    graph: Graph, incoming: Mapping[str, set[str]]
) -> list[Finding]:
    return [
        Finding(
            "tools",
            f"checker {n.data['language']}/{n.label} runs, "
            "but no guideline names it.",
            (n.id,),
        )
        for n in graph.nodes.values()
        if n.type == "tool"
        and n.data["role"] == "checker"
        and "checked_by" not in incoming.get(n.id, set())
    ] + [
        Finding(
            "tools",
            f"measurer {n.label} is declared, but no metric names it.",
            (n.id,),
        )
        for n in graph.nodes.values()
        if n.type == "tool"
        and n.data["role"] == "measurer"
        and "measured_by" not in incoming.get(n.id, set())
    ]


def _empty_languages(graph: Graph) -> list[Finding]:
    parents = {n.parent for n in graph.nodes.values() if n.type == "guideline"}
    return [
        Finding("guidelines", f"language {n.label} has no guidelines.", (n.id,))
        for n in graph.nodes.values()
        if n.type == "language" and n.id not in parents
    ]


def _unlinked(graph: Graph) -> list[Finding]:
    """Sources and metrics in force without any edge."""
    linked = {end for e in graph.edges for end in (e.source, e.target)}
    return [
        Finding("unlinked", f"{n.type} {n.label} has no edge.", (n.id,))
        for n in graph.nodes.values()
        if n.type in ("source", "metric")
        and n.data.get("status") != "planned"
        and n.id not in linked
    ]


def graph_page(
    standards_graph: Graph, title: str, prompts: Mapping[str, str] | None = None
) -> str:
    """The browsable HTML page of ``standards_graph``, with its findings.

    ``prompts`` are a consumer's prompt sections by language, for the loop view.
    """
    data = {
        "title": title,
        "nodes": [asdict(node) for node in standards_graph.nodes.values()],
        "edges": [asdict(edge) for edge in standards_graph.edges],
        "findings": [asdict(finding) for finding in coherence(standards_graph)],
        "prompts": dict(prompts or {}),
    }
    payload = json.dumps(data).replace("</", "<\\/")
    return GRAPH_PAGE.read_text().replace("/*GRAPH*/null", payload)
