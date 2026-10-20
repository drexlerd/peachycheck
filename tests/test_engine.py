"""The engine: languages, guidelines, tools, selection, checks and graph."""

import json
import os
import re
import shutil
import subprocess
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

import pytest

from verdog_standards import cli
from verdog_standards import engine as standards

CLEAN = '''"""A module."""


def add(left: int, right: int) -> int:
    """Add two numbers.

    Args:
        left: The first number.
        right: The second number.

    Returns:
        The sum.
    """
    return left + right
'''
# N802 and D103: a mixedCase name without a docstring breaks both Python rules.
VIOLATION = "\n\ndef mixedCase() -> None:\n    pass\n"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _repository(tmp_path: Path, contents: dict[str, str]) -> tuple[Path, str]:
    root = tmp_path / "repository"
    root.mkdir()
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
    instance = tmp_path / "standards"
    shutil.copytree(standards.STANDARDS, instance)
    monkeypatch.setattr(standards, "STANDARDS", instance)
    return instance


def _installed(tool: str) -> str:
    return f"/usr/bin/{tool}"


def _language(
    folder: Path, guidelines: str, tools: str = "", header: str = ""
) -> Path:
    _write(
        folder / "language.toml", header or 'title = "X"\nextensions = [".x"]\n'
    )
    _write(folder / "guidelines.toml", guidelines)
    _write(folder / "tools.toml", tools)
    return folder


def test_a_guideline_table_carries_its_fields_and_line(tmp_path: Path) -> None:
    folder = _language(
        tmp_path / "cpp",
        '[no-raw-new]\ntext = "Never call new directly."\nadvisory = true\n'
        'cite = "k"\nchecked_by = "clang-tidy"\nkind = "coding"\n\n'
        '[plain]\ntext = "Just advice."\n',
        '[clang-tidy]\nrole = "checker"\nrun = "clang-tidy {files}"\n',
        'title = "C++"\nextensions = [".cpp"]\n',
    )
    language = standards.parse(folder, "cpp")
    first, second = language.guidelines
    assert (first.key, first.advisory, first.cite, first.checked_by) == (
        "cpp/no-raw-new",
        True,
        "k",
        "clang-tidy",
    )
    assert (first.line, second.line) == (1, 8)
    assert (second.kind, second.advisory, second.checked_by) == (
        "coding",
        False,
        "",
    )
    (check,) = language.checkers()
    assert (check.tool, check.guidelines) == ("clang-tidy", ("cpp/no-raw-new",))


def test_only_the_instance_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root, base = _repository(
        tmp_path,
        {
            "a.cpp": "",
            "standards/languages/mine/guidelines.toml": '[x]\ntext = "X"\n',
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
        standards.load(), ["b.py", "a.hpp", "notes.md", "src/c.cc"], tmp_path
    )
    assert [a.language for a in active] == ["general", "cpp", "python"]
    assert active[0].reasons == ("always",)
    assert active[1].reasons == (
        "a.hpp ends with .hpp",
        "src/c.cc ends with .cc",
    )
    assert active[1].files == ("a.hpp", "src/c.cc")
    assert active[1].cite == ("llvm-project-2026", "stroustrup-sutter-2026")
    # The general checkers (secrets) see every changed file.
    assert active[0].files == ("a.hpp", "b.py", "notes.md", "src/c.cc")


def test_every_loop_sees_general_then_its_languages_by_kind(
    tmp_path: Path,
) -> None:
    active = standards.select(
        standards.load(), ["a.cpp", "docs/x.rst"], tmp_path
    )
    design = standards.for_loop(active, "architecture_designer")
    assert [g["language"] for g in design] == ["general", "cpp", "rst"]
    assert [g["key"] for g in design[1]["coding"]] == [
        "cpp/core-guidelines-checks",
        "cpp/ownership-in-types",
        "cpp/typed-interfaces",
        "cpp/raii",
        "cpp/independent-tests",
        "cpp/bug-patterns",
    ]
    assert design[2]["coding"] == [] and len(design[2]["documentation"]) == 16
    assert standards.for_loop(active, "test_implementer") == design


def test_checkers_come_from_the_language_s_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (check,) = standards.select(standards.load(), ["a.cpp"], tmp_path)[1].checks
    assert check.tool == "clang-tidy"
    assert check.guidelines == (
        "cpp/core-guidelines-checks",
        "cpp/ownership-in-types",
        "cpp/raii",
        "cpp/bug-patterns",
    )
    assert check.run == (
        "clang-tidy -p {compile_commands} --checks=-*,cppcoreguidelines-*,"
        "bugprone-*,performance-*,modernize-make-unique --quiet {files}"
    )
    tools = (
        _instance(monkeypatch, tmp_path) / "languages" / "cpp" / "tools.toml"
    )
    tools.write_text(
        tools.read_text().replace(
            "clang-tidy -p {compile_commands} --checks",
            "clang-tidy-18 --checks",
        )
    )
    (check,) = standards.select(standards.load(), ["a.cpp"], tmp_path)[1].checks
    assert check.run.startswith("clang-tidy-18 --checks=")


def test_lint_names_every_format_error_with_file_and_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = _language(
        _instance(monkeypatch, tmp_path) / "languages" / "bad",
        '[ok]\ntext = "Fine."\n\n'
        '[Bad_Id]\ntext = "T"\ncolour = "red"\n\n'
        '[no-text]\nadvisory = "yes"\n\n'
        '[odd-kind]\ntext = "T"\nkind = "prose"\n\n'
        '[refs]\ntext = "T"\ncite = "no-such-key"\nchecked_by = "nosuch"\n'
        'good = "examples/missing.x"\n',
        '[runner]\nrole = "fixer"\n\n[empty]\nrole = "checker"\n',
        'colour = "red"\n',
    )
    errors = standards.lint(standards.parse(folder, "bad"))
    guidelines, tools = folder / "guidelines.toml", folder / "tools.toml"
    for where, expected in (
        (folder / "language.toml", "bad: unknown field 'colour'."),
        (folder / "language.toml", "bad: the language needs a title."),
        (folder / "language.toml", "bad: the language needs file extensions."),
        (f"{guidelines}:4", "bad/Bad_Id: unknown field 'colour'."),
        (f"{guidelines}:4", "bad/Bad_Id: the id must match"),
        (f"{guidelines}:8", "bad/no-text: advisory must be a bool."),
        (f"{guidelines}:8", "bad/no-text: a guideline needs a text."),
        (f"{guidelines}:11", "bad/odd-kind: kind must be one of"),
        (f"{guidelines}:15", "bad/refs: unknown cite key 'no-such-key'."),
        (f"{guidelines}:15", "bad/refs: 'nosuch' is not a checker of bad."),
        (
            f"{guidelines}:15",
            "bad/refs: good file examples/missing.x does not exist.",
        ),
        (f"{tools}:1", "bad/runner: role must be one of"),
        (f"{tools}:4", "bad/empty: a checker needs a run command."),
    ):
        assert any(
            e.startswith(f"{where}:") and expected in e for e in errors
        ), expected
    assert not any("bad/ok" in e for e in errors)


def test_a_toml_syntax_error_names_file_and_line(tmp_path: Path) -> None:
    folder = _language(tmp_path / "x", '[a]\ntext = "A"\n\n[b]\ntext = \n')
    with pytest.raises(ValueError, match=r"guidelines\.toml:5: "):
        standards.parse(folder, "x")


def test_the_instance_is_well_formed() -> None:
    assert sorted(standards.load()) == [
        "cpp",
        "general",
        "python",
        "rst",
        "typescript",
        "verdog",
    ]
    assert standards.lint_all() == []


def test_new_python_files_must_follow_the_google_style(tmp_path: Path) -> None:
    # verifies: style.google_python
    root, base = _repository(tmp_path, {"legacy.py": CLEAN})
    (root / "clean.py").write_text(CLEAN)
    assert standards.changed_errors(root, base) == []
    (root / "new.py").write_text(CLEAN + VIOLATION)
    assert standards.changed_errors(root, base) == [
        "new.py gains ruff findings (0 -> 1); follow the Python guidelines it "
        "checks: python/names, python/bug-patterns.",
        "new.py gains ruff-docstrings findings (0 -> 1); follow the Python "
        "guidelines it checks: python/docstrings.",
    ]


def test_changed_files_may_keep_but_not_add_legacy_violations(
    tmp_path: Path,
) -> None:
    # verifies: style.google_python
    root, base = _repository(tmp_path, {"legacy.py": CLEAN + VIOLATION})
    (root / "legacy.py").write_text(CLEAN + VIOLATION + "\n\nVALUE = 1\n")
    assert standards.changed_errors(root, base) == []
    (root / "legacy.py").write_text(
        CLEAN + VIOLATION + VIOLATION.replace("mixedCase", "otherCase")
    )
    # ruff and ruff-docstrings each count one more finding.
    assert len(standards.changed_errors(root, base)) == 2


def test_example_files_of_the_instance_are_not_style_checked(
    tmp_path: Path,
) -> None:
    """Examples violate their guideline on purpose; checks skip them."""
    root, base = _repository(tmp_path, {"legacy.py": CLEAN})
    _write(
        root / "standards" / "languages" / "python" / "examples" / "x_bad.py",
        VIOLATION,
    )
    assert standards.changed_errors(root, base) == []


def test_generated_files_keep_the_compilers_style(tmp_path: Path) -> None:
    root, base = _repository(
        tmp_path,
        {
            "project.json": json.dumps(
                {"sources": [{"path": "gen.py", "ownership": "generated"}]}
            )
        },
    )
    (root / "gen.py").write_text(VIOLATION)
    assert standards.changed_errors(root, base) == []


def test_typescript_follows_the_same_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # verifies: style.google_typescript
    root, base = _repository(tmp_path, {"old.ts": "let a = 1;\n"})
    (root / "old.ts").write_text("let a = 1;\nvar b = 2;\n")
    (root / "new.ts").write_text("var c = 3;\n")

    def count(
        check: standards.Check,
        title: str,
        directory: Path,
        paths: Sequence[str],
        build: Path,
    ) -> Counter[str]:
        if check.tool != "eslint":
            return Counter()
        return Counter(
            {
                f: (directory / f).read_text().count("var ")
                for f in paths
                if (directory / f).is_file()
            }
        )

    monkeypatch.setattr(standards, "count", count)
    assert standards.style_errors(root, base, ["old.ts", "new.ts"]) == [
        "new.ts gains eslint findings (0 -> 1); follow the TypeScript "
        "guidelines it checks: typescript/gts-style.",
        "old.ts gains eslint findings (0 -> 1); follow the TypeScript "
        "guidelines it checks: typescript/gts-style.",
    ]


def _clang_tidy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    script = (
        'for f in "$@"; do case "$f" in *.cpp) '
        'grep -n "new" "$f" | sed "s|^\\([0-9]*\\):.*|'
        "$f:\\1:1: warning: owning [cppcoreguidelines-owning-memory]"
        '\\n$f:\\1:1: note: here|";; esac; done'
    )
    tool = _write(tmp_path / "bin" / "clang-tidy", f"#!/bin/sh\n{script}\n")
    tool.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tool.parent}{os.pathsep}{os.environ['PATH']}")


def test_cpp_changes_follow_the_core_guidelines_through_clang_tidy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # verifies: style.cpp_core
    root, base = _repository(
        tmp_path, {"a.cpp": "int f();\n", "build/compile_commands.json": "[]"}
    )
    (root / "a.cpp").write_text("int f();\nint* g = new int;\n")
    _clang_tidy(tmp_path, monkeypatch)
    assert standards.style_errors(root, base, ["a.cpp"]) == [
        "a.cpp gains clang-tidy findings (0 -> 1); follow the C++ guidelines "
        "it checks: cpp/core-guidelines-checks, cpp/ownership-in-types, "
        "cpp/raii, cpp/bug-patterns."
    ]


def test_a_missing_tool_fails_instead_of_passing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, base = _repository(tmp_path, {"a.cpp": "int f() { return 1; }\n"})
    (root / "a.cpp").write_text("int f() { return 2; }\n")
    which = standards.shutil.which

    def without_clang_tidy(tool: str, path: str | None = None) -> str | None:
        return None if tool == "clang-tidy" else which(tool, path=path)

    monkeypatch.setattr(standards.shutil, "which", without_clang_tidy)
    assert standards.style_errors(root, base, ["a.cpp"]) == [
        "C++ needs clang-tidy; install it with sudo apt install clang-tidy."
    ]


def test_example_tests_confirm_the_tool_flags_the_bad_example(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cpp = _instance(monkeypatch, tmp_path) / "languages" / "cpp"
    _clang_tidy(tmp_path, monkeypatch)
    _write(cpp / "build" / "compile_commands.json", "[]")
    _write(cpp / "examples" / "good.cpp", "int f();\n")
    _write(cpp / "examples" / "bad.cpp", "int* g = new int;\n")
    (cpp / "guidelines.toml").write_text(
        '[owning]\ntext = "No owning new."\nchecked_by = "clang-tidy"\n'
        'good = "examples/good.cpp"\nbad = "examples/bad.cpp"\n\n'
        '[inverted]\ntext = "Wrong way round."\nchecked_by = "clang-tidy"\n'
        'good = "examples/bad.cpp"\nbad = "examples/good.cpp"\n'
    )
    assert standards.example_tests({"cpp": standards.load()["cpp"]}) == [
        "cpp/inverted: clang-tidy does not flag examples/good.cpp.",
        "cpp/inverted: clang-tidy flags examples/bad.cpp.",
    ]


def test_the_graph_mirrors_the_files_and_reports_coherence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = _instance(monkeypatch, tmp_path)
    tools = instance / "languages" / "cpp" / "tools.toml"
    tools.write_text(
        tools.read_text() + '\n[unused]\nrole = "checker"\nrun = "true"\n'
    )
    _language(instance / "languages" / "empty", "")
    monkeypatch.setattr(standards, "LOOPS", ["planner", "feature_implementer"])
    graph = standards.graph()
    kinds = Counter(node.type for node in graph.nodes.values())
    assert set(kinds) == {
        "language",
        "guideline",
        "tool",
        "loop",
        "metric",
        "source",
    }
    raii = graph.nodes["guideline:cpp/raii"]
    assert raii.parent == "language:cpp"
    assert graph.nodes["tool:cpp/clang-tidy"].parent == "language:cpp"
    edges = {(e.source, e.target, e.type) for e in graph.edges}
    assert (
        "guideline:cpp/core-guidelines-checks",
        "tool:cpp/clang-tidy",
        "checked_by",
    ) in edges
    assert (
        "guideline:cpp/raii",
        "source:stroustrup-sutter-2026",
        "cites",
    ) in edges
    assert ("metric:cc_max", "loop:feature_implementer", "judges") in edges
    assert ("metric:cc_max", "source:mccabe-tse1976", "cites") in edges
    assert ("metric:cc_max", "tool:general/radon", "measured_by") in edges
    assert ("tool:cpp/clang-tidy", "source:llvm-project-2026", "cites") in edges
    assert (
        "guideline:python/small-functions",
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
    (node,) = [n for n in data["nodes"] if n["id"] == "guideline:cpp/raii"]
    assert {
        "key",
        "language",
        "text",
        "kind",
        "advisory",
        "link",
        "cite",
        "good",
        "bad",
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
    page = standards.graph_page(standards.graph(), "auto-se")
    assert 'role="tablist"' in page
    assert re.findall(r'role="tab" id="tab-(\w+)"', page) == [
        "guidelines",
        "metrics",
        "graph",
        "loops",
    ]
    prompts = {"python": "python/docstrings"}
    with_prompts = standards.graph_page(standards.graph(), "auto-se", prompts)
    data = json.loads(
        with_prompts.split("const DATA = ", 1)[1].split(";\n", 1)[0]
    )
    assert data["prompts"] == prompts
    headers = re.findall(
        r'<th scope="col">([^<]+)<', page.split('id="guidelines"')[1]
    )[:8]
    assert headers == [
        "Guideline",
        "Language",
        "Kind",
        "Advisory",
        "Checked by",
        "Sources",
        "Examples",
        "Defined at",
    ]
    assert 'id="legend"' in page and "How to read this map" in page
    assert (
        '<details class="howto band" id="howto">' in page
    )  # opened by script once
    assert 'id="filtered" role="status"' in page and 'id="planned"' in page
    for library in ("cytoscape@3.30.2", "elkjs@0.9.3", "cytoscape-elk@2.2.0"):
        assert f"cdn.jsdelivr.net/npm/{library}/" in page


def test_only_the_instance_s_literature_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = _instance(monkeypatch, tmp_path)
    folder = _language(
        instance / "languages" / "x", '[a]\ntext = "A"\ncite = "found-2026"\n'
    )
    before = standards.graph_page(standards.graph(), "t")
    found = "@misc{found-2026,\n  author = {A},\n  title = {T},\n"
    found += "  year = {2026}\n}\n"
    _write(tmp_path / "discovered" / "literature" / "discovered.bib", found)
    assert standards.graph_page(standards.graph(), "t") == before
    assert any(
        "unknown cite key 'found-2026'" in e for e in standards.lint_all()
    )
    bib = instance / "literature.bib"
    bib.write_text(bib.read_text() + "\n" + found)
    assert not any(
        "found-2026" in e for e in standards.lint(standards.parse(folder, "x"))
    )
    # The map and its checks never read the loops' records.
    for module in (Path(standards.__file__), standards.GRAPH_PAGE):
        assert "discovered" not in module.read_text()


def test_check_and_show(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, _ = _repository(tmp_path, {"parser.hpp": "int f();\n"})
    monkeypatch.setattr(standards.shutil, "which", _installed)
    assert cli.main(["--root", str(root), "check"]) == 0
    assert capsys.readouterr().out.endswith("6 standards, no problems.\n")
    assert cli.main(["--root", str(root), "show"]) == 0
    shown = capsys.readouterr().out
    assert (
        "| general | General | all files | 2 | gitleaks, radon, coverage, "
        "pytest, ruff, pyright, verdog |"
    ) in shown
    assert (
        "| cpp | C++ | .cpp .cc .cxx .hpp .hh .h .tpp .ipp | 6 | clang-tidy |"
    ) in shown
    assert (
        cli.main(["--root", str(root), "show", str(root / "parser.hpp")]) == 0
    )
    shown = capsys.readouterr().out
    assert "cpp: parser.hpp ends with .hpp" in shown
    assert "  cpp/raii: Prefer RAII" in shown
    page = tmp_path / "map.html"
    assert cli.main(["--root", str(root), "show", "--html", str(page)]) == 0
    assert page.read_text().startswith("<!doctype html>")


def test_a_finding_names_one_path_after_a_preamble() -> None:
    output = (
        "Scanning...\nValidating...\ndocs/a.rst:1: D002 Trailing whitespace\n"
    )
    assert [m["path"] for m in standards.FINDING.finditer(output)] == [
        "docs/a.rst"
    ]


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
        "metrics.toml: cc_over_10 is measured by 'lizard', which "
        "general/tools.toml does not declare as a measurer."
    ]


def test_check_branch_runs_the_gate_check_on_hand_made_changes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``check-branch`` reports and fails on new findings only."""
    root, base = _repository(tmp_path, {"legacy.py": CLEAN})
    (root / "clean.py").write_text(CLEAN)
    arguments = ["--root", str(root), "check-branch", "--base", base]
    assert cli.main(arguments) == 0
    assert capsys.readouterr().out == "no new findings\n"
    (root / "new.py").write_text(CLEAN + VIOLATION)
    assert cli.main(arguments) == 1
    assert "new.py gains ruff findings (0 -> 1)" in capsys.readouterr().out


def test_judged_by_names_a_metric(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A guideline's ``judged_by`` must name a metric of ``metrics.toml``."""
    instance = _instance(monkeypatch, tmp_path)
    folder = _language(
        instance / "languages" / "x",
        '[short]\ntext = "Keep it short."\njudged_by = "new complexity"\n\n'
        '[long]\ntext = "Keep it long."\njudged_by = "no such metric"\n',
    )
    errors = standards.lint(standards.parse(folder, "x"))
    assert errors == [
        f"{folder / 'guidelines.toml'}:5: x/long: "
        "'no such metric' is not a metric."
    ]


def test_a_guide_splits_into_sections_by_anchor_and_heading(
    tmp_path: Path,
) -> None:
    """Markdown, HTML and reST guides answer to anchors, ids and title slugs."""
    markdown = tmp_path / "guide.md"
    markdown.write_text(
        '# Guide\n\n### <a name="r1"></a>R.1: First\nOne.\n'
        "#### Detail\nMore.\n### Second {#two}\nTwo.\n"
    )
    found = standards.sections(markdown)
    assert (
        found["r1"]
        == '### <a name="r1"></a>R.1: First\nOne.\n#### Detail\nMore.'
    )
    assert found["two"].startswith("### Second") and found["guide"].startswith(
        "#"
    )
    html = tmp_path / "guide.html"
    html.write_text(
        '<h2 id="naming">Naming</h2>\n<p>Names.</p>\n<h2 id="x">X</h2>\n'
    )
    assert (
        standards.sections(html)["naming"]
        == '<h2 id="naming">Naming</h2>\n<p>Names.</p>'
    )
    rst = tmp_path / "guide.rst"
    rst.write_text(".. _kinds:\n\nKinds\n=====\n\nText.\n\nSub\n---\n\nMore.\n")
    assert standards.sections(rst)["kinds"].endswith("More.")
    assert standards.sections(rst)["sub"] == "Sub\n---\n\nMore."


def test_a_reference_must_name_a_section_of_a_pinned_guide(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``standards check`` reports a reference whose section does not exist."""
    guidelines = (
        _instance(monkeypatch, tmp_path)
        / "languages"
        / "cpp"
        / "guidelines.toml"
    )
    guidelines.write_text(
        guidelines.read_text().replace("#rr-raii", "#rr-nowhere", 1)
    )
    assert any(
        "cpp/raii: reference references/CppCoreGuidelines.md#rr-nowhere"
        in error
        for error in standards.lint_all()
    )


COMPLEX = (
    "def branchy(x: int) -> int:\n"
    + "".join(f"    if x == {i}:\n        return {i}\n" for i in range(11))
    + "    return -1\n"
)


def test_no_new_or_changed_function_above_the_complexity_limit(
    tmp_path: Path,
) -> None:
    root, base = _repository(tmp_path, {"old.py": COMPLEX})
    (root / "old.py").write_text(COMPLEX + "\n\nVALUE = 1\n")
    assert standards.complexity_errors(root, base, ["old.py"]) == []
    # Reformatting a function does not change it.
    (root / "old.py").write_text(COMPLEX.replace("return 1\n", "return (1)\n"))
    assert standards.complexity_errors(root, base, ["old.py"]) == []
    (root / "old.py").write_text(COMPLEX.replace("return -1", "return -2"))
    (root / "new.py").write_text(COMPLEX)
    (root / "tests").mkdir()
    (root / "tests" / "test_new.py").write_text(COMPLEX)
    changed = ["new.py", "old.py", "tests/test_new.py"]
    assert standards.complexity_errors(root, base, changed) == [
        "new.py: branchy has cyclomatic complexity 12 > 10; "
        "split it into smaller functions.",
        "old.py: branchy has cyclomatic complexity 12 > 10; "
        "split it into smaller functions.",
    ]


def test_docstrings_are_not_required_in_tests(tmp_path: Path) -> None:
    root, base = _repository(tmp_path, {"tests/test_a.py": CLEAN})
    (root / "tests" / "test_a.py").write_text(
        CLEAN + "\n\ndef test_b() -> None:\n    pass\n"
    )
    assert standards.changed_errors(root, base) == []


def test_types_are_recognised_and_their_configs_synced(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("")
    assert cli.types_of(tmp_path) == []
    (tmp_path / "uv.lock").write_text("")
    assert cli.types_of(tmp_path) == ["python-package"]
    assert cli.sync(tmp_path, check=True) == ["pyrightconfig.json", "ruff.toml"]
    assert cli.sync(tmp_path, check=False) == [
        "pyrightconfig.json",
        "ruff.toml",
    ]
    assert cli.sync(tmp_path, check=True) == []
    (tmp_path / "ruff.toml").write_text("line-length = 120\n")
    assert cli.main(["--root", str(tmp_path), "sync", "--check"]) == 1
    # A Verdog project generates its pyproject.toml: no plain package.
    (tmp_path / "project.json").write_text("{}")
    (tmp_path / "CMakeLists.txt").write_text("")
    assert cli.types_of(tmp_path) == ["cpp", "verdog-project"]


def test_a_pinned_guide_is_fetched_once_and_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = _instance(monkeypatch, tmp_path)
    monkeypatch.setattr(standards, "CACHE", tmp_path / "cache")
    cpp = standards.load()["cpp"]
    pinned = (instance / "languages/cpp/references/downloads.toml").read_text()
    data = b"# Guide\n"
    digest = __import__("hashlib").sha256(data).hexdigest()
    (instance / "languages/cpp/references/downloads.toml").write_text(
        re.sub(r'sha256 = "\w+"', f'sha256 = "{digest}"', pinned)
    )
    calls: list[str] = []

    class Response:
        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_: object) -> None:
            pass

        def read(self) -> bytes:
            return data

    def urlopen(url: str, timeout: int) -> Response:
        calls.append(url)
        return Response()

    monkeypatch.setattr(standards.urllib.request, "urlopen", urlopen)
    file = "references/CppCoreGuidelines.md"
    first = standards.guide(cpp, file)
    assert first is not None and first.read_bytes() == data
    assert standards.guide(cpp, file) == first and len(calls) == 1
    first.unlink()
    (instance / "languages/cpp/references/downloads.toml").write_text(pinned)
    with pytest.raises(ValueError, match="sha256"):
        standards.guide(cpp, file)
