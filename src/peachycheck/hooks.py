"""The Claude Code and Codex hooks: lint while the agent works.

``post-edit`` lints, after fixing, the files an edit touched; ``stop`` lints
the files changed since the merge base before the agent finishes, and blocks
at most ``ATTEMPTS`` times in a row. Builds and tests are left to the
repository's own CI. Both read the hook's JSON on stdin; the CLI is told apart by its
input (Codex sends a ``turn_id`` and edits with ``apply_patch``).

Each file counts for its own repository, the nearest folder above it with a
``peachycheck.yml``: an edit may span repositories, and ``stop`` checks every
repository with changes at or below the session's folder. Elsewhere the
hooks do nothing, so they can be installed for the whole user;
``PEACHYCHECK_HOOKS=off`` turns them off.
A repository whose ``.venv`` has another version of peachycheck answers
with that one, so its pin wins over the user's install.
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

from peachycheck import cli, config, linting

ATTEMPTS: Final = 2  # blocked stops in a row before the agent may stop
EDIT_FINDINGS: Final = 20  # shown after an edit
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


def _off() -> bool:
    return os.environ.get("PEACHYCHECK_HOOKS") == "off"


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


def _edited_roots(payload: dict[str, Any]) -> dict[Path, list[str]]:
    """The edited files by their repository; files of none are dropped.

    A Codex patch may span repositories; each file counts for its own.
    """
    if _off():
        return {}
    cwd = Path(payload.get("cwd") or ".")
    return config.grouped(cwd / path for path in edited(payload))


def _shown(root: Path, where: Path, messages: list[str]) -> list[str]:
    """``messages`` of ``root``, under its path when it is not ``where``."""
    prefix = os.path.relpath(root, where)
    if not messages or prefix == ".":
        return messages
    return [f"In {prefix}/:", *messages]


def post_edit(payload: dict[str, Any]) -> Reply:
    """Fix and lint the edited files; findings go back to the agent.

    Each file is linted with the repository it belongs to.
    """
    where = Path(payload.get("cwd") or ".").resolve()
    messages: list[str] = []
    for root, paths in _edited_roots(payload).items():
        found = linting.lint(root, paths, fix=True)
        messages += _shown(root, where, linting.report(found, EDIT_FINDINGS))
    return Reply(2, stderr="\n".join(messages)) if messages else Reply()


COUNTERS: Final = Path("~/.cache/peachycheck/stop")
SEARCH_DEPTH: Final = 3  # of the folders below the session's for repositories


def _counter(session: str) -> Path:
    name = re.sub(r"[^\w.-]", "_", session or "session")
    return COUNTERS.expanduser() / name


def roots(folder: Path) -> list[Path]:
    """The repositories the Stop hook checks for a session in ``folder``.

    Its own repository and those below it (side-by-side worktrees under one
    parent, say), each with a ``peachycheck.yml`` and changes since its base.
    """
    if _off():
        return []
    found = {r for r in [config.owner(folder)] if r}
    for path, folders, files in os.walk(folder):
        depth = len(Path(path).relative_to(folder).parts)
        folders[:] = [
            f
            for f in folders
            if depth < SEARCH_DEPTH
            and not f.startswith(".")
            and f != "node_modules"
        ]
        if config.NAME in files and config.owner(Path(path)) == Path(path):
            found.add(Path(path))
    return sorted(r for r in found if _changed(r))


def _changed(root: Path) -> bool:
    try:
        return bool(linting.changed_files(root, cli.base_of(root)))
    except subprocess.CalledProcessError:
        return False


def stop(
    payload: dict[str, Any], check: Callable[[Path], list[str]] | None = None
) -> Reply:
    """Block the agent's stop while the check fails, at most ``ATTEMPTS`` times.

    The check runs in every repository ``roots`` finds. ``check`` returns the
    problems of one; by default the findings since the merge base with the
    main branch.
    """
    codex = _codex(payload)
    allow = Reply(stdout="{}" if codex else "")
    where = Path(payload.get("cwd") or ".").resolve()
    found = roots(where)
    if not found:
        return allow
    counter = _counter(str(payload.get("session_id", "")))
    attempts = int(counter.read_text()) if counter.is_file() else 0
    if not payload.get("stop_hook_active"):
        attempts = 0  # a new turn
    problems = [
        line
        for root in found
        for line in _shown(root, where, (check or _problems)(root))
    ]
    if not problems:
        counter.unlink(missing_ok=True)
        return allow
    reason = (
        "peachycheck's lint fails; fix every finding, then "
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
    return linting.lint_errors(root, cli.base_of(root))


def respond(reply: Reply) -> int:
    """Print ``reply`` and return its exit code."""
    if reply.stdout:
        print(reply.stdout)
    if reply.stderr:
        print(reply.stderr, file=sys.stderr)
    return reply.code


def run(event: str) -> int:
    """Answer one hook ``event`` (``post-edit`` or ``stop``) from stdin."""
    text = sys.stdin.read()
    payload = cast(dict[str, Any], json.loads(text or "{}"))
    pinned = _pinned_answer(event, payload, text)
    if pinned is not None:
        return pinned
    try:
        reply = post_edit(payload) if event == "post-edit" else stop(payload)
    except Exception as error:  # A hook must never crash the agent.
        reply = failed(payload, event, error)
    return respond(reply)


def pinned(root: Path) -> Path | None:
    """The ``peachycheck`` of ``root``'s ``.venv``, if another version.

    ponytail: reads only ``.venv``; a moved UV_PROJECT_ENVIRONMENT is missed.
    """
    command = root / ".venv" / "bin" / "peachycheck"
    found = sorted(
        root.glob(".venv/lib/python*/site-packages/peachycheck-*.dist-info")
    )
    if not found or not command.is_file():
        return None
    version = found[-1].name.removeprefix("peachycheck-")
    return None if version == f"{cli.version()}.dist-info" else command


def _pinned_answer(
    event: str, payload: dict[str, Any], text: str
) -> int | None:
    """Answer with the repository's own version; None to answer here.

    Only its answers (0, or 2 to block) count; a crash or anything else falls
    back to this version.
    """
    try:
        found = (
            list(_edited_roots(payload))
            if event == "post-edit"
            else roots(Path(payload.get("cwd") or ".").resolve())
        )
        # ponytail: only a single repository answers with its own version
        root = found[0] if len(found) == 1 else None
        command = pinned(root) if root else None
        if command is None:
            return None
        done = subprocess.run(
            [str(command), "hook", event],
            input=text,
            cwd=root,
            capture_output=True,
            text=True,
        )
    except Exception:  # A hook must never crash the agent.
        return None
    if done.returncode not in (0, 2):
        return None
    return respond(
        Reply(
            done.returncode, done.stdout.rstrip("\n"), done.stderr.rstrip("\n")
        )
    )


def failed(payload: dict[str, Any], event: str, error: Exception) -> Reply:
    """Report a failure of the hook itself without blocking the agent."""
    message = f"peachycheck hook {event} failed: {error!r}"
    if _codex(payload) and event == "stop":
        return Reply(stdout=json.dumps({"systemMessage": message}))
    return Reply(0 if _codex(payload) else 1, stderr=message)


MARK: Final = "peachycheck hook "
# The tool's name until 2026-10-24: installing replaces those hooks too.
FORMER_MARK: Final = "plumbcheck hook "


def _ours(hook: dict[str, Any]) -> bool:
    command = str(hook.get("command", ""))
    return MARK in command or FORMER_MARK in command


def _others(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The hook groups of an event that are not ours."""
    return [
        group
        for group in groups
        if not any(_ours(h) for h in group.get("hooks", []))
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
    found = shutil.which("peachycheck")
    command = found or str(Path(sys.executable).with_name("peachycheck"))
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
