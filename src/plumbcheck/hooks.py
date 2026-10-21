"""The Claude Code and Codex hooks: lint while the agent works.

``post-edit`` lints, after fixing, the files an edit touched; ``stop`` runs
the full check (``lint`` since the merge base, then the project types'
commands) before the agent finishes, and blocks at most ``ATTEMPTS`` times in
a row. Both read the hook's JSON on stdin; the CLI is told apart by its
input (Codex sends a ``turn_id`` and edits with ``apply_patch``).

The hooks act only in repositories with a ``standards.yml``, so they can be
installed for the whole user; ``VERDOG_STANDARDS_HOOKS=off`` turns them off.
"""

import difflib
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, cast

from verdog_standards import cli, config, linting

ATTEMPTS: Final = 2  # blocked stops in a row before the agent may stop
EDIT_FINDINGS: Final = 20  # shown after an edit
OUTPUT_CHARS: Final = 3000  # of a failed command's output
PATCH_PATH: Final = re.compile(
    r"^\*\*\* (?:Add File|Update File|Move to): (.+)$", re.M
)
CLAUDE_EDITS: Final = "Edit|Write|MultiEdit"


@dataclass(frozen=True, slots=True)
class Reply:
    """What a hook prints and returns."""

    code: int = 0
    stdout: str = ""
    stderr: str = ""


def _codex(payload: dict[str, Any]) -> bool:
    return "turn_id" in payload or payload.get("tool_name") == "apply_patch"


def _root(folder: str | Path) -> Path | None:
    """The repository of ``folder`` if the hooks act there.

    They act in a repository with a ``standards.yml``, unless
    ``VERDOG_STANDARDS_HOOKS`` is ``off``.
    """
    if os.environ.get("VERDOG_STANDARDS_HOOKS") == "off":
        return None
    found = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=folder,
        capture_output=True,
        text=True,
    )
    if found.returncode:
        return None
    root = Path(found.stdout.strip())
    return root if (root / config.NAME).is_file() else None


def edited(payload: dict[str, Any]) -> list[str]:
    """The paths an edit wrote: Claude's ``file_path``, Codex's patch files."""
    tool_input = cast(dict[str, Any], payload.get("tool_input") or {})
    if "file_path" in tool_input:
        return [str(tool_input["file_path"])]
    command = tool_input.get("command", "")
    text = (
        "\n".join(map(str, cast(list[Any], command)))
        if isinstance(command, list)
        else str(command)
    )
    return [m.strip() for m in PATCH_PATH.findall(text)]


def _inside(root: Path, cwd: str, paths: list[str]) -> list[str]:
    """``paths`` relative to ``root``; those outside it are dropped."""
    found: list[str] = []
    for path in paths:
        absolute = (Path(cwd) / path).resolve()
        if absolute.is_relative_to(root.resolve()):
            found.append(absolute.relative_to(root.resolve()).as_posix())
    return found


def post_edit(payload: dict[str, Any]) -> Reply:
    """Fix and lint the edited files; findings go back to the agent.

    The repository is that of the first edited file's folder.
    """
    cwd = payload.get("cwd") or "."
    written = edited(payload)
    folder = (Path(cwd) / written[0]).parent if written else None
    root = _root(folder) if folder and folder.is_dir() else None
    if root is None:
        return Reply()
    paths = _inside(root, cwd, written)
    if not paths:
        return Reply()
    messages = linting.report(
        root, linting.lint(root, paths, fix=True), EDIT_FINDINGS
    )
    return Reply(2, stderr="\n".join(messages)) if messages else Reply()


def _commands(root: Path) -> str:
    """The output of the first project check that fails, or "".

    The setup commands (installs, syncs) are left to ``run`` and CI.
    """
    for name in cli.types_of(root):
        for line in cli.commands_of(root, name, setup=False):
            done = subprocess.run(
                line, shell=True, cwd=root, capture_output=True, text=True
            )
            if done.returncode:
                tail = (done.stdout + done.stderr)[-OUTPUT_CHARS:]
                return f"{name}: `{line}` failed ({done.returncode}):\n{tail}"
    return ""


def _counter(root: Path, session: str) -> Path:
    found = subprocess.run(
        ["git", "rev-parse", "--absolute-git-dir"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    name = re.sub(r"[^\w.-]", "_", session or "session")
    return Path(found.stdout.strip()) / "verdog-standards" / f"stop-{name}"


def stop(
    payload: dict[str, Any], check: Callable[[Path], list[str]] | None = None
) -> Reply:
    """Block the agent's stop while the check fails, at most ``ATTEMPTS`` times.

    ``check`` returns the problems of a repository; by default the findings
    since the merge base with the main branch and the first failing command.
    """
    codex = _codex(payload)
    allow = Reply(stdout="{}" if codex else "")
    root = _root(payload.get("cwd") or ".")
    if root is None:
        return allow
    counter = _counter(root, str(payload.get("session_id", "")))
    attempts = int(counter.read_text()) if counter.is_file() else 0
    if not payload.get("stop_hook_active"):
        attempts = 0  # a new turn
    problems = (check or _problems)(root)
    if not problems:
        counter.unlink(missing_ok=True)
        return allow
    reason = (
        "The standards' check fails; fix every finding and failure, then "
        "finish:\n" + "\n".join(problems)
    )
    if attempts >= ATTEMPTS:
        counter.unlink(missing_ok=True)
        message = f"Stopped after {ATTEMPTS} blocked attempts; {reason}"
        return Reply(stdout=json.dumps({"systemMessage": message}))
    counter.parent.mkdir(parents=True, exist_ok=True)
    counter.write_text(str(attempts + 1))
    if codex:
        return Reply(stdout=json.dumps({"decision": "block", "reason": reason}))
    return Reply(2, stderr=reason)


def _problems(root: Path) -> list[str]:
    found = linting.lint_errors(root, cli.base_of(root))
    failed = _commands(root)
    return found + ([failed] if failed else [])


def respond(reply: Reply) -> int:
    """Print ``reply`` and return its exit code."""
    if reply.stdout:
        print(reply.stdout)
    if reply.stderr:
        print(reply.stderr, file=sys.stderr)
    return reply.code


def run(event: str) -> int:
    """Answer one hook ``event`` (``post-edit`` or ``stop``) from stdin."""
    payload = cast(dict[str, Any], json.loads(sys.stdin.read() or "{}"))
    return respond(
        post_edit(payload) if event == "post-edit" else stop(payload)
    )


MARK: Final = "verdog-standards hook "


def _others(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The hook groups of an event that are not ours."""
    return [
        group
        for group in groups
        if not any(
            MARK in str(h.get("command", "")) for h in group.get("hooks", [])
        )
    ]


def _with_hooks(
    settings: dict[str, Any], command: str, edits: str
) -> dict[str, Any]:
    """``settings`` with our two hooks, replacing earlier copies of them."""
    hooks = cast(dict[str, list[dict[str, Any]]], settings.get("hooks", {}))
    wanted = {
        "PostToolUse": (edits, f"{command} hook post-edit", 300),
        "Stop": ("", f"{command} hook stop", 3600),
    }
    merged = dict(hooks)
    for event, (matcher, line, timeout) in wanted.items():
        ours: dict[str, Any] = {"matcher": matcher} if matcher else {}
        ours["hooks"] = [
            {"type": "command", "command": line, "timeout": timeout}
        ]
        merged[event] = [*_others(hooks.get(event, [])), ours]
    return {**settings, "hooks": merged}


def _without_hooks(settings: dict[str, Any]) -> dict[str, Any]:
    """``settings`` without our hooks; events left empty go too."""
    hooks = cast(dict[str, list[dict[str, Any]]], settings.get("hooks", {}))
    kept = {event: _others(groups) for event, groups in hooks.items()}
    if kept == hooks:
        return settings
    kept = {event: groups for event, groups in kept.items() if groups}
    rest = {key: value for key, value in settings.items() if key != "hooks"}
    return {**rest, "hooks": kept} if kept else rest


def install(
    home: Path, *, claude: bool, codex: bool, remove: bool = False
) -> str:
    """Write the user's hook configurations; return their diff.

    With ``remove``, take exactly our hooks out of them instead.
    """
    found = shutil.which("verdog-standards")
    command = found or str(Path(sys.executable).with_name("verdog-standards"))
    targets = [
        (home / ".claude" / "settings.json", CLAUDE_EDITS, claude),
        (home / ".codex" / "hooks.json", "apply_patch", codex),
    ]
    diff: list[str] = []
    for path, edits, wanted in targets:
        if not wanted or (remove and not path.is_file()):
            continue
        before = path.read_text() if path.is_file() else ""
        settings = cast(dict[str, Any], json.loads(before or "{}"))
        changed = (
            _without_hooks(settings)
            if remove
            else _with_hooks(settings, command, edits)
        )
        if changed == settings:
            continue  # keep the user's own layout
        after = json.dumps(changed, indent=2) + "\n"
        diff += difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            str(path),
            str(path),
        )
        if after != before:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(after)
    return "".join(diff)
