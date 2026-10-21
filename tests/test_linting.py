"""standards.yml, the linters, and the Claude Code and Codex hooks."""

import io
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from verdog_standards import cli, config, engine, hooks, linting

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


def _repository(tmp_path: Path, contents: dict[str, str]) -> tuple[Path, str]:
    """A committed repository with the organization's ruff.toml."""
    root = tmp_path / "repository"
    root.mkdir()
    shutil.copy(config.RULES / "ruff.toml", root)
    for path, text in contents.items():
        _write(root / path, text)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root, _git(root, "rev-parse", "HEAD").strip()


def _rules(found: linting.Result) -> set[tuple[str, str, str]]:
    return {(f.path, f.linter, f.rule) for f in found.findings}


# --- standards.yml


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

    def urlopen(url: str, timeout: int) -> io.BytesIO:
        del timeout
        fetched.append(url)
        return io.BytesIO(b"A: 1\nB: 1\n")

    monkeypatch.setattr(config.urllib.request, "urlopen", urlopen)
    url = "https://example.org/x/standards.yml"
    own = _write(tmp_path / "standards.yml", f"EXTENDS: {url}\nB: 2\n")
    assert config.load(tmp_path) == {"A": 1, "B": 2}
    assert config.resolve(str(own)) == {"A": 1, "B": 2}
    assert fetched == [url]


def test_a_repository_without_its_own_file_gets_the_organization_s(
    tmp_path: Path,
) -> None:
    linters = config.linters(config.load(tmp_path))
    assert "PYTHON_RUFF" in linters and "CPP_CLANG_TIDY" in linters
    # Run by the project's commands, not by ``lint``.
    assert "TYPESCRIPT_TSC" not in linters


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
        ("/r/a.py", 3, "F401", "unused")
    ]
    assert linting.parse({"output": "json-eslint"}, eslint) == [
        ("/r/a.ts", 2, "no-var", "var")
    ]
    assert linting.parse({"output": "json-vale"}, vale) == [
        ("a.rst", 4, "Google.Will", "will")
    ]
    assert linting.parse({"output": "json-gitleaks"}, leaks) == [
        ("a.txt", 1, "generic-api-key", "key")
    ]
    specs = config.load()["LINTERS"]
    assert linting.parse(specs["CPP_CLANG_TIDY"], tidy) == [
        (
            "/r/a.cpp",
            2,
            "cppcoreguidelines-owning-memory",
            "initializing non-owner",
        )
    ]
    assert linting.parse(
        specs["SPELL_CODESPELL"], "a.rst:1: teh ==> the\n"
    ) == [("a.rst", 1, "misspelling", "teh ==> the")]


# --- file selection


def test_changed_files_are_modified_and_new_ones_and_nothing_is_staged(
    tmp_path: Path,
) -> None:
    root, base = _repository(
        tmp_path,
        {
            "a.py": CLEAN,
            "b.py": CLEAN,
            "project.json": json.dumps(
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
    assert error.startswith("new.py has 2 findings;")
    assert "  new.py:17: PYTHON_RUFF:N802 Function name" in error
    assert "    - google-python/3.16: Name modules" in error


def test_guides_generated_deleted_and_test_files_need_no_docstrings(
    tmp_path: Path,
) -> None:
    root, _ = _repository(
        tmp_path,
        {
            "project.json": json.dumps(
                {"sources": [{"path": "gen.py", "ownership": "generated"}]}
            )
        },
    )
    guide = "standards/languages/python/references/x.py"
    _write(root / guide, VIOLATION)
    _write(root / "gen.py", VIOLATION)
    _write(
        root / "tests" / "test_a.py",
        CLEAN + "\n\ndef test_b() -> None:\n    pass\n",
    )
    found = linting.lint(root, [guide, "gen.py", "gone.py", "tests/test_a.py"])
    assert found.findings == [] and found.problems == []


def test_a_missing_requirement_is_a_problem_not_a_pass(tmp_path: Path) -> None:
    root, _ = _repository(
        tmp_path, {"a.cpp": "int f();\n", "a.ts": "var x = 1;\n"}
    )
    assert linting.lint(root, ["a.cpp", "a.ts"]).problems == [
        "TYPESCRIPT_ESLINT needs node_modules/.bin/eslint: run `npm ci` first",
        "CPP_CLANG_TIDY needs build/standards/compile_commands.json: "
        "configure first (`verdog-standards run` creates it)",
    ]


def test_an_unmapped_rule_names_the_linter_s_documentation(
    tmp_path: Path,
) -> None:
    root, _ = _repository(tmp_path, {"a.py": "import os\n"})
    (error,) = linting.report(root, linting.lint(root, ["a.py"]))
    assert (
        "    - no guideline maps ruff F401; see https://docs.astral.sh/ruff/rules/"
        in error.splitlines()
    )


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
# A violation per linter, inline; its finding must map to a guideline unless
# no guideline names the linter (the formatter).
FIRES = [
    ("PYTHON_RUFF", "a.py", VIOLATION, True),
    ("PYTHON_RUFF_FORMAT", "a.py", CLEAN.replace("left + right", "left+right"), False),
    ("PYTHON_JINJA_ENVIRONMENT", "a.py", "import jinja2\n\nT = jinja2.Environment()\n", True),
    ("JINJA_TAGS", "a.j2", "{% if a %}{% if b %}\nx\n{% endif %}{% endif %}\n", True),
    ("RST_DOC8", "a.rst", "The gate measures the change.   \n", True),
    ("SPELL_VALE", "a.rst", "The change is measured by the gate.\n", True),
    ("SPELL_CODESPELL", "a.rst", "The gate measures teh change.\n", True),
    ("TYPESCRIPT_ESLINT", "a.ts", "var count = 1;\nexport const same = count;\n", True),
    ("CPP_CLANG_TIDY", "a.cpp", "struct N {};\nN* make() { return new N; }\n", True),
    ("REPOSITORY_GITLEAKS", "a.txt", f'api_key = "{_LEAK}"\n', True),
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


@pytest.mark.parametrize(("linter", "name", "text", "mapped"), FIRES)
def test_each_linter_flags_a_violation_its_guidelines_map(
    tmp_path: Path,
    node_modules: Path | None,
    linter: str,
    name: str,
    text: str,
    mapped: bool,
) -> None:
    """The real linter runs, its output parses and a guideline maps it.

    A missing linter skips, unless ``REQUIRE_CHECKERS`` is set (the instance
    job of CI installs them all).
    """
    root, _ = _repository(tmp_path, {name: text})
    if linter == "TYPESCRIPT_ESLINT" and node_modules:
        (root / "node_modules").symlink_to(node_modules)
        _write(root / "eslint.config.mjs", ESLINT_CONFIG)
    _write(
        root / "build" / "standards" / "compile_commands.json",
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
    if mapped:
        spec: dict[str, Any] = config.load()["LINTERS"][linter]
        assert any(
            engine.matches(entry, alias, f.rule)
            for f in mine
            for language in engine.load().values()
            for g in language.guidelines
            for entry in g.checked_by
            for alias in spec["aliases"]
        ), mine


def test_a_cache_only_guide_s_rule_text_stays_out_of_the_report() -> None:
    (owner,) = [
        g
        for g in engine.load()["cpp"].guidelines
        if g.id == "cpp-core-guidelines/R.20"
    ]
    line = engine.rule_line(owner, Path.cwd())
    entry = engine.rule(owner.id)
    assert line.startswith(
        "- cpp-core-guidelines/R.20: cpp-core-guidelines/R.20 ["
    )
    assert entry is None or entry.title not in line


def test_the_base_is_the_merge_base_with_origin_main_without_origin_head(
    tmp_path: Path,
) -> None:
    root, base = _repository(tmp_path, {"a.py": CLEAN})
    _git(root, "update-ref", "refs/remotes/origin/main", base)
    _git(root, "commit", "-q", "--allow-empty", "-m", "work")
    _git(root, "commit", "-q", "--allow-empty", "-m", "more")
    assert cli.base_of(root) == base


def test_a_complex_function_cites_the_function_length_rule(
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
    assert "google-python/3.18" in error


# --- hooks


def _hooked(tmp_path: Path, contents: dict[str, str]) -> tuple[Path, str]:
    """A repository the hooks act in: it extends the organization's file."""
    own = f"EXTENDS: {config.ORGANIZATION}\n"
    return _repository(tmp_path, {**contents, config.NAME: own})


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
    counters = Path(_git(root, "rev-parse", "--absolute-git-dir").strip())
    assert not list((counters / "verdog-standards").iterdir())
    # A new turn starts counting again.
    assert hooks.stop(again, check).code == 2
    assert hooks.stop(claude, check).code == 2
    codex = {**claude, "turn_id": "t", "last_assistant_message": "done"}
    blocked = json.loads(hooks.stop(codex, check).stdout)
    assert blocked["decision"] == "block" and "a.py" in blocked["reason"]
    problems.clear()
    assert hooks.stop(codex, check) == hooks.Reply(stdout="{}")
    assert hooks.stop(claude, check) == hooks.Reply()


def test_hooks_act_only_with_a_standards_yml_and_not_when_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "plain").mkdir()
    (tmp_path / "hooked").mkdir()
    root, _ = _repository(tmp_path / "plain", {"a.py": VIOLATION})
    hooked, _ = _hooked(tmp_path / "hooked", {"a.py": VIOLATION})

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
    monkeypatch.setenv("VERDOG_STANDARDS_HOOKS", "off")
    assert hooks.post_edit(edit) == hooks.Reply()
    assert hooks.stop(codex, check) == hooks.Reply(stdout="{}")


def test_the_stop_hook_runs_the_checks_but_not_the_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = _hooked(tmp_path, {"CMakeLists.txt": "", "package.json": "{}"})
    lines: list[str] = []

    def record(line: str, **_: object) -> subprocess.CompletedProcess[str]:
        lines.append(line)
        return subprocess.CompletedProcess(line, 0, "", "")

    monkeypatch.setattr(hooks.subprocess, "run", record)
    assert hooks._commands(root) == ""  # pyright: ignore[reportPrivateUsage]
    types = cli.project_types(root)
    assert "npm ci" not in lines and types["typescript"]["commands"][0] in lines
    # Without build/standards, CMake configures first.
    assert types["cpp"]["setup"][0] in lines
    (root / "build" / "standards").mkdir(parents=True)
    assert cli.commands_of(root, "cpp", setup=False) == types["cpp"]["commands"]
    assert cli.commands_of(root, "cpp")[0] == types["cpp"]["setup"][0]


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
    assert (
        post[1]["hooks"][0]["command"] == "/bin/verdog-standards hook post-edit"
    )
    assert stop[0]["hooks"][0]["command"] == "/bin/verdog-standards hook stop"
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
