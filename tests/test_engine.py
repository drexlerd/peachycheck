"""The engine: languages, guidelines, tools, selection, checks and graph."""

import hashlib
import json
import re
import shutil
import subprocess
from collections import Counter
from pathlib import Path

import pytest

from plumbcheck import cli, config
from plumbcheck import engine as standards

ORG = Path(__file__).parent / "fixtures" / "org"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _repository(tmp_path: Path, contents: dict[str, str]) -> tuple[Path, str]:
    """A committed repository of the sample organization."""
    root = tmp_path / "repository"
    root.mkdir()
    _write(root / config.NAME, f"EXTENDS: {ORG / config.NAME}\n")
    for path, text in contents.items():
        _write(root / path, text)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root, _git(root, "rev-parse", "HEAD").strip()


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _instance(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A copy of the instance that the test may change."""
    instance = tmp_path / "guides"
    shutil.copytree(ORG / "guides", instance)
    monkeypatch.setattr(standards.IN_USE, "guides", instance)
    return instance


def _organization(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, text: str
) -> Path:
    """An organization file extending the sample one with ``text``, in use."""
    path = _write(
        tmp_path / "org" / config.NAME, f"EXTENDS: {ORG / config.NAME}\n{text}"
    )
    standards.use(path.parent)
    return path


def _text(rule: str) -> str:
    entry = standards.rule(rule)
    assert entry is not None
    return entry.text


def _installed(tool: str) -> str:
    return f"/usr/bin/{tool}"


def _language(
    folder: Path, guidelines: str, tools: str = "", header: str = ""
) -> Path:
    _write(
        folder / "language.toml", header or 'title = "X"\nextensions = [".x"]\n'
    )
    _write(folder / "overlay.toml", guidelines)
    _write(folder / "tools.toml", tools)
    return folder


def test_a_guideline_table_carries_its_fields_and_line(tmp_path: Path) -> None:
    folder = _language(
        tmp_path / "cpp",
        '["house/no-raw-new"]\ntext = "Never call new directly."\n'
        'advisory = true\nchecked_by = ["clang-tidy"]\n'
        'kind = "coding"\n\n'
        '["some-guide/R.20"]\n'
        'checked_by = ["clang-tidy:cppcoreguidelines-owning-memory"]\n'
        "priority = 1\n",
        "",
        'title = "C++"\nextensions = [".cpp"]\n',
    )
    language = standards.parse(folder, "cpp")
    house, derived = language.guidelines
    assert (house.key, house.advisory, house.tools) == (
        "cpp/no-raw-new",
        True,
        ("clang-tidy",),
    )
    assert (house.line, derived.line) == (1, 7)
    assert (derived.key, derived.source, derived.priority) == (
        "some-guide/R.20",
        "some-guide",
        1,
    )
    assert derived.text == "" and derived.rule == "some-guide/R.20"
    # The checkers are the aliases of the configuration's linters.
    (tool,) = language.tools
    assert (tool.name, tool.linter, tool.role) == (
        "clang-tidy",
        "CPP_CLANG_TIDY",
        "checker",
    )


def test_only_the_instance_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root, base = _repository(
        tmp_path,
        {
            "a.cpp": "",
            "guides/languages/mine/overlay.toml": '["house/x"]\ntext = "X"\n',
        },
    )
    _write(
        tmp_path / "home/.config/auto-se/standards/topics/yours.md",
        "+++\n+++\n",
    )
    loaded = standards.load()
    assert "mine" not in loaded and "yours" not in loaded
    assert [
        a.language for a in standards.select(loaded, ["a.cpp"], root, base)
    ] == [
        "general",
        "cpp",
    ]


def test_languages_activate_by_extension_and_general_always(
    tmp_path: Path,
) -> None:
    active = standards.select(
        standards.load(), ["b.py", "a.hpp", "notes.md", "src/c.cpp"], tmp_path
    )
    assert [a.language for a in active] == ["general", "cpp", "python"]
    assert active[0].reasons == ("always",)
    assert active[1].reasons == (
        "a.hpp ends with .hpp",
        "src/c.cpp ends with .cpp",
    )
    assert active[1].files == ("a.hpp", "src/c.cpp")
    (guide,) = active[2].guides
    assert guide["id"] == "sample-style"
    assert guide["root"] == ".plumbcheck/references/sample-style"
    assert guide["lines"] == [
        "- 1: Naming and structure — 2 rules, 2 checked → "
        "1-naming-and-structure/index.md",
        "- 2: Documentation — 1 rule, 0 checked → 2-documentation/index.md",
    ]
    # The general checkers (secrets) see every changed file.
    assert active[0].files == ("a.hpp", "b.py", "notes.md", "src/c.cpp")
    # A marker activates its language whatever the files.
    (tmp_path / "example.json").write_text("{}")
    marked = standards.select(standards.load(), [], tmp_path)
    assert [a.language for a in marked] == ["general", "example"]


def test_every_loop_sees_general_then_its_languages_by_kind(
    tmp_path: Path,
) -> None:
    active = standards.select(
        standards.load(), ["a.py", "docs/x.rst"], tmp_path
    )
    design = standards.for_loop(active, "architecture_designer")
    assert [g["language"] for g in design] == ["general", "python", "rst"]
    assert [g["key"] for g in design[1]["coding"]] == [
        "sample-style/1.1",
        "sample-style/1.2",
    ]
    assert [g["key"] for g in design[1]["documentation"]] == [
        "python/docstrings"
    ]
    names = design[1]["coding"][0]
    assert names["text"] == _text("sample-style/1.1")
    assert names["enforcement"] == "[checked by ruff N*]"
    assert names["pointer"] == (
        ".plumbcheck/references/sample-style/1-naming-and-structure/"
        "1.1-names.md"
    )
    assert design[2]["guides"] == [] and design[2]["coding"] == []
    assert len(design[2]["documentation"]) == 2
    assert standards.for_loop(active, "test_implementer") == design


def test_lint_names_every_format_error_with_file_and_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = _language(
        _instance(monkeypatch, tmp_path) / "languages" / "bad",
        '["house/ok"]\ntext = "Fine."\n\n'
        '["house/Bad_Id"]\ntext = "T"\ncolour = "red"\n\n'
        '["house/no-text"]\nadvisory = "yes"\n\n'
        '["house/odd-kind"]\ntext = "T"\nkind = "prose"\n\n'
        '["house/refs"]\ntext = "T"\nlink = "https://x.org"\n'
        'checked_by = ["nosuch:X1"]\ndetails = "missing.md"\n\n'
        '["sample-style/1.1"]\n\n'
        '["nowhere"]\ntext = "T"\n',
        '[runner]\nrole = "fixer"\n\n[empty]\nrole = "checker"\n',
        'colour = "red"\n',
    )
    errors = standards.lint(standards.parse(folder, "bad"))
    overlay, tools = folder / "overlay.toml", folder / "tools.toml"
    for where, expected in (
        (folder / "language.toml", "bad: unknown field 'colour'."),
        (folder / "language.toml", "bad: the language needs a title."),
        (folder / "language.toml", "bad: the language needs file extensions."),
        (f"{overlay}:4", "bad/house/Bad_Id: unknown field 'colour'."),
        (f"{overlay}:4", "bad/house/Bad_Id: the id must match"),
        (f"{overlay}:8", "bad/house/no-text: advisory must be a bool."),
        (f"{overlay}:8", "bad/house/no-text: a house rule needs a text."),
        (f"{overlay}:11", "bad/house/odd-kind: kind must be one of"),
        (f"{overlay}:15", "bad/refs: 'nosuch' is not a checker of bad."),
        (
            f"{overlay}:15",
            "bad/refs: details file missing.md does not exist.",
        ),
        (f"{overlay}:21", "'sample-style' is no guide of bad."),
        (f"{overlay}:23", "bad/nowhere: the key must be house/<id>"),
        (f"{tools}:1", "bad/runner: role must be one of"),
        (f"{tools}:4", "checkers are the LINTERS of plumbcheck.yml."),
    ):
        assert any(
            e.startswith(f"{where}:") and expected in e for e in errors
        ), expected
    assert not any("bad/ok" in e for e in errors)


def test_every_overlay_key_names_a_rule_of_the_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    overlay = _instance(monkeypatch, tmp_path) / "languages/python/overlay.toml"
    overlay.write_text(
        overlay.read_text() + '\n["sample-style/9.99"]\npriority = 9\n'
    )
    assert [e for e in standards.lint_all() if "9.99" in e] == [
        f"{overlay}:{overlay.read_text().count(chr(10)) - 1}: "
        "sample-style/9.99: sample-style/9.99 is no rule of sample-style."
    ]


def test_a_toml_syntax_error_names_file_and_line(tmp_path: Path) -> None:
    folder = _language(tmp_path / "x", '[a]\ntext = "A"\n\n[b]\ntext = \n')
    with pytest.raises(ValueError, match=r"overlay\.toml:5: "):
        standards.parse(folder, "x")


def test_the_instance_is_well_formed() -> None:
    assert sorted(standards.load()) == [
        "cpp",
        "example",
        "general",
        "jinja",
        "python",
        "rst",
        "typescript",
    ]
    assert standards.lint_all() == []
    assert standards.source_errors() == []
    assert not standards.coherence(standards.graph())


def test_without_guides_only_general_and_its_checkers_load(
    tmp_path: Path,
) -> None:
    standards.use(tmp_path)
    assert standards.instance() is None
    loaded = standards.load()
    assert list(loaded) == ["general"]
    assert [t.name for t in loaded["general"].tools] == ["gitleaks"]
    assert standards.guides() == {} and standards.lint_all() == []


def test_the_graph_mirrors_the_files_and_reports_coherence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _organization(
        monkeypatch,
        tmp_path,
        "LINTERS:\n  CPP_UNUSED:\n    language: cpp\n    aliases: [unused]\n"
        "    command: 'true'\n",
    )
    instance = _instance(monkeypatch, tmp_path)
    _language(instance / "languages" / "empty", "")
    monkeypatch.setattr(standards, "LOOPS", ["planner", "feature_implementer"])
    graph = standards.graph()
    kinds = Counter(node.type for node in graph.nodes.values())
    assert set(kinds) == {"language", "guideline", "tool", "loop", "metric"}
    names = graph.nodes["guideline:sample-style/1.1"]
    assert names.parent == "language:python"
    assert "#### 1.1.1 Why" in names.data["section"]
    assert names.data["license"] == "CC0-1.0"
    assert names.data["reference"] == (
        "sample-style/1-naming-and-structure/1.1-names.md"
    )
    assert graph.nodes["tool:cpp/clang-tidy"].parent == "language:cpp"
    edges = {(e.source, e.target, e.type) for e in graph.edges}
    assert ("guideline:cpp/owners", "tool:cpp/clang-tidy", "checked_by") in (
        edges
    )
    assert ("metric:cc_max", "loop:feature_implementer", "judges") in edges
    assert ("metric:cc_max", "tool:general/radon", "measured_by") in edges
    assert (
        "guideline:sample-style/1.2",
        "metric:new complexity",
        "judged_by",
    ) in edges
    # An empty ``loops`` means every loop; a planned metric judges nothing yet.
    assert ("metric:cc_mean", "loop:planner", "judges") in edges
    assert not [
        e for e in graph.edges if e.source == "metric:behavior traceability"
    ]
    assert ".hpp" in graph.nodes["language:cpp"].data["extensions"]
    findings = {(f.rule, f.message) for f in standards.coherence(graph)}
    assert findings == {
        ("tools", "checker cpp/unused runs, but no guideline names it."),
        ("guidelines", "language empty has no guidelines."),
    }
    page = standards.graph_page(graph, "target")
    data = json.loads(page.split("const DATA = ", 1)[1].split(";\n", 1)[0])
    (node,) = [
        n for n in data["nodes"] if n["id"] == "guideline:sample-style/1.1"
    ]
    assert {
        "key",
        "language",
        "text",
        "kind",
        "advisory",
        "link",
        "details",
        "checked_by",
        "path",
        "line",
    } <= set(node["data"])
    assert "/*GRAPH*/" not in page
    (instance / "metrics.toml").unlink()
    assert not [
        n for n in standards.graph().nodes.values() if n.type == "metric"
    ]


def test_the_page_opens_on_the_guidelines_table_with_tabs_and_legend() -> None:
    page = standards.graph_page(standards.graph(), "sample")
    assert 'role="tablist"' in page
    assert re.findall(r'role="tab" id="tab-(\w+)"', page) == [
        "guidelines",
        "metrics",
        "graph",
        "loops",
    ]
    prompts = {"python": "python/docstrings"}
    with_prompts = standards.graph_page(standards.graph(), "sample", prompts)
    data = json.loads(
        with_prompts.split("const DATA = ", 1)[1].split(";\n", 1)[0]
    )
    assert data["prompts"] == prompts
    headers = re.findall(
        r'<th scope="col">([^<]+)<', page.split('id="guidelines"')[1]
    )[:7]
    assert headers == [
        "Guideline",
        "Language",
        "Kind",
        "Advisory",
        "Checked by",
        "Sources",
        "Defined at",
    ]
    assert 'id="legend"' in page and "How to read this map" in page
    assert (
        '<details class="howto band" id="howto">' in page
    )  # opened by script once
    assert 'id="filtered" role="status"' in page and 'id="planned"' in page
    for library in ("cytoscape@3.30.2", "elkjs@0.9.3", "cytoscape-elk@2.2.0"):
        assert f"cdn.jsdelivr.net/npm/{library}/" in page


def test_check_and_show(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, _ = _repository(tmp_path, {"parser.hpp": "int f();\n"})
    monkeypatch.setattr(standards.shutil, "which", _installed)
    assert cli.main(["--root", str(root), "check"]) == 0
    assert capsys.readouterr().out.endswith("7 standards, no problems.\n")
    assert cli.main(["--root", str(root), "show"]) == 0
    shown = capsys.readouterr().out
    assert "| general | General | all files | 1 | gitleaks, radon |" in shown
    assert "| cpp | C++ | .cpp .hpp | 1 | clang-tidy |" in shown
    assert (
        cli.main(["--root", str(root), "show", str(root / "parser.hpp")]) == 0
    )
    shown = capsys.readouterr().out
    assert "cpp: parser.hpp ends with .hpp" in shown
    assert "  cpp/owners: Give every owning pointer an owner type." in shown
    page = tmp_path / "map.html"
    assert cli.main(["--root", str(root), "show", "--html", str(page)]) == 0
    assert page.read_text().startswith("<!doctype html>")
    # Without GUIDES there is nothing to check.
    assert cli.main(["--root", str(tmp_path), "check"]) == 0
    assert "no GUIDES" in capsys.readouterr().out


def test_a_metric_names_only_declared_measurers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    metrics = _instance(monkeypatch, tmp_path) / "metrics.toml"
    metrics.write_text(
        metrics.read_text().replace(
            'measured_by = ["radon"]', 'measured_by = ["lizard"]', 1
        )
    )
    assert standards.metric_tool_errors() == [
        "metrics.toml: cc_max is measured by 'lizard', which "
        "general/tools.toml does not declare as a measurer."
    ]


def test_judged_by_names_a_metric(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A guideline's ``judged_by`` must name a metric of ``metrics.toml``."""
    instance = _instance(monkeypatch, tmp_path)
    folder = _language(
        instance / "languages" / "x",
        '["house/short"]\ntext = "Keep it short."\n'
        'judged_by = "new complexity"\n\n'
        '["house/long"]\ntext = "Keep it long."\n'
        'judged_by = "no such metric"\n',
    )
    errors = standards.lint(standards.parse(folder, "x"))
    assert errors == [
        f"{folder / 'overlay.toml'}:5: x/long: "
        "'no such metric' is not a metric."
    ]


COMPLEX = (
    "def branchy(x: int) -> int:\n"
    + "".join(f"    if x == {i}:\n        return {i}\n" for i in range(11))
    + "    return -1\n"
)


def test_types_are_recognised_and_their_base_synced(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("")
    assert cli.types_of(tmp_path) == []
    (tmp_path / "uv.lock").write_text("")
    assert cli.types_of(tmp_path) == ["python-package"]
    # Without a plumbcheck.yml, no base: plumbcheck has no opinions.
    assert cli.sync(tmp_path, check=True) == []
    # The organization's base lands in .plumbcheck/: general and per type.
    _write(tmp_path / config.NAME, f"EXTENDS: {ORG / config.NAME}\n")
    written = [
        ".plumbcheck/.clang-tidy",
        ".plumbcheck/.vale.ini",
        ".plumbcheck/pyrightconfig.json",
        ".plumbcheck/ruff.toml",
    ]
    assert cli.sync(tmp_path, check=True) == written
    assert cli.sync(tmp_path, check=False) == written
    assert cli.sync(tmp_path, check=True) == []
    assert (tmp_path / ".plumbcheck/ruff.toml").read_text() == (
        (ORG / "rules" / "ruff.toml").read_text()
    )
    # Only the folder counts: a stale or changed file differs.
    (tmp_path / ".plumbcheck/old.toml").write_text("")
    (tmp_path / ".plumbcheck/ruff.toml").write_text("line-length = 120\n")
    assert cli.main(["--root", str(tmp_path), "sync", "--check"]) == 1
    assert cli.sync(tmp_path, check=False) == [
        ".plumbcheck/old.toml",
        ".plumbcheck/ruff.toml",
    ]
    assert not (tmp_path / ".plumbcheck/old.toml").exists()
    # The repository's own configurations inherit the base.
    assert cli.inherit(tmp_path) == ["ruff.toml", "pyrightconfig.json"]
    assert (tmp_path / "ruff.toml").read_text() == cli.INHERIT["ruff.toml"]
    assert cli.inherit(tmp_path) == []
    assert cli.sync(tmp_path, check=True) == []
    # plumbcheck knows no Verdog project; an organization may add the type.
    (tmp_path / "project.json").write_text("{}")
    (tmp_path / "CMakeLists.txt").write_text("")
    assert cli.types_of(tmp_path) == ["python-package", "cpp"]


CACHED = (
    b'# <a name="s-r"></a>R: Rules\n\n'
    b'### <a name="r1"></a>R.1: One rule whose title is long enough\n'
)


def _cached_source(instance: Path, data: bytes) -> str:
    """Add a cache-only guide of ``data`` to ``instance``'s sources.toml."""
    listing = f"{hashlib.sha256(data).hexdigest()}  g.md\n".encode()
    entry = (
        '\n[cached-guide]\ntitle = "Cached guide"\nlanguage = "cpp"\n'
        'parser = "core-guidelines"\nstore = "cache"\n'
        'url = "https://example.invalid/{pin}/"\n'
        'link = "https://example.invalid/guide"\npin = "abc"\n'
        'files = ["g.md"]\nlevels = [1, 2, 3]\nlicense = "LicenseRef-Private"\n'
        f'sha256 = "{hashlib.sha256(listing).hexdigest()}"\n'
    )
    manifest = instance / "sources.toml"
    manifest.write_text(manifest.read_text() + entry)
    return entry


def test_a_cached_guide_is_fetched_once_verified_copied_and_never_quoted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = _instance(monkeypatch, tmp_path)
    monkeypatch.setattr(standards, "CACHE", tmp_path / "cache")
    entry = _cached_source(instance, CACHED)
    overlay = instance / "languages/cpp/overlay.toml"
    overlay.write_text(
        overlay.read_text()
        + '\n["cached-guide/R.1"]\nchecked_by = ["clang-tidy"]\npriority = 2\n'
    )
    calls: list[str] = []

    def download(url: str) -> bytes:
        calls.append(url)
        return CACHED

    monkeypatch.setattr(standards.sources, "_download", download)
    source = standards.guides()["cached-guide"]
    folder = standards.tree(source)
    assert folder.is_relative_to(tmp_path / "cache")
    assert (folder / "R/R.1.md").read_text().endswith("enough\n")
    assert standards.tree(source) == folder and len(calls) == 1
    standards.copy_references(tmp_path / "worktree")
    copied = tmp_path / "worktree" / standards.WORKTREE_REFERENCES
    assert (copied / "cached-guide/R/R.1.md").is_file()
    assert not (copied / "cached-guide/source").exists()
    assert (copied / "sample-style/index.md").is_file()
    # Its rules' text stays out of reports and the map.
    (owner,) = [
        g
        for g in standards.load()["cpp"].guidelines
        if g.id == "cached-guide/R.1"
    ]
    line = standards.rule_line(owner, tmp_path)
    assert line.startswith("- cached-guide/R.1: cached-guide/R.1 [")
    assert "long enough" not in line
    node = standards.graph().nodes["guideline:cached-guide/R.1"]
    assert node.data["text"] == "" and node.data["section"] == ""
    # A tracked file that quotes a rule's title fails the check.
    _git(tmp_path, "init", "-q")
    _write(tmp_path / "notes.md", "One rule whose title is long enough\n")
    _git(tmp_path, "add", "notes.md")
    assert standards.committed_cache_errors() == [
        "notes.md: cached-guide allows no public copy; never commit it or "
        "quote its rules."
    ]
    # A copy that is not the pinned one fails.
    shutil.rmtree(folder)
    manifest = instance / "sources.toml"
    pinned = re.search(r'sha256 = "(\w+)"\n$', entry)
    assert pinned is not None
    manifest.write_text(manifest.read_text().replace(pinned[1], "0" * 64))
    with pytest.raises(ValueError, match="not the pinned"):
        standards.tree(standards.guides()["cached-guide"])


def _ran(root: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[int, list[str]]:
    """``run`` in ``root`` with every command recorded instead of run."""
    lines: list[str] = []

    def record(line: str, **_: object) -> subprocess.CompletedProcess[str]:
        lines.append(line)
        return subprocess.CompletedProcess(line, 0)

    monkeypatch.setattr(cli.subprocess, "run", record)
    return cli.run(root), lines


def test_cpp_runs_after_the_python_types_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Native dependencies from wheels exist before CMake configures."""
    for name in ("CMakeLists.txt", "pyproject.toml", "uv.lock"):
        _write(tmp_path / name, "")
    assert cli.types_of(tmp_path) == ["python-package", "cpp"]
    code, lines = _ran(tmp_path, monkeypatch)
    assert code == 0
    assert lines[0].startswith("uv sync")
    cpp = cli.project_types()["cpp"]
    assert lines[-3:] == cpp["setup"] + cpp["commands"]


def test_a_standards_preset_replaces_the_plain_cmake_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The presets run instead; the build must still land in build/standards."""
    _write(tmp_path / "CMakeLists.txt", "")
    presets = {
        kind: [{"name": "standards"}]
        for kind in ("configurePresets", "buildPresets", "testPresets")
    }
    _write(tmp_path / "CMakePresets.json", json.dumps(presets))
    code, lines = _ran(tmp_path, monkeypatch)
    assert lines == list(cli.project_types()["cpp"]["presets"].values())
    assert code == 1  # no build/standards/compile_commands.json
    _write(tmp_path / "build/standards/compile_commands.json", "[]")
    assert _ran(tmp_path, monkeypatch)[0] == 0


def test_siblings_are_the_uv_path_sources_next_to_the_repository(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Sources of the root and of workspace members count, others do not."""
    root = tmp_path / "app"
    _write(
        root / "pyproject.toml",
        '[tool.uv.workspace]\nmembers = ["packages/*"]\n'
        "[tool.uv.sources]\n"
        'lib = { workspace = true }\nruntime = { path = "../runtime" }\n'
        'vendored = { path = "vendor/x" }\n',
    )
    _write(
        root / "packages/lib/pyproject.toml",
        '[tool.uv.sources]\ncli = { path = "../../../cli" }\n',
    )
    assert cli.main(["--root", str(root), "siblings"]) == 0
    assert capsys.readouterr().out == "cli\nruntime\n"


def test_coverage_counts_checked_rules_and_names_unmapped_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _organization(
        monkeypatch,
        tmp_path,
        "LINTERS:\n  PYTHON_LISTER:\n    language: python\n"
        "    aliases: [lister]\n    command: 'true'\n"
        "    list: printf 'N802\\nZ999\\n'\n    link: https://x.org\n",
    )
    python = _instance(monkeypatch, tmp_path) / "languages" / "python"
    overlay = python / "overlay.toml"
    overlay.write_text(
        overlay.read_text().replace(
            'checked_by = ["ruff:N*"]', 'checked_by = ["ruff:N*", "lister:N*"]'
        )
    )
    shown = standards.coverage()
    assert "## Sample style guide (`sample-style`, CC0-1.0)" in shown
    assert "| 1 Naming and structure | 2 | 2 | 0 |" in shown
    assert "| 2 Documentation | 1 | 0 | 1 |" in shown
    assert "- python/lister: Z999 maps to no guideline; see https://x.org" in (
        shown
    )
    assert "N802" not in shown
