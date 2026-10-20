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


def _title(rule: str) -> str:
    """A cached rule's title: the Core Guidelines may not be quoted here."""
    entry = standards.rule(rule)
    assert entry is not None
    return entry.title


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
        'advisory = true\ncite = "k"\nchecked_by = ["clang-tidy"]\n'
        'kind = "coding"\n\n'
        '["cpp-core-guidelines/R.20"]\n'
        'checked_by = ["clang-tidy:cppcoreguidelines-owning-memory"]\n'
        "priority = 1\n",
        '[clang-tidy]\nrole = "checker"\nrun = "clang-tidy {files}"\n',
        'title = "C++"\nextensions = [".cpp"]\n',
    )
    language = standards.parse(folder, "cpp")
    house, derived = language.guidelines
    assert (house.key, house.advisory, house.cite, house.tools) == (
        "cpp/no-raw-new",
        True,
        "k",
        ("clang-tidy",),
    )
    assert (house.line, derived.line) == (1, 8)
    assert (derived.key, derived.source, derived.priority) == (
        "cpp-core-guidelines/R.20",
        "cpp-core-guidelines",
        1,
    )
    assert derived.text == "" and derived.rule == "cpp-core-guidelines/R.20"
    (check,) = language.checkers()
    assert (check.tool, check.guidelines) == (
        "clang-tidy",
        ("cpp/no-raw-new", "cpp-core-guidelines/R.20"),
    )


def test_only_the_instance_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root, base = _repository(
        tmp_path,
        {
            "a.cpp": "",
            "standards/languages/mine/overlay.toml": '["house/x"]\ntext = "X"\n',
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
    (guide,) = active[1].guides
    assert guide["id"] == "cpp-core-guidelines"
    assert guide["root"] == ".verdog-standards/references/cpp-core-guidelines"
    assert (
        "- R: Resource management — 25 rules, 3 checked → R/index.md"
        in (guide["lines"])
    )
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
        "cpp-core-guidelines/R.20",
        "cpp-core-guidelines/R.1",
        "cpp-core-guidelines/F.7",
        "cpp-core-guidelines/I.4",
        "cpp-core-guidelines/F.16",
        "cpp-core-guidelines/R.10",
        "cpp-core-guidelines/ES.49",
        "cpp-core-guidelines/ES.20",
        "cpp-core-guidelines/ES.42",
        "cpp/bug-patterns",
        "cpp/independent-tests",
    ]
    owner = design[1]["coding"][0]
    # The rule's title as the cached index has it; never quoted here.
    title = _title("cpp-core-guidelines/R.20")
    assert owner["text"] == title and title.startswith("R.20: ")
    assert owner["enforcement"] == (
        "[checked by clang-tidy cppcoreguidelines-owning-memory]"
    )
    assert owner["pointer"] == (
        ".verdog-standards/references/cpp-core-guidelines/R/smart/R.20.md"
    )
    # About 30 section lines of the guide, and no rule's full text.
    lines = design[1]["guides"][0]["lines"]
    assert 25 <= len(lines) <= 35
    assert not any("##### Reason" in line for line in lines)
    assert [g["id"] for g in design[2]["guides"]] == [
        "google-developer-style",
        "diataxis",
    ]
    assert design[2]["coding"] == [] and len(design[2]["documentation"]) == 16
    assert standards.for_loop(active, "test_implementer") == design


def test_checkers_come_from_the_language_s_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (check,) = standards.select(standards.load(), ["a.cpp"], tmp_path)[1].checks
    assert check.tool == "clang-tidy"
    assert "cpp-core-guidelines/R.20" in check.guidelines
    assert "cpp/bug-patterns" in check.guidelines
    assert "cpp-core-guidelines/R.1" not in check.guidelines
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
        '["house/ok"]\ntext = "Fine."\n\n'
        '["house/Bad_Id"]\ntext = "T"\ncolour = "red"\n\n'
        '["house/no-text"]\nadvisory = "yes"\n\n'
        '["house/odd-kind"]\ntext = "T"\nkind = "prose"\n\n'
        '["house/refs"]\ntext = "T"\ncite = "no-such-key"\n'
        'checked_by = ["nosuch:X1"]\ngood = "examples/missing.x"\n\n'
        '["google-python/3.16"]\n\n'
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
        (f"{overlay}:15", "bad/refs: unknown cite key 'no-such-key'."),
        (f"{overlay}:15", "bad/refs: 'nosuch' is not a checker of bad."),
        (
            f"{overlay}:15",
            "bad/refs: good file examples/missing.x does not exist.",
        ),
        (f"{overlay}:21", "'google-python' is no guide of bad."),
        (f"{overlay}:23", "bad/nowhere: the key must be house/<id>"),
        (f"{tools}:1", "bad/runner: role must be one of"),
        (f"{tools}:4", "bad/empty: a checker needs a run command."),
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
        overlay.read_text() + '\n["google-python/9.99"]\npriority = 9\n'
    )
    assert [e for e in standards.lint_all() if "9.99" in e] == [
        f"{overlay}:{overlay.read_text().count(chr(10)) - 1}: "
        "google-python/9.99: google-python/9.99 is no rule of google-python."
    ]


def test_a_toml_syntax_error_names_file_and_line(tmp_path: Path) -> None:
    folder = _language(tmp_path / "x", '[a]\ntext = "A"\n\n[b]\ntext = \n')
    with pytest.raises(ValueError, match=r"overlay\.toml:5: "):
        standards.parse(folder, "x")


def test_the_instance_is_well_formed() -> None:
    assert sorted(standards.load()) == [
        "cpp",
        "general",
        "jinja",
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
    rules = standards.STANDARDS / "languages/python/references/google-python"
    style = rules / "3-python-style-rules"
    assert standards.changed_errors(root, base) == [
        "new.py gains ruff findings (0 -> 1); fix each new one and follow the "
        "guideline it breaks:\n"
        "  new.py:17: N802 Function name `mixedCase` should be lowercase\n"
        "    - google-python/3.16: Name modules, functions and variables in "
        "`snake_case` and classes in `CapWords`. [checked by ruff N*] → "
        f"{style / '3.16-naming.md'}",
        "new.py gains ruff-docstrings findings (0 -> 1); fix each new one and "
        "follow the guideline it breaks:\n"
        "  new.py:17: D103 Missing docstring in public function\n"
        "    - google-python/3.8: 3.8 Comments and Docstrings: Be sure to use "
        "the right style for module, function, method docstrings and inline "
        "comments. [checked by ruff-docstrings D*] → "
        f"{style / '3.8-comments-and-docstrings.md'}",
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

    def findings(
        check: standards.Check,
        title: str,
        directory: Path,
        paths: Sequence[str],
        build: Path,
    ) -> list[standards.Violation]:
        if check.tool != "eslint":
            return []
        message = "Unexpected var, use let or const instead. [Error/no-var]"
        return [
            standards.Violation(f, i, "no-var", message)
            for f in paths
            if (directory / f).is_file()
            for i, line in enumerate(
                (directory / f).read_text().splitlines(), 1
            )
            if line.startswith("var ")
        ]

    monkeypatch.setattr(standards, "findings", findings)
    errors = standards.style_errors(root, base, ["old.ts", "new.ts"])
    assert [e.splitlines()[:2] for e in errors] == [
        [
            "new.ts gains eslint findings (0 -> 1); fix each new one and "
            "follow the guideline it breaks:",
            "  new.ts:1: Unexpected var, use let or const instead. "
            "[Error/no-var]",
        ],
        [
            "old.ts gains eslint findings (0 -> 1); fix each new one and "
            "follow the guideline it breaks:",
            "  old.ts:2: Unexpected var, use let or const instead. "
            "[Error/no-var]",
        ],
    ]
    # One check maps to every guideline that names it.
    assert [line.split(":")[0] for line in errors[0].splitlines()[2:]] == [
        "    - typescript/gts-style",
        "    - google-typescript/local-variable-declarations",
    ]


def _clang_tidy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    check: str = "cppcoreguidelines-owning-memory",
) -> None:
    script = (
        'for f in "$@"; do case "$f" in *.cpp) '
        'grep -n "new" "$f" | sed "s|^\\([0-9]*\\):.*|'
        f"$f:\\1:1: warning: owning [{check}]"
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
    (error,) = standards.style_errors(root, base, ["a.cpp"])
    tree = standards.tree(standards.guides()["cpp-core-guidelines"])
    head, finding, *rules = error.splitlines()
    assert head == (
        "a.cpp gains clang-tidy findings (0 -> 1); fix each new one and "
        "follow the guideline it breaks:"
    )
    assert finding == (
        "  a.cpp:2: warning: owning [cppcoreguidelines-owning-memory]"
    )
    # owning-memory maps to I.11 and R.20, each with its text and leaf.
    assert [r.split(":")[0] for r in rules] == [
        "    - cpp-core-guidelines/R.20",
        "    - cpp-core-guidelines/I.11",
    ]
    assert rules[0].endswith(
        f"{_title('cpp-core-guidelines/R.20')} "
        "[checked by clang-tidy cppcoreguidelines-owning-memory] → "
        f"{tree / 'R/smart/R.20.md'}"
    )
    # In a worktree with the copied trees, the leaf is the copy's.
    standards.copy_references(root)
    (error,) = standards.style_errors(root, base, ["a.cpp"])
    assert error.splitlines()[2].endswith(
        f"→ {standards.WORKTREE_REFERENCES}/cpp-core-guidelines/R/smart/R.20.md"
    )


def test_an_unmapped_check_names_the_tool_s_documentation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, base = _repository(
        tmp_path, {"a.cpp": "int f();\n", "build/compile_commands.json": "[]"}
    )
    (root / "a.cpp").write_text("int f();\nint* g = new int;\n")
    _clang_tidy(tmp_path, monkeypatch, "misc-no-such-check")
    (error,) = standards.style_errors(root, base, ["a.cpp"])
    assert error.splitlines()[2] == (
        "    - no guideline maps clang-tidy misc-no-such-check; see "
        "https://clang.llvm.org/extra/clang-tidy/"
    )


def test_a_finding_is_new_by_file_check_and_message_not_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, base = _repository(
        tmp_path,
        {"a.cpp": "int* g = new int;\n", "build/compile_commands.json": "[]"},
    )
    _clang_tidy(tmp_path, monkeypatch)
    (root / "a.cpp").write_text("int f();\n\nint* g = new int;\n")
    assert standards.style_errors(root, base, ["a.cpp"]) == []
    (root / "a.cpp").write_text("int* g = new int;\nint* h = new int;\n")
    (error,) = standards.style_errors(root, base, ["a.cpp"])
    assert error.splitlines()[:2] == [
        "a.cpp gains clang-tidy findings (1 -> 2); fix each new one and "
        "follow the guideline it breaks:",
        "  a.cpp:2: warning: owning [cppcoreguidelines-owning-memory]",
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
    (cpp / "overlay.toml").write_text(
        '["house/owning"]\ntext = "No owning new."\nchecked_by = ["clang-tidy"]\n'
        'good = "examples/good.cpp"\nbad = "examples/bad.cpp"\n\n'
        '["house/inverted"]\ntext = "Wrong way round."\n'
        'checked_by = ["clang-tidy"]\n'
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
    raii = graph.nodes["guideline:cpp-core-guidelines/R.1"]
    assert raii.parent == "language:cpp"
    # A cache-only guide's text never reaches the map; a committed one's does.
    assert raii.data["text"] == "" and _title(raii.data["key"]) not in (
        standards.graph_page(graph, "t")
    )
    assert raii.data["section"] == "" and raii.data["reference"] == (
        "cpp-core-guidelines/R/R.1.md"
    )
    names = graph.nodes["guideline:google-python/3.16"]
    assert "#### 3.16.1 Names to Avoid" in names.data["section"]
    assert names.data["license"] == "CC-BY-3.0"
    assert graph.nodes["tool:cpp/clang-tidy"].parent == "language:cpp"
    edges = {(e.source, e.target, e.type) for e in graph.edges}
    assert (
        "guideline:cpp-core-guidelines/R.20",
        "tool:cpp/clang-tidy",
        "checked_by",
    ) in edges
    assert (
        "guideline:cpp-core-guidelines/R.1",
        "source:stroustrup-sutter-2026",
        "cites",
    ) in edges
    assert ("metric:cc_max", "loop:feature_implementer", "judges") in edges
    assert ("metric:cc_max", "source:mccabe-tse1976", "cites") in edges
    assert ("metric:cc_max", "tool:general/radon", "measured_by") in edges
    assert ("tool:cpp/clang-tidy", "source:llvm-project-2026", "cites") in edges
    assert (
        "guideline:google-python/3.18",
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
        n
        for n in data["nodes"]
        if n["id"] == "guideline:cpp-core-guidelines/R.1"
    ]
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
        instance / "languages" / "x",
        '["house/a"]\ntext = "A"\ncite = "found-2026"\n',
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
    assert capsys.readouterr().out.endswith("7 standards, no problems.\n")
    assert cli.main(["--root", str(root), "show"]) == 0
    shown = capsys.readouterr().out
    assert (
        "| general | General | all files | 2 | gitleaks, radon, coverage, "
        "pytest, ruff, pyright, verdog |"
    ) in shown
    assert (
        "| cpp | C++ | .cpp .cc .cxx .hpp .hh .h .tpp .ipp | 53 | clang-tidy |"
    ) in shown
    assert (
        cli.main(["--root", str(root), "show", str(root / "parser.hpp")]) == 0
    )
    shown = capsys.readouterr().out
    assert "cpp: parser.hpp ends with .hpp" in shown
    assert (
        f"  cpp-core-guidelines/R.1: {_title('cpp-core-guidelines/R.1')}"
        in (shown)
    )
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


def test_no_new_or_changed_function_above_the_complexity_limit(
    tmp_path: Path,
) -> None:
    root, base = _repository(tmp_path, {"old.py": COMPLEX})
    (root / "old.py").write_text(COMPLEX + "\n\nVALUE = 1\n")
    assert standards.complexity_errors(root, base, ["old.py"]) == []
    # Reformatting or documenting a function does not change it.
    reformatted = COMPLEX.replace("return 1\n", "return (1)\n")
    documented = reformatted.replace(":\n", ':\n    """Branch."""\n', 1)
    documented += 'def split(x: int) -> str:\n    return f"a {x} b" f"c"\n'
    (root / "old.py").write_text(
        documented.replace('f"a {x} b" f"c"', 'f"a {x} bc"')
    )
    _git(root, "commit", "-qam", "split")
    base = _git(root, "rev-parse", "HEAD").strip()
    (root / "old.py").write_text(documented)
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
    assert cli.types_of(tmp_path) == ["verdog-project", "cpp"]


def test_a_cached_guide_is_fetched_once_verified_and_copied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = _instance(monkeypatch, tmp_path)
    monkeypatch.setattr(standards, "CACHE", tmp_path / "cache")
    data = b'# <a name="s-r"></a>R: Rules\n\n### <a name="r1"></a>R.1: One\n'
    manifest = instance / "sources.toml"
    pinned = manifest.read_text()
    digest = __import__("hashlib").sha256(data).hexdigest()
    listing = f"{digest}  CppCoreGuidelines.md\n".encode()
    manifest.write_text(
        re.sub(
            r'sha256 = "\w+"',
            f'sha256 = "{__import__("hashlib").sha256(listing).hexdigest()}"',
            pinned,
            count=1,
        )
    )
    (instance / "languages/cpp/overlay.toml").write_text("")
    calls: list[str] = []

    def download(url: str) -> bytes:
        calls.append(url)
        return data

    monkeypatch.setattr(standards.sources, "_download", download)
    source = standards.guides()["cpp-core-guidelines"]
    folder = standards.tree(source)
    assert folder.is_relative_to(tmp_path / "cache")
    assert (folder / "R/R.1.md").read_text().endswith("R.1: One\n")
    assert standards.tree(source) == folder and len(calls) == 1
    standards.copy_references(tmp_path / "worktree")
    copied = tmp_path / "worktree" / standards.WORKTREE_REFERENCES
    assert (copied / "cpp-core-guidelines/R/R.1.md").is_file()
    assert not (copied / "cpp-core-guidelines/source").exists()
    assert (copied / "google-python/index.md").is_file()
    shutil.rmtree(folder)
    manifest.write_text(pinned)
    with pytest.raises(ValueError, match="not the pinned"):
        standards.tree(standards.guides()["cpp-core-guidelines"])


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
    assert lines[-3:] == cli.project_types()["cpp"]["commands"]


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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    python = _instance(monkeypatch, tmp_path) / "languages" / "python"
    tools = python / "tools.toml"
    tools.write_text(
        tools.read_text()
        + '\n[lister]\nrole = "checker"\nrun = "true"\n'
        + 'list = "printf \'N802\\\\nZ999\\\\n\'"\nlink = "https://x.org"\n'
    )
    overlay = python / "overlay.toml"
    overlay.write_text(
        overlay.read_text().replace(
            'checked_by = ["ruff:N*"]', 'checked_by = ["ruff:N*", "lister:N*"]'
        )
    )
    assert cli.main(["show", "--coverage"]) == 0
    shown = capsys.readouterr().out
    assert "## Google Python Style Guide (`google-python`, CC-BY-3.0)" in shown
    assert "| 3 Python Style Rules | 18 | 2 | 16 |" in shown
    assert "| R: Resource management | 25 | 3 | 22 |" in shown
    assert "- python/lister: Z999 maps to no guideline; see https://x.org" in (
        shown
    )
    assert "N802" not in shown
