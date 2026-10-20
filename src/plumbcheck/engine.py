"""The engine of the organization's standards: load, select, map.

The instance is the folder ``standards/`` of ``verdog-ai/standards``, which the
user selects and no loop changes; this module only interprets it. Each
language has a folder ``languages/<language>/`` with ``language.toml`` (title,
extensions, project marker, test globs) and ``overlay.toml`` (what it adds to
the rules of its guides, and its house rules). ``general/`` holds the house
rules for every repository and ``tools.toml``, the measurers of
``metrics.toml``; ``sources.toml`` names the guides the guidelines derive from
(see ``sources``). The checkers are the ``LINTERS`` of ``standards.yml``, run
by ``linting``.

A guideline is a rule of a guide, named by its index id
(``cpp-core-guidelines/R.20``) and curated in the overlay, or a house rule
(``house/<id>`` in the overlay, ``<language>/<id>`` as its key). The prompt
shows three levels: each active guide's top sections and the curated rules
with their leaves; the agent looks up the levels below; and a lint's
repair message names each finding with its rule and leaf.

A consumer such as auto-se adds what concerns only itself through ``LAYERS``
(extra ``<kind>.toml`` and ``general/tools.toml`` roots), ``LOOPS`` and
``CHECKS``; it never changes the organization's rules.
"""

import fnmatch
import functools
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Final, cast

from verdog_standards import config, sources

# The instance: everything repositories follow and are judged by.
STANDARDS: Path = Path(
    os.environ.get("VERDOG_STANDARDS") or config.data("standards")
)
# What a consumer adds on top, never changing the rules: roots with extra
# ``<kind>.toml`` tables and ``general/tools.toml`` tools, its loop names and
# extra ``check`` errors.
LAYERS: list[Path] = []
LOOPS: list[str] = []
CHECKS: list[Callable[[], list[str]]] = []
CACHE: Final = Path("~/.cache/verdog-standards")
BIN: Final = CACHE / "bin"  # the tools ``verdog-standards install`` downloads
LANGUAGES: Final = "languages"
GENERAL: Final = "general"
MANIFEST_NAME: Final = sources.MANIFEST
MAIN_BRANCH: str = ""  # empty: the remote's default branch (origin/HEAD)
GUIDELINE_ID: Final = re.compile(r"[a-z][a-z0-9-]*")
KINDS: Final = ("coding", "documentation")
# The roles of ``tools.toml``; the checkers are the ``LINTERS``.
ROLES: Final = ("retriever", "measurer")
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
# The fields of an overlay table. ``checked_by`` names ``tool`` or
# ``tool:check`` (a glob such as ``ruff:D*``), ``tool`` an alias of a linter;
# ``priority`` lists the guideline in the prompt, lowest first; ``see``
# points a house rule at a rule of a guide.
GUIDELINE_FIELDS: Final = {
    "text": str,
    "kind": str,
    "advisory": bool,
    "link": str,
    "details": str,
    "checked_by": list,
    "judged_by": str,
    "priority": int,
    "see": str,
}
OVERLAY: Final = "overlay.toml"
HOUSE: Final = "house"  # the overlay's own rules: ``house/<id>``
REFERENCES: Final = "references"
SECTION_CHARS: Final = 4000  # of a rule's text, shown on the map
# Where a target repository's worktree gets the pinned guides it may read.
WORKTREE_REFERENCES: str = (
    ".verdog-standards/references"  # a consumer may move it
)
TOOL_FIELDS: Final = {"role": str, "link": str}


@dataclass(frozen=True, slots=True)
class Guideline:
    """One guideline: a table of a language's ``overlay.toml``.

    ``id`` is the table's key: a rule's index id, or ``house/<id>``.
    """

    language: str
    id: str
    text: str = ""
    path: str = ""
    line: int = 1
    kind: str = "coding"
    advisory: bool = False
    link: str = ""
    details: str = ""
    checked_by: tuple[str, ...] = ()
    judged_by: str = ""
    priority: int = 0
    see: str = ""
    errors: tuple[str, ...] = ()

    @property
    def source(self) -> str:
        """``house``, or the id of the guide whose rule this is."""
        return self.id.partition("/")[0]

    @property
    def key(self) -> str:
        """The unique name: ``<language>/<id>`` of a house rule, else its id."""
        if self.source == HOUSE:
            return f"{self.language}/{self.id.partition('/')[2]}"
        return self.id

    @property
    def tools(self) -> tuple[str, ...]:
        """The checkers ``checked_by`` names."""
        return tuple(
            dict.fromkeys(c.partition(":")[0] for c in self.checked_by)
        )

    @property
    def rule(self) -> str:
        """The index id of the rule whose leaf holds this guideline's text."""
        return self.see if self.source == HOUSE else self.id


@dataclass(frozen=True, slots=True)
class Tool:
    """One tool: a checker, a retriever of context or a measurer of metrics.

    A checker is an alias of a linter of ``standards.yml``; measurers are
    declared in ``tools.toml`` and run by their consumer's own code, such as
    auto-se's gate.
    """

    language: str
    name: str
    role: str = "checker"
    linter: str = ""  # the key in ``LINTERS``
    link: str = ""
    list: str = ""  # a command printing the checks the checker runs
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


@dataclass(frozen=True, slots=True)
class Activation:
    """A language active for a task: why, on which files, and its guidelines."""

    language: str
    title: str
    reasons: tuple[str, ...]
    files: tuple[str, ...]
    guidelines: tuple[dict[str, Any], ...] = ()
    sha256: str = ""
    guides: tuple[dict[str, Any], ...] = ()


type Standards = dict[str, Language]


def search_path() -> str:
    """``PATH`` with the engine's environment and the downloaded tools first."""
    return os.pathsep.join(
        (
            str(Path(sys.executable).parent),
            str(BIN.expanduser()),
            os.environ.get("PATH", ""),
        )
    )


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
    errors += _key_errors(f"{language}/{name}", name, table)
    if table.get("kind", "coding") not in KINDS:
        errors.append(f"{language}/{name}: kind must be one of {KINDS}")
    known: dict[str, Any] = {
        k: tuple(cast(list[str], v)) if k == "checked_by" else v
        for k, v in table.items()
        if k in GUIDELINE_FIELDS and not errors
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


def _key_errors(subject: str, name: str, table: dict[str, Any]) -> list[str]:
    """A key is ``house/<id>`` with a text, or ``<guide>/<rule id>``."""
    source, _, rule = name.partition("/")
    if not rule:
        return [f"{subject}: the key must be house/<id> or <guide>/<rule>"]
    if source != HOUSE:
        return []
    errors: list[str] = []
    if not GUIDELINE_ID.fullmatch(rule):
        errors.append(f"{subject}: the id must match {GUIDELINE_ID.pattern}")
    if not str(table.get("text", "")).strip():
        errors.append(f"{subject}: a house rule needs a text")
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


def _tool(
    language: str, name: str, table: dict[str, Any], where: str, text: str
) -> Tool:
    """A table of ``tools.toml``; a retriever's sub-tables are its retrievals."""
    plain = {k: v for k, v in table.items() if not isinstance(v, dict)}
    errors = _field_errors(plain, TOOL_FIELDS, f"{language}/{name}")
    role = str(table.get("role", ""))
    if role not in ROLES:
        errors.append(
            f"{language}/{name}: role must be one of {ROLES}; checkers are "
            "the LINTERS of standards.yml"
        )
    return Tool(
        language=language,
        name=name,
        role=role,
        link=str(table.get("link", "")),
        retrieval={
            k: cast(dict[str, Any], v)
            for k, v in table.items()
            if isinstance(v, dict)
        },
        path=where,
        line=_line(text, name),
        errors=tuple(errors),
    )


def checkers(language: str) -> tuple[Tool, ...]:
    """The aliases of the organization's linters of ``language``."""
    linters = cast(
        dict[str, dict[str, Any]],
        config.resolve(str(config.ORGANIZATION)).get("LINTERS", {}),
    )
    return tuple(
        Tool(
            language=language,
            name=alias,
            linter=key,
            link=str(spec.get("link", "")),
            list=str(spec.get("list", "")),
            path=str(config.ORGANIZATION),
        )
        for key, spec in linters.items()
        if spec.get("language") == language
        for alias in spec.get("aliases", [key])
    )


def parse(folder: Path, name: str) -> Language:
    """The language in ``folder``; ``ValueError`` on a TOML syntax error."""
    header, header_text = _read(folder / "language.toml")
    found, text = _read(folder / OVERLAY)
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
            _guideline(name, key, dict(table), str(folder / OVERLAY), text)
            for key, table in found.items()
        ),
        tools=checkers(name)
        + tuple(
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


def guides() -> dict[str, sources.Source]:
    """The guides of ``sources.toml``, by id."""
    return sources.manifest(STANDARDS)


def curations(source: sources.Source) -> dict[str, tuple[str, ...]]:
    """The checkers the overlay of ``source``'s language maps to its rules."""
    language = load().get(source.language)
    return {
        g.id: g.checked_by
        for g in (language.guidelines if language else ())
        if g.source == source.id
    }


def _render(
    source: sources.Source,
) -> tuple[Path, dict[str, str], list[sources.Rule]]:
    """The folder, files and index of ``source``'s tree, from its copy."""
    folder = sources.tree_folder(source, STANDARDS, CACHE)
    copy = sources.fetch(source, folder)
    files, rules = sources.render(source, copy, curations(source))
    return folder, files, rules


def generate(source: sources.Source) -> list[sources.Rule]:
    """Write ``source``'s tree from its pinned copy; return its index."""
    folder, files, rules = _render(source)
    stamp = {STAMP: _stamp(source)} if source.store == "cache" else {}
    sources.write(folder, {**files, **stamp})
    return rules


STAMP: Final = ".generated"  # what a cached tree was generated from


def _stamp(source: sources.Source) -> str:
    """The generator and overlay a tree derives from, as a digest."""
    data = (
        Path(sources.__file__).read_bytes() + repr(curations(source)).encode()
    )
    return hashlib.sha256(data).hexdigest() + "\n"


def tree(source: sources.Source) -> Path:
    """The folder of ``source``'s tree.

    A cached tree is generated on first use and again when the generator or
    the overlay changed; a committed one is as committed.
    """
    folder = sources.tree_folder(source, STANDARDS, CACHE)
    if source.store == "cache":
        stamp = folder / STAMP
        if not stamp.is_file() or stamp.read_text() != _stamp(source):
            generate(source)
    return folder


def index(source: sources.Source) -> dict[str, sources.Rule]:
    """The entries of ``source``'s tree by id; ``OSError`` when unreadable."""
    path = tree(source) / sources.INDEX_TOML
    return _index(str(path), path.stat().st_mtime)


@functools.cache
def _index(path: str, mtime: float) -> dict[str, sources.Rule]:
    del mtime  # part of the cache's key only
    return {r.id: r for r in sources.read_index(Path(path).parent)}


def rule(name: str) -> sources.Rule | None:
    """The index entry ``name`` (``cpp-core-guidelines/R.20``), if readable."""
    source = guides().get(name.partition("/")[0])
    try:
        return index(source).get(name) if source else None
    except (OSError, ValueError):
        return None  # ponytail: offline and not cached; shown without its text


def location(
    root: Path, source: sources.Source, path: str, *, here: bool = False
) -> str:
    """Where an agent working in ``root`` opens ``path`` of ``source``'s tree.

    A worktree reads the copies ``copy_references`` put into it, the
    standards repository its committed trees. Elsewhere it is that copy's
    path too, which a prompt's worktree gets; with ``here``, the tree on this
    machine instead, for a check's message.
    """
    copied = Path(WORKTREE_REFERENCES, source.id)
    if (root / copied).is_dir():
        return (copied / path).as_posix()
    folder = sources.tree_folder(source, STANDARDS, CACHE)
    if source.store == "commit" and folder.is_relative_to(STANDARDS.parent):
        mine = folder.relative_to(STANDARDS.parent)
        if (root / mine).is_dir():
            return (mine / path).as_posix()
    return (
        str(folder.expanduser() / path) if here else (copied / path).as_posix()
    )


def copy_references(root: Path) -> None:
    """Copy every guide's tree into ``root``'s ignored ``WORKTREE_REFERENCES``.

    An agent in another repository then reads them offline, cached ones
    included; the pinned copies themselves stay behind.
    """
    for source in guides().values():
        try:
            folder = tree(source)
        except (OSError, ValueError):
            continue  # ponytail: offline and not cached; its rules show no leaf
        shutil.copytree(
            folder,
            root / WORKTREE_REFERENCES / source.id,
            ignore=shutil.ignore_patterns(sources.SOURCE, STAMP),
            dirs_exist_ok=True,
        )


def source_errors() -> list[str]:
    """Guides whose copy, completeness or committed tree is wrong.

    A committed tree must equal what ``verdog-standards index`` generates; a
    cached guide must never be committed.
    """
    errors: list[str] = []
    for source in guides().values():
        errors += _tree_errors(source)
    return errors + committed_cache_errors()


def _tree_errors(source: sources.Source) -> list[str]:
    try:
        folder, files, _ = _render(source)
    except (OSError, ValueError) as error:
        return [f"{MANIFEST_NAME}: {error}."]
    if source.store == "cache":
        sources.write(folder, {**files, STAMP: _stamp(source)})
        return []
    found = sources.stale(folder, files)
    return (
        [
            f"{source.id}: {len(found)} generated files differ from the "
            f"source and overlay, such as {found[0]}; run "
            "`verdog-standards index`."
        ]
        if found
        else []
    )


def committed_cache_errors() -> list[str]:
    """Files of the repository that hold a cache-only guide or its tree."""
    listed = subprocess.run(
        ["git", "-C", str(STANDARDS.parent), "ls-files"],
        capture_output=True,
        text=True,
    )
    tracked = listed.stdout.splitlines() if listed.returncode == 0 else []
    cached = [s for s in guides().values() if s.store == "cache"]
    return [
        f"{path}: {source.id} allows no public copy; never commit it."
        for path in tracked
        for source in cached
        if source.id in Path(path).parts or Path(path).name in source.files
    ]


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
    prompted = sorted(
        (g for g in language.guidelines if g.priority),
        key=lambda g: (g.priority, g.key),
    )
    return Activation(
        language=language.id,
        title=language.title,
        reasons=tuple(reasons),
        files=tuple(sorted(changed)),
        guidelines=tuple(_prompted(g, root) for g in prompted),
        sha256=hashlib.sha256(language.text.encode()).hexdigest(),
        guides=tuple(_guide_of(s, root) for s in _guides_of(language)),
    )


def _guides_of(language: Language) -> list[sources.Source]:
    return [s for s in guides().values() if s.language == language.id]


def _guide_of(source: sources.Source, root: Path) -> dict[str, Any]:
    """A guide as the prompt shows it: its notice and its top sections."""
    try:
        top = (tree(source) / sources.INDEX).read_text().splitlines()
    except (OSError, ValueError):
        top = []  # ponytail: offline and not cached; the prompt lists no sections
    return {
        "id": source.id,
        "title": source.title,
        "license": source.license,
        "notice": sources.notice(source),
        "root": location(root, source, ""),
        "lines": [line for line in top if line.startswith("- ")],
    }


def resolved(
    g: Guideline,
) -> tuple[sources.Source | None, sources.Rule | None]:
    """The guide and index entry whose leaf holds ``g``'s rule, if any."""
    entry = rule(g.rule) if g.rule else None
    return (
        (guides().get(g.rule.partition("/")[0]), entry)
        if entry
        else (None, None)
    )


def _prompted(g: Guideline, root: Path) -> dict[str, Any]:
    """A guideline as the prompt shows it: its text, checkers and leaf."""
    source, entry = resolved(g)
    return {
        **asdict(g),
        "key": g.key,
        "text": g.text or (entry.text if entry else g.id),
        "checked_by": list(g.checked_by),
        "tools": list(g.tools),
        "enforcement": sources.enforcement(g.checked_by),
        "pointer": location(root, source, entry.path)
        if source and entry
        else "",
    }


def for_loop(
    activations: Sequence[Activation], loop: str
) -> list[dict[str, Any]]:
    """What ``loop`` sees: per language its guides, then its guidelines.

    Every loop working on a language's files sees the top sections of its
    guides and its curated guidelines, coding then documentation.
    """
    del loop
    groups = [
        {
            "language": a.language,
            "title": a.title,
            "reasons": list(a.reasons),
            "guides": [dict(g) for g in a.guides],
            **{
                kind: [g for g in a.guidelines if g["kind"] == kind]
                for kind in KINDS
            },
        }
        for a in activations
    ]
    return [g for g in groups if g["guides"] or any(g[k] for k in KINDS)]


def generated(root: Path) -> set[str]:
    """The files of a Verdog project that its compiler generates."""
    project_json = root / "project.json"
    if not project_json.is_file():
        return set()
    sources = json.loads(project_json.read_text()).get("sources", [])
    return {s["path"] for s in sources if s.get("ownership") == "generated"}


def _references(paths: Iterable[str]) -> set[str]:
    """The pinned guides of the instance; they keep their authors' style."""
    return {
        path
        for path in paths
        if Path(path).parts[:1] == ("standards",)
        and REFERENCES in Path(path).parts
    }


def authored(root: Path, changed: Iterable[str]) -> set[str]:
    """The files still present that are neither generated nor guides."""
    present = {p for p in changed if (root / p).is_file()}
    return present - generated(root) - _references(present)


def rule_line(g: Guideline, root: Path) -> str:
    """A guideline's index line: id, rule, enforcement and its leaf."""
    source, entry = resolved(g)
    text = g.text or (entry.text if entry else g.key)
    leaf = (
        location(root, source, entry.path, here=True)
        if source and entry
        else ""
    )
    where = f" → {leaf}" if leaf else (f" → {g.link}" if g.link else "")
    return f"- {g.key}: {text} {sources.enforcement(g.checked_by)}{where}"


def _guideline_errors(
    language: Language,
    g: Guideline,
    metrics: Mapping[str, Any],
    checkers: set[str],
) -> list[str]:
    """What is wrong with guideline ``g`` beyond its fields' types."""
    found = [*g.errors, *_named_errors(g, metrics)]
    found += [
        f"{g.key}: {tool!r} is not a checker of {language.id}"
        for tool in g.tools
        if tool not in checkers
    ]
    names = [g.id] if g.source != HOUSE else []
    found += [
        _rule_error(language, g, name) for name in [*names, g.see] if name
    ]
    return [f for f in found if f] + _missing_files(language, g)


def _named_errors(g: Guideline, metrics: Mapping[str, Any]) -> list[str]:
    """A ``judged_by`` that is no metric."""
    found: list[str] = []
    if g.judged_by and g.judged_by not in metrics:
        found.append(f"{g.key}: {g.judged_by!r} is not a metric")
    return found


def _rule_error(language: Language, g: Guideline, name: str) -> str:
    """Why ``name`` is no rule of a guide of ``language``, or ""."""
    source = guides().get(name.partition("/")[0])
    if source is None or source.language != language.id:
        return (
            f"{g.key}: {name.partition('/')[0]!r} is no guide of {language.id}"
        )
    try:
        known = index(source)
    except (OSError, ValueError) as error:
        return f"{g.key}: the index of {source.id} cannot be read: {error}"
    return "" if name in known else f"{g.key}: {name} is no rule of {source.id}"


def _missing_files(language: Language, g: Guideline) -> list[str]:
    if g.details and not (Path(language.path) / g.details).is_file():
        return [f"{g.key}: details file {g.details} does not exist"]
    return []


def lint(language: Language) -> list[str]:
    """Format errors of one language folder, each naming its file and line."""
    found = list(language.errors)
    metrics = declarations("metrics")
    checkers = {t.name for t in language.tools if t.role == "checker"}
    for g in language.guidelines:
        found += [
            (g.path, g.line, error)
            for error in _guideline_errors(language, g, metrics, checkers)
        ]
    for tool in language.tools:
        found += [(tool.path, tool.line, error) for error in tool.errors]
    return [f"{path}:{line}: {message}." for path, line, message in found]


def lint_all() -> list[str]:
    """The format errors of every language folder of the instance."""
    return [error for language in load().values() for error in lint(language)]


def matches(entry: str, tool: str, check: str) -> bool:
    """Whether a ``checked_by`` entry (``tool`` or ``tool:glob``) covers it."""
    name, _, pattern = entry.partition(":")
    return name == tool and (not pattern or fnmatch.fnmatchcase(check, pattern))


def coverage() -> str:
    """The coverage of the guides by checkers, and its gaps.

    Per guide and top section the rules a checker covers and the prose ones;
    then the curated guidelines without a summary, and
    the checks no guideline maps.
    """
    loaded = load()
    lines = ["# Coverage of the guides", ""]
    for source in guides().values():
        lines += _guide_coverage(source, loaded[source.language])
    lines += ["## Gaps", ""]
    for language in loaded.values():
        lines += _gaps(language) + _unmapped(language)
    return "\n".join(lines) + "\n"


def _guide_coverage(source: sources.Source, language: Language) -> list[str]:
    head = [f"## {source.title} (`{source.id}`, {source.license})", ""]
    try:
        rules = index(source)
    except (OSError, ValueError) as error:
        return [*head, f"Cannot be read: {error}", ""]
    checked = {g.id for g in language.guidelines if g.checked_by}
    rows = ["| section | rules | checked | prose |", "|---|---|---|---|"]
    by_top: dict[str, list[bool]] = {}
    for r in rules.values():
        if r.leaf:
            by_top.setdefault(_top(r, rules), []).append(r.id in checked)
    rows += [
        f"| {rules[top].title} | {len(marks)} | {sum(marks)} | "
        f"{len(marks) - sum(marks)} |"
        for top, marks in by_top.items()
    ]
    return [*head, *rows, ""]


def _top(entry: sources.Rule, rules: Mapping[str, sources.Rule]) -> str:
    """The top section an entry belongs to, or the entry if it is one."""
    while entry.parent:
        entry = rules[entry.parent]
    return entry.id


def _gaps(language: Language) -> list[str]:
    """Curated guidelines without a summary."""
    return [
        f"- {g.key}: no summary"
        for g in language.guidelines
        if g.priority and _unsummarized(g)
    ]


def _unsummarized(g: Guideline) -> bool:
    """A rule of a prose guide shown by its title alone.

    A Core Guidelines rule's title is its one-line summary.
    """
    source, entry = resolved(g)
    if g.text or g.source == HOUSE or source is None or entry is None:
        return False
    return source.parser != "core-guidelines" and entry.text == entry.title


def _unmapped(language: Language) -> list[str]:
    """The checks a checker lists that no guideline's ``checked_by`` maps."""
    found: list[str] = []
    for tool in (t for t in language.tools if t.list):
        try:
            checks = _listed(tool)
        except OSError as missing:
            found.append(f"- {language.id}/{tool.name}: not listed ({missing})")
            continue
        found += [
            f"- {language.id}/{tool.name}: {check} maps to no guideline; "
            f"see {tool.link}"
            for check in checks
            if not any(
                matches(e, tool.name, check)
                for g in language.guidelines
                for e in g.checked_by
            )
        ]
    return found


def _listed(tool: Tool) -> list[str]:
    """The checks ``tool.list`` prints, one per line."""
    values = {"python": sys.executable, "rules": str(config.RULES)}
    result = subprocess.run(
        shlex.split(tool.list.format_map(values)),
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": search_path()},
    )
    names = (line.strip() for line in result.stdout.splitlines())
    return sorted({n for n in names if re.fullmatch(r"[\w.-]+", n)})


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


def _guideline_node(
    graph: Graph, parent: str, language: Language, g: Guideline
) -> None:
    source, entry = resolved(g)
    found = {"reference": "", "license": "", "attribution": "", "section": ""}
    if source and entry:
        found = {
            "reference": f"{source.id}/{entry.path}",
            "license": source.license,
            "attribution": source.attribution,
            "section": _leaf_text(source, entry)[:SECTION_CHARS],
        }
    data = {**asdict(g), "key": g.key, "errors": None, "tools": list(g.tools)}
    # A cache-only guide's text, its rules' titles included, stays off the map.
    public = source is not None and source.store == "commit"
    text = g.text or (entry.text if entry and public else "")
    guideline = graph.add(
        Node(
            f"guideline:{g.key}",
            "guideline",
            g.key,
            parent,
            {**data, **found, "text": text},
        )
    )
    for tool in g.tools:
        if f"tool:{language.id}/{tool}" in graph.nodes:
            graph.link(guideline, f"tool:{language.id}/{tool}", "checked_by")


def _leaf_text(source: sources.Source, entry: sources.Rule) -> str:
    """A committed rule's leaf for the map; never a cache-only guide's."""
    if source.store != "commit":
        return ""
    path = tree(source) / entry.path
    return path.read_text() if path.is_file() else ""


def _metric_node(graph: Graph, name: str, metric: dict[str, Any]) -> None:
    node = graph.shared("metric", name, **metric)
    if metric.get("status") == "planned":
        return  # Nothing measures it yet, so it judges nothing.
    # An empty ``loops`` means every loop.
    for loop in metric.get("loops") or loops():
        graph.link(node, graph.shared("loop", loop), "judges")
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
