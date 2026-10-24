"""plumbcheck.yml, the linters, and the Claude Code and Codex hooks."""

import gzip
import io
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path
from typing import Any

import pytest
import yaml

from plumbcheck import cli, config, hooks, linting

ORG = Path(__file__).parent / "fixtures" / "org"

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
VIOLATION = CLEAN + "\n\ndef mixedCase() -> None:\n    pass\n"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _repository(
    tmp_path: Path, contents: dict[str, str], *, configured: bool = True
) -> tuple[Path, str]:
    """A committed repository of the sample organization, its base synced.

    Without ``configured``, it has no plumbcheck.yml.
    """
    root = tmp_path / "repository"
    root.mkdir()
    for path, text in contents.items():
        _write(root / path, text)
    if configured:
        _write(root / config.NAME, f"EXTENDS: {ORG / config.NAME}\n")
        cli.sync(root, check=False)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root, _git(root, "rev-parse", "HEAD").strip()


def _rules(found: linting.Result) -> set[tuple[str, str, str]]:
    return {(f.path, f.linter, f.rule) for f in found.findings}


# --- plumbcheck.yml


def test_later_files_win_mappings_merge_and_named_lists_append(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "base.yml",
        "EXTENDS: child.yml\n"  # a cycle is read once
        "A: 1\nL: [a]\nK: [a]\nM: {x: 1, y: 2}\n"
        "CONFIG_PROPERTIES_TO_APPEND: [L]\n",
    )
    child = _write(
        tmp_path / "child.yml",
        "EXTENDS: base.yml\nA: 2\nL: [b]\nK: [b]\nM: {y: 3}\n",
    )
    found = config.resolve(str(child))
    assert (found["A"], found["L"], found["K"], found["M"]) == (
        2,
        ["a", "b"],
        ["b"],
        {"x": 1, "y": 3},
    )


def test_an_extended_url_is_fetched_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    fetched: list[str] = []

    def download(url: str) -> bytes:
        fetched.append(url)
        return b"A: 1\nB: 1\n"

    monkeypatch.setattr(config, "_download", download)
    url = "https://example.org/x/plumbcheck.yml"
    own = _write(tmp_path / config.NAME, f"EXTENDS: {url}\nB: 2\n")
    loaded = config.load(tmp_path)
    assert (loaded["A"], loaded["B"]) == (1, 2) and "LINTERS" in loaded
    assert config.resolve(str(own)) == {"A": 1, "B": 2}
    assert fetched == [url]


def _archive(files: dict[str, str]) -> bytes:
    """A GitHub tarball: every file under one top folder."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, text in files.items():
            data = text.encode()
            member = tarfile.TarInfo(f"org-abc/{name}")
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return gzip.compress(buffer.getvalue())


def test_a_github_file_resolves_its_paths_in_its_repository_s_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    fetched: list[str] = []
    data = _archive(
        {
            "plumbcheck.yml": "EXTENDS: base.yml\nFILES:\n"
            "  pyproject.toml: {ruff.toml: rules/ruff.toml, vale: rules/vale}\n",
            "base.yml": "FILES: {general: {.vale.ini: rules/.vale.ini}}\n",
            "rules/.vale.ini": "StylesPath = vale\n",
            "rules/ruff.toml": "line-length = 70\n",
            "rules/vale/A/B.yml": "extends: existence\n",
        }
    )

    def download(url: str) -> bytes:
        fetched.append(url)
        return data

    monkeypatch.setattr(config, "_download", download)
    url = "https://raw.githubusercontent.com/o/org/abc/plumbcheck.yml"
    _write(tmp_path / config.NAME, f"EXTENDS: {url}\n")
    loaded = config.load(tmp_path)
    snapshot = tmp_path / "home/.cache/plumbcheck/extends/o/org/abc"
    assert loaded["FILES"]["general"][".vale.ini"] == str(
        snapshot / "rules/.vale.ini"
    )
    assert loaded["FILES"]["pyproject.toml"]["ruff.toml"] == str(
        snapshot / "rules/ruff.toml"
    )
    (tmp_path / "pyproject.toml").write_text("")
    (tmp_path / "uv.lock").write_text("")
    # A folder brings every file below it.
    assert cli.sync(tmp_path, check=False) == [
        ".plumbcheck/.vale.ini",
        ".plumbcheck/ruff.toml",
        ".plumbcheck/vale/A/B.yml",
    ]
    base = tmp_path / ".plumbcheck"
    assert (base / "ruff.toml").read_text() == "line-length = 70\n"
    assert (base / "vale/A/B.yml").is_file()
    config.load(tmp_path)
    assert fetched == ["https://codeload.github.com/o/org/tar.gz/abc"]


def test_a_repository_without_its_own_file_gets_the_defaults(
    tmp_path: Path,
) -> None:
    found = config.load(tmp_path)
    linters = config.linters(found)
    assert "PYTHON_RUFF" in linters and "CPP_CLANG_TIDY" in linters
    # No opinions: no base configurations.
    assert "FILES" not in found


def test_overrides_enable_disable_filter_and_add_arguments() -> None:
    found = config.linters(
        {
            "LINTERS": {
                "A": {"command": "a"},
                "B": {"command": "b"},
                "C": {"command": "c"},
            },
            "DISABLE_LINTERS": ["B"],
            "ENABLE_LINTERS": ["A", "B"],
            "FILTER_REGEX_EXCLUDE": "^vendor/",
            "A_FILTER_REGEX_EXCLUDE": "^docs/",
            "A_ARGUMENTS": "--strict",
        }
    )
    assert list(found) == ["A"]
    assert found["A"]["exclude"] == ["^vendor/", "^docs/"]
    assert found["A"]["arguments"] == ["--strict"]


# --- parsers


def test_each_output_kind_parses_path_line_rule_and_message() -> None:
    ruff = '[{"filename": "/r/a.py", "location": {"row": 3}, "code": "F401", "message": "unused"}]'
    eslint = (
        '[{"filePath": "/r/a.ts", "messages": '
        '[{"line": 2, "ruleId": "no-var", "message": "var"}]}]'
    )
    vale = '{"a.rst": [{"Line": 4, "Check": "Google.Will", "Message": "will"}]}'
    leaks = '[{"File": "a.txt", "StartLine": 1, "RuleID": "generic-api-key", "Description": "key"}]'
    tidy = (
        "1 warning generated.\n"
        "/r/a.cpp:2:12: warning: initializing non-owner [cppcoreguidelines-owning-memory]\n"
        "/r/a.cpp:2:12: note: here\n"
    )
    assert linting.parse({"output": "json-ruff"}, ruff) == [
        ("/r/a.py", 3, "F401", "unused", "")
    ]
    assert linting.parse({"output": "json-eslint"}, eslint) == [
        ("/r/a.ts", 2, "no-var", "var", "")
    ]
    assert linting.parse({"output": "json-vale"}, vale) == [
        ("a.rst", 4, "Google.Will", "will", "")
    ]
    assert linting.parse({"output": "json-gitleaks"}, leaks) == [
        ("a.txt", 1, "generic-api-key", "key", "")
    ]
    specs = config.load()["LINTERS"]
    assert linting.parse(specs["CPP_CLANG_TIDY"], tidy) == [
        (
            "/r/a.cpp",
            2,
            "cppcoreguidelines-owning-memory",
            "initializing non-owner",
            "",
        )
    ]
    assert linting.parse(
        specs["SPELL_CODESPELL"], "a.rst:1: teh ==> the\n"
    ) == [("a.rst", 1, "misspelling", "teh ==> the", "")]


# --- links


def test_a_rule_links_its_documentation_by_template_and_prefix() -> None:
    linters = config.load()["LINTERS"]
    tidy, eslint = linters["CPP_CLANG_TIDY"], linters["TYPESCRIPT_ESLINT"]
    checks = "https://clang.llvm.org/extra/clang-tidy/checks/"
    assert linting.link(tidy, "bugprone-use-after-move,hicpp-x") == (
        f"{checks}bugprone/use-after-move.html"
    )
    assert linting.link(tidy, "clang-analyzer-core.NullDereference") == (
        f"{checks}clang-analyzer/core.NullDereference.html"
    )
    assert linting.link(eslint, "@typescript-eslint/no-unused-vars") == (
        "https://typescript-eslint.io/rules/no-unused-vars"
    )
    gitleaks = linters["REPOSITORY_GITLEAKS"]
    assert linting.link(gitleaks, "generic-api-key") == gitleaks["link"]


# --- file selection


def test_the_base_of_general_and_of_the_markers_present_is_synced(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text("")
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
    # What git ignores there, such as a tool's cache, does not count.
    _git(tmp_path, "init", "-q")
    _write(tmp_path / ".plumbcheck/.ruff_cache/.gitignore", "*\n")
    _write(tmp_path / ".plumbcheck/.ruff_cache/x", "")
    assert cli.sync(tmp_path, check=True) == []
    # The repository's own configurations inherit the base.
    assert cli.inherit(tmp_path) == ["ruff.toml", "pyrightconfig.json"]
    assert (tmp_path / "ruff.toml").read_text() == cli.INHERIT["ruff.toml"]
    assert cli.inherit(tmp_path) == []
    assert cli.sync(tmp_path, check=True) == []
    # Without the marker, its files go.
    (tmp_path / "pyproject.toml").unlink()
    assert cli.sync(tmp_path, check=True) == [".plumbcheck/pyrightconfig.json"]


def test_changed_files_are_modified_and_new_ones_and_nothing_is_staged(
    tmp_path: Path,
) -> None:
    root, base = _repository(
        tmp_path,
        {
            "a.py": CLEAN,
            "b.py": CLEAN,
            "manifest.json": json.dumps(
                {"sources": [{"path": "gen.py", "ownership": "generated"}]}
            ),
        },
    )
    (root / "a.py").write_text(VIOLATION)
    (root / "new.py").write_text(VIOLATION)
    (root / "gen.py").write_text(VIOLATION)
    assert linting.changed_files(root, base) == ["a.py", "new.py"]
    assert _git(root, "diff", "--cached", "--name-only") == ""
    assert "b.py" in linting.all_files(root)


def test_every_finding_of_a_checked_file_counts(tmp_path: Path) -> None:
    root, base = _repository(tmp_path, {"legacy.py": VIOLATION})
    (root / "new.py").write_text(VIOLATION)
    assert {"N802", "D103"} <= {
        r
        for p, _, r in _rules(linting.lint(root, ["legacy.py"]))
        if p == "legacy.py"
    }
    (error,) = linting.lint_errors(root, base)
    assert error.startswith("new.py has 2 findings; fix each one:")
    assert "  new.py:17: PYTHON_RUFF:N802 Function name" in error
    assert "→ https://docs.astral.sh/ruff/rules/invalid-function-name" in error


def test_generated_deleted_and_test_files_need_no_docstrings(
    tmp_path: Path,
) -> None:
    root, _ = _repository(
        tmp_path,
        {
            "manifest.json": json.dumps(
                {"sources": [{"path": "gen.py", "ownership": "generated"}]}
            )
        },
    )
    _write(root / "gen.py", VIOLATION)
    _write(
        root / "tests" / "test_a.py",
        CLEAN + "\n\ndef test_b() -> None:\n    pass\n",
    )
    found = linting.lint(root, ["gen.py", "gone.py", "tests/test_a.py"])
    assert found.findings == [] and found.problems == []


def test_a_linter_s_config_is_the_repository_s_the_base_or_none(
    tmp_path: Path,
) -> None:
    root, _ = _repository(tmp_path, {})

    def flags(name: str) -> list[str]:
        spec: dict[str, Any] = {"command": "x", "arguments": []}
        spec["config"] = {"file": name, "flag": "-c"}
        return linting._command(root, spec, "command")[1:]  # pyright: ignore[reportPrivateUsage]

    base = root / config.BASE
    assert flags("ruff.toml") == ["-c", str(base / "ruff.toml")]
    assert flags(".missing") == []
    _write(root / "ruff.toml", 'extend = ".plumbcheck/ruff.toml"\n')
    assert flags("ruff.toml") == ["-c", str(root / "ruff.toml")]


def test_a_repository_s_clang_tidy_is_laid_over_the_base(
    tmp_path: Path,
) -> None:
    base = _write(
        tmp_path / "base.yml",
        "Checks: '-*,bugprone-*'\nHeaderFilterRegex: 'src/'\n"
        "CheckOptions:\n  - key: a.x\n    value: '1'\n",
    )
    own = _write(
        tmp_path / "own.yml",
        "Checks: '-bugprone-easily-swappable-parameters'\n"
        "CheckOptions:\n  b.y: '2'\n",
    )
    merged = yaml.safe_load(linting.merged_yaml(base, own).read_text())
    assert merged == {
        "Checks": "-*,bugprone-*,-bugprone-easily-swappable-parameters",
        "HeaderFilterRegex": "src/",
        "CheckOptions": {"a.x": "1", "b.y": "2"},
    }


def test_a_tool_that_needs_a_configuration_skips_without_one(
    tmp_path: Path,
) -> None:
    root, _ = _repository(
        tmp_path, {"a.rst": "Title\n=====\n\nIt is is fine.\n"}
    )
    assert {f.linter for f in linting.lint(root, ["a.rst"]).findings} >= {
        "SPELL_VALE"
    }
    (root / config.BASE / ".vale.ini").unlink()
    assert "SPELL_VALE" not in {
        f.linter for f in linting.lint(root, ["a.rst"]).findings
    }


def test_a_missing_requirement_is_a_problem_not_a_pass(tmp_path: Path) -> None:
    root, _ = _repository(tmp_path, {"a.cpp": "int f();\n"})
    assert linting.lint(root, ["a.cpp"]).problems == [
        "CPP_CLANG_TIDY needs build/plumbcheck/compile_commands.json or "
        "build/compile_commands.json: configure CMake with "
        "CMAKE_EXPORT_COMPILE_COMMANDS=ON",
    ]
    spec = config.linters(config.load(root))["CPP_CLANG_TIDY"]
    _write(root / "build/compile_commands.json", "[]")
    assert linting._command(root, spec, "command")[:3] == [  # pyright: ignore[reportPrivateUsage]
        "clang-tidy", "-p", str(root / "build"),
    ]  # fmt: skip
    # CPP_CLANG_TIDY_COMPILE_COMMANDS names another one.
    _write(
        root / config.NAME,
        (root / config.NAME).read_text()
        + "CPP_CLANG_TIDY_COMPILE_COMMANDS: out/compile_commands.json\n",
    )
    assert linting.lint(root, ["a.cpp"]).problems == [
        "CPP_CLANG_TIDY needs out/compile_commands.json: configure CMake "
        "with CMAKE_EXPORT_COMPILE_COMMANDS=ON",
    ]


def test_lint_fixes_first_and_fails_on_what_remains(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root, _ = _repository(tmp_path, {"a.py": "import os\n" + VIOLATION})
    assert (
        cli.main(["--root", str(root), "lint", "--fix", str(root / "a.py")])
        == 1
    )
    assert "import os" not in (root / "a.py").read_text()
    shown = capsys.readouterr().out
    assert "N802" in shown and "F401" not in shown
    (root / "a.py").write_text(CLEAN)
    assert cli.main(["--root", str(root), "lint", "--all"]) == 0
    assert capsys.readouterr().out.endswith("no findings\n")


# Reversed, so this file itself leaks nothing.
_LEAK = "vA0jG5sF1dH6cN4bY7rX2wP9mLk3Tv8qZ"[::-1]
# A violation per linter, inline, and the start of the link its finding gets.
FIRES = [
    ("PYTHON_RUFF", "a.py", VIOLATION, "https://docs.astral.sh/ruff/rules/"),
    ("PYTHON_RUFF_FORMAT", "a.py", CLEAN.replace("left + right", "left+right"), "https://docs.astral.sh/ruff/"),
    ("J2LINT", "a.j2", "{% if a %}{% if b %}\nx\n{% endif %}{% endif %}\n", "https://github.com/aristanetworks/j2lint"),
    ("RST_DOC8", "a.rst", "The gate measures the change.   \n", "https://github.com/PyCQA/doc8"),
    ("SPELL_VALE", "a.rst", "The gate is is ready.\n", "https://vale.sh/"),
    ("SPELL_CODESPELL", "a.rst", "The gate measures teh change.\n", "https://github.com/codespell-project/"),
    ("TYPESCRIPT_ESLINT", "a.ts", "var count = 1;\nexport const same = count;\n", "https://eslint.org/docs/latest/rules/no-var"),
    ("CPP_CLANG_TIDY", "a.cpp", "struct N {};\nN* make() { return new N; }\n", "https://clang.llvm.org/extra/clang-tidy/checks/cppcoreguidelines/owning-memory.html"),
    ("CPP_CLANG_FORMAT", "a.cpp", "int  f( int a ){return a;}\n", "https://clang.llvm.org/docs/ClangFormatStyleOptions.html"),
    ("REPOSITORY_GITLEAKS", "a.txt", f'api_key = "{_LEAK}"\n', "https://github.com/gitleaks/"),
]  # fmt: skip
ESLINT_CONFIG = """import tseslint from 'typescript-eslint';
export default tseslint.config({
  files: ['**/*.ts'],
  extends: [tseslint.configs.base],
  rules: {'no-var': 'error'},
});
"""


@pytest.fixture(scope="module")
def node_modules(tmp_path_factory: pytest.TempPathFactory) -> Path | None:
    """ESLint with the TypeScript parser, installed once; None without npm."""
    if shutil.which("npm") is None:
        return None
    folder = tmp_path_factory.mktemp("eslint")
    subprocess.run(
        [
            "npm",
            "install",
            "--silent",
            "eslint@9",
            "typescript-eslint@8",
            "typescript@5",
        ],
        cwd=folder,
        capture_output=True,
        check=True,
    )
    return folder / "node_modules"


@pytest.mark.parametrize(("linter", "name", "text", "link"), FIRES)
def test_each_linter_flags_a_violation_and_links_its_rule(
    tmp_path: Path,
    node_modules: Path | None,
    linter: str,
    name: str,
    text: str,
    link: str,
) -> None:
    """The real linter runs, its output parses and the finding links its rule.

    A missing linter skips, unless ``REQUIRE_CHECKERS`` is set (the instance
    job of CI installs them all).
    """
    root, _ = _repository(tmp_path, {name: text})
    if linter == "TYPESCRIPT_ESLINT" and node_modules:
        (root / "node_modules").symlink_to(node_modules)
        _write(root / "eslint.config.mjs", ESLINT_CONFIG)
    _write(
        root / "build" / "plumbcheck" / "compile_commands.json",
        json.dumps(
            [
                {
                    "directory": str(root),
                    "file": str(root / name),
                    "command": f"c++ -std=c++20 -c {root / name}",
                }
            ]
        ),
    )
    found = linting.lint(root, [name])
    if found.problems:
        if os.environ.get("REQUIRE_CHECKERS"):
            raise AssertionError(found.problems)
        pytest.skip("; ".join(found.problems))
    mine = [f for f in found.findings if f.linter == linter]
    assert mine, found.findings
    assert any(f.link.startswith(link) for f in mine), mine


def test_the_base_is_the_merge_base_with_origin_main_without_origin_head(
    tmp_path: Path,
) -> None:
    root, base = _repository(tmp_path, {"a.py": CLEAN})
    _git(root, "update-ref", "refs/remotes/origin/main", base)
    _git(root, "commit", "-q", "--allow-empty", "-m", "work")
    _git(root, "commit", "-q", "--allow-empty", "-m", "more")
    assert cli.base_of(root) == base


def test_a_complex_function_is_a_finding(
    tmp_path: Path,
) -> None:
    branches = "".join(
        f"    if x == {i}:\n        return {i}\n" for i in range(16)
    )
    complex_ = f'\n\ndef pick(x: int) -> int:\n    """Pick."""\n{branches}    return -1\n'
    root, base = _repository(tmp_path, {})
    _write(root / "a.py", CLEAN + complex_)
    (error,) = linting.lint_errors(root, base)
    assert "PYTHON_RUFF:C901" in error


# --- hooks


def _hooked(tmp_path: Path, contents: dict[str, str]) -> tuple[Path, str]:
    """A repository the hooks act in: it has a plumbcheck.yml."""
    return _repository(tmp_path, contents)


def test_a_patch_names_its_added_updated_and_moved_files() -> None:
    patch = (
        "*** Begin Patch\n*** Add File: a.py\n+x\n*** Update File: b.py\n"
        "*** Move to: c.py\n@@\n-y\n+z\n*** Delete File: d.py\n*** End Patch\n"
    )
    payload = {"tool_name": "apply_patch", "tool_input": {"command": patch}}
    assert hooks.edited(payload) == ["a.py", "b.py", "c.py"]
    claude = {"tool_name": "Edit", "tool_input": {"file_path": "/r/a.py"}}
    assert hooks.edited(claude) == ["/r/a.py"]


def test_post_edit_fixes_and_feeds_the_findings_back(tmp_path: Path) -> None:
    root, _ = _hooked(tmp_path, {"a.py": CLEAN})
    (root / "a.py").write_text("import os\n" + VIOLATION)
    claude = {
        "cwd": str(root),
        "tool_name": "Write",
        "tool_input": {"file_path": str(root / "a.py")},
    }
    reply = hooks.post_edit(claude)
    assert reply.code == 2 and reply.stdout == ""
    assert "PYTHON_RUFF:N802" in reply.stderr and "F401" not in reply.stderr
    codex = {
        "cwd": str(root / "sub"),
        "turn_id": "t",
        "tool_name": "apply_patch",
        "tool_input": {
            "command": "*** Begin Patch\n*** Update File: ../a.py\n"
        },
    }
    (root / "sub").mkdir()
    assert hooks.post_edit(codex).code == 2
    (root / "a.py").write_text(CLEAN)
    assert hooks.post_edit(claude) == hooks.Reply()
    outside = {**claude, "tool_input": {"file_path": str(tmp_path / "x.py")}}
    assert hooks.post_edit(outside) == hooks.Reply()


def test_stop_blocks_twice_then_lets_the_agent_stop(tmp_path: Path) -> None:
    root, _ = _hooked(tmp_path, {})
    (root / "new.txt").write_text("changed\n")
    problems = ["a.py has 1 findings"]

    def check(found: Path) -> list[str]:
        assert found == root
        return problems

    claude = {"cwd": str(root), "session_id": "s/1", "stop_hook_active": False}
    first = hooks.stop(claude, check)
    assert first.code == 2 and "a.py has 1 findings" in first.stderr
    again = {**claude, "stop_hook_active": True}
    assert hooks.stop(again, check).code == 2
    third = hooks.stop(again, check)
    assert third.code == 0
    assert (
        "after 2 blocked attempts" in json.loads(third.stdout)["systemMessage"]
    )
    assert not list(hooks.COUNTERS.expanduser().iterdir())
    # A new turn starts counting again.
    assert hooks.stop(again, check).code == 2
    assert hooks.stop(claude, check).code == 2
    codex = {**claude, "turn_id": "t", "last_assistant_message": "done"}
    blocked = json.loads(hooks.stop(codex, check).stdout)
    assert blocked["decision"] == "block" and "a.py" in blocked["reason"]
    problems.clear()
    assert hooks.stop(codex, check) == hooks.Reply(stdout="{}")
    assert hooks.stop(claude, check) == hooks.Reply()


def test_hooks_act_only_with_a_plumbcheck_yml_and_not_when_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "plain").mkdir()
    (tmp_path / "hooked").mkdir()
    root, _ = _repository(
        tmp_path / "plain", {"a.py": VIOLATION}, configured=False
    )
    hooked, _ = _hooked(tmp_path / "hooked", {"a.py": VIOLATION})
    (hooked / "new.txt").write_text("changed\n")

    def check(found: Path) -> list[str]:
        return ["fails"]

    def payloads(folder: Path) -> tuple[dict[str, Any], dict[str, Any]]:
        edit = {
            "cwd": str(folder),
            "tool_name": "Write",
            "tool_input": {"file_path": str(folder / "a.py")},
        }
        return edit, {"cwd": str(folder), "turn_id": "t", "session_id": "s"}

    edit, codex = payloads(root)
    assert hooks.post_edit(edit) == hooks.Reply()
    assert hooks.stop(codex, check) == hooks.Reply(stdout="{}")
    assert hooks.stop({"cwd": str(root)}, check) == hooks.Reply()
    # The edited file's repository counts, not the session's folder.
    assert hooks.post_edit({**payloads(hooked)[0], "cwd": str(root)}).code == 2
    edit, codex = payloads(hooked)
    assert hooks.stop(codex, check).stdout != "{}"
    monkeypatch.setenv("PLUMBCHECK_HOOKS", "off")
    assert hooks.post_edit(edit) == hooks.Reply()
    assert hooks.stop(codex, check) == hooks.Reply(stdout="{}")


def test_hooks_uninstall_removes_exactly_ours(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = {"matcher": "Bash", "hooks": [{"type": "command", "command": "x"}]}
    before = {"model": "m", "hooks": {"PostToolUse": [other]}}
    settings = _write(
        tmp_path / ".claude" / "settings.json", json.dumps(before)
    )

    def which(name: str) -> str:
        return f"/bin/{name}"

    monkeypatch.setattr(hooks.shutil, "which", which)
    hooks.install(tmp_path, claude=True, codex=True)
    assert "-" in hooks.install(tmp_path, claude=True, codex=True, remove=True)
    assert json.loads(settings.read_text()) == before
    codex = tmp_path / ".codex" / "hooks.json"
    assert json.loads(codex.read_text()) == {}
    assert hooks.install(tmp_path, claude=True, codex=True, remove=True) == ""
    codex.unlink()
    assert hooks.install(tmp_path, claude=False, codex=True, remove=True) == ""
    assert not codex.exists()


def test_hooks_install_is_idempotent_and_keeps_other_hooks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = {"matcher": "Bash", "hooks": [{"type": "command", "command": "x"}]}
    settings = _write(
        tmp_path / ".claude" / "settings.json",
        json.dumps({"model": "m", "hooks": {"PostToolUse": [other]}}),
    )

    def which(name: str) -> str:
        return f"/bin/{name}"

    monkeypatch.setattr(hooks.shutil, "which", which)
    diff = hooks.install(tmp_path, claude=True, codex=True)
    assert "+++ " + str(settings) in diff
    assert hooks.install(tmp_path, claude=True, codex=True) == ""
    written = json.loads(settings.read_text())
    assert written["model"] == "m"
    post, stop = written["hooks"]["PostToolUse"], written["hooks"]["Stop"]
    assert post[0] == other and post[1]["matcher"] == "Edit|Write|MultiEdit"
    assert post[1]["hooks"][0]["command"] == "/bin/plumbcheck hook post-edit"
    assert stop[0]["hooks"][0]["command"] == "/bin/plumbcheck hook stop"
    codex = json.loads((tmp_path / ".codex" / "hooks.json").read_text())
    assert codex["hooks"]["PostToolUse"][0]["matcher"] == "apply_patch"
    assert len(codex["hooks"]["Stop"]) == 1


def test_base_of_a_lone_commit_is_the_empty_tree(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t",
         "commit", "-qm", "init"],
        check=True,
    )  # fmt: skip
    assert cli.base_of(tmp_path) == cli.EMPTY_TREE
    assert linting.changed_files(tmp_path, cli.EMPTY_TREE) == ["a.py"]


def test_a_failing_hook_reports_without_blocking(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken(payload: dict[str, Any]) -> hooks.Reply:
        raise RuntimeError("boom")

    monkeypatch.setattr(hooks, "stop", broken)
    codex = json.dumps({"turn_id": "t", "hook_event_name": "Stop"})
    monkeypatch.setattr("sys.stdin", io.StringIO(codex))
    assert hooks.run("stop") == 0
    assert "boom" in json.loads(capsys.readouterr().out)["systemMessage"]
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    assert hooks.run("stop") == 1
    assert "boom" in capsys.readouterr().err


def _sibling(parent: Path, name: str, ruff: str) -> Path:
    """A committed repository ``name`` in ``parent`` with its own ruff.toml."""
    (parent / name).mkdir()
    root, head = _repository(parent / name, {"ruff.toml": ruff, "a.py": CLEAN})
    _git(root, "update-ref", "refs/remotes/origin/main", head)  # pushed
    target = parent / name.upper()
    root.rename(target)
    return target


def test_one_patch_across_two_repositories_judges_each_file_by_its_own(
    tmp_path: Path,
) -> None:
    strict = _sibling(tmp_path, "strict", 'extend = ".plumbcheck/ruff.toml"\n')
    loose = _sibling(
        tmp_path,
        "loose",
        'extend = ".plumbcheck/ruff.toml"\n[lint]\nextend-ignore = ["N802"]\n',
    )
    for root in (strict, loose):
        (root / "a.py").write_text(VIOLATION)
    patch = (
        "*** Begin Patch\n*** Update File: STRICT/a.py\n"
        "*** Update File: LOOSE/a.py\n*** End Patch\n"
    )
    codex = {
        "cwd": str(tmp_path),
        "turn_id": "t",
        "tool_name": "apply_patch",
        "tool_input": {"command": patch},
    }
    reply = hooks.post_edit(codex)
    assert reply.code == 2
    assert "In STRICT/:" in reply.stderr and "In LOOSE/:" in reply.stderr
    strict_part, loose_part = reply.stderr.split("In LOOSE/:")
    assert "N802" in strict_part.split("In STRICT/:")[1]
    assert "N802" not in loose_part and "D103" in loose_part
    # `lint` with files of both does the same.
    found = dict(linting.lint_files([strict / "a.py", loose / "a.py"]))
    assert "N802" in {f.rule for f in found[strict].findings}
    assert "N802" not in {f.rule for f in found[loose].findings}


def test_stop_in_a_parent_folder_checks_each_changed_repository(
    tmp_path: Path,
) -> None:
    first = _sibling(tmp_path, "first", "")
    second = _sibling(tmp_path, "second", "")
    _sibling(tmp_path, "clean", "")
    (first / "new.py").write_text(CLEAN)
    (second / "new.py").write_text(CLEAN)
    checked: list[Path] = []

    def check(root: Path) -> list[str]:
        checked.append(root)
        return [f"{root.name} fails"]

    reply = hooks.stop({"cwd": str(tmp_path), "session_id": "p"}, check)
    assert sorted(checked) == sorted([first, second])
    assert reply.code == 2
    assert "In FIRST/:\nFIRST fails" in reply.stderr
    assert "In SECOND/:\nSECOND fails" in reply.stderr


FAKE_LINTER = """\
import sys
sources = sys.argv[1:]
for source in sources:
    print(f"{source}:1: one of {len(sources)} [run]")
if any(source.endswith(".cpp") for source in sources):
    print("include/a.hpp:2: seen from a source [header]")
"""
FAKE_CONFIG = """\
ENABLE_LINTERS: [FAKE]
LINTERS:
  FAKE:
    extensions: [.cpp, .hpp]
    command: "{python} fake.py"
    output: 'regex:^(?P<path>[^:]+):(?P<line>\\d+): (?P<message>.*) \\[(?P<rule>\\w+)\\]$'
"""


def _fake(tmp_path: Path, files: list[str]) -> Path:
    """A repository whose only linter reports each file with its run's size."""
    contents = {"fake.py": FAKE_LINTER, config.NAME: FAKE_CONFIG}
    root, _ = _repository(
        tmp_path, contents | dict.fromkeys(files, ""), configured=False
    )
    return root


def test_runs_are_spread_over_the_workers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(linting, "WORKERS", 4)
    sources = [f"src/f{i}.cpp" for i in range(10)]
    found = linting.lint(_fake(tmp_path, sources), sources)
    sizes = sorted(f.message for f in found.findings if f.rule == "run")
    assert sizes == ["one of 1"] + ["one of 3"] * 9


def test_header_findings_from_another_run_are_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # clang-tidy reports a header's findings while checking a source that
    # includes it; with one file per run, the header is in a run of its own.
    monkeypatch.setattr(linting, "WORKERS", 3)
    files = ["include/a.hpp", "src/a.cpp", "src/b.cpp"]
    root = _fake(tmp_path, files)
    assert ("include/a.hpp", "FAKE", "header") in _rules(
        linting.lint(root, files)
    )
    # A file outside the lint stays out.
    assert ("include/a.hpp", "FAKE", "header") not in _rules(
        linting.lint(root, ["src/a.cpp"])
    )


CONFIG_LINTER = """\
import sys
given = "--cfg" in sys.argv
print(f"src/a.cpp:1: config given: {given} [cfg]")
"""
CONFIG_SPEC = """\
ENABLE_LINTERS: [FAKE]
LINTERS:
  FAKE:
    extensions: [.cpp]
    command: "{python} fake.py"
    config: {file: .fake.yml, flag: --cfg, merge: yaml}
    output: 'regex:^(?P<path>[^:]+):(?P<line>\\d+): (?P<message>.*) \\[(?P<rule>\\w+)\\]$'
"""


def test_a_merged_config_is_given_only_over_a_base(tmp_path: Path) -> None:
    # Without a base the tool finds the repository's file itself, and the
    # files in subfolders that adjust it; a given file would hide those.
    contents = {
        "fake.py": CONFIG_LINTER,
        config.NAME: CONFIG_SPEC,
        "src/a.cpp": "",
    }
    root, _ = _repository(
        tmp_path, contents | {".fake.yml": "A: 1\n"}, configured=False
    )
    messages = {f.message for f in linting.lint(root, ["src/a.cpp"]).findings}
    assert messages == {"config given: False"}
    _write(root / config.BASE / ".fake.yml", "B: 2\n")
    messages = {f.message for f in linting.lint(root, ["src/a.cpp"]).findings}
    assert messages == {"config given: True"}
