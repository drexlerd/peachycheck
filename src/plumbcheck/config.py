"""``plumbcheck.yml``: plumbcheck's defaults and a repository's overrides.

Every configuration extends plumbcheck's defaults first. A file may
``EXTENDS`` others (URLs or paths, resolved first); later files win, mappings
merge key by key, and the keys ``CONFIG_PROPERTIES_TO_APPEND`` names append
their lists, as in MegaLinter's configuration. A repository without a
``plumbcheck.yml`` gets the defaults.

The paths in ``RULES``, ``GUIDES`` and ``FILES`` resolve against the file
that names them. A file extended by its GitHub raw URL
(``https://raw.githubusercontent.com/<owner>/<repo>/<ref>/<path>``) is read
from a snapshot of its repository at that ref, so its folders resolve too.
"""

import hashlib
import io
import re
import subprocess
import tarfile
import urllib.request
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Final, cast

import yaml

NAME: Final = "plumbcheck.yml"
APPEND: Final = "CONFIG_PROPERTIES_TO_APPEND"
CACHE: Final = Path("~/.cache/plumbcheck/extends")
DEFAULTS: Final = Path(__file__).parent / "defaults" / NAME
# The folder ``sync`` writes the shared base configurations into.
BASE: Final = ".plumbcheck"
# The keys whose values are paths, relative to the file naming them.
PATHS: Final = ("RULES", "GUIDES")
GITHUB_RAW: Final = re.compile(
    r"https://raw\.githubusercontent\.com/([^/]+)/([^/]+)/([^/]+)/(.+)"
)

type Config = dict[str, Any]


def _url(source: str) -> bool:
    return bool(re.match(r"https?://", source))


def _download(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "plumbcheck"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return cast(bytes, response.read())


def _snapshot(owner: str, repo: str, ref: str) -> Path:
    """The files of ``owner/repo`` at ``ref``, downloaded once.

    ponytail: cached forever, so EXTENDS should pin a commit; delete the cache
    to refetch a moving ref. Public repositories only.
    """
    folder = CACHE.expanduser() / owner / repo / ref
    if not folder.is_dir():
        data = _download(
            f"https://codeload.github.com/{owner}/{repo}/tar.gz/{ref}"
        )
        partial = folder.with_name(f".{ref}.partial")
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            archive.extractall(partial, filter="data")
        (top,) = partial.iterdir()  # the archive's one folder, repo-ref/
        top.rename(folder)
        partial.rmdir()
    return folder


def local(source: str) -> str:
    """``source`` as a local path; a GitHub raw URL in its repository's snapshot.

    Other URLs stay URLs.
    """
    found = GITHUB_RAW.fullmatch(source)
    if found:
        owner, repo, ref, path = found.groups()
        return str(_snapshot(owner, repo, ref) / path)
    return source


def _text(source: str) -> str:
    """A file's text; a URL is fetched once and cached by its address."""
    if not _url(source):
        return Path(source).read_text()
    cached = CACHE.expanduser() / hashlib.sha256(source.encode()).hexdigest()
    if not cached.is_file():
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(_download(source))
    return cached.read_text()


def _located(entry: str, parent: str) -> str:
    """``entry``, a path relative to the file ``parent`` that names it."""
    if _url(entry) or Path(entry).is_absolute():
        return entry
    if _url(parent):
        return parent.rsplit("/", 1)[0] + "/" + entry
    return str(Path(parent).parent / entry)


def _anchored(data: Config, source: str) -> Config:
    """``data`` with its relative paths resolved against ``source``."""
    found = dict(data)
    for key in PATHS:
        if isinstance(found.get(key), str):
            found[key] = _located(found[key], source)
    files = cast(dict[str, dict[str, str]], found.get("FILES") or {})
    found_files = {
        kind: {
            target: _located(str(path), source) for target, path in by.items()
        }
        for kind, by in files.items()
    }
    return {**found, "FILES": found_files} if files else found


def merge(base: Config, extra: Config, append: Iterable[str] = ()) -> Config:
    """``extra`` over ``base``: later wins, mappings merge, ``append`` lists join."""
    keys = set(append) | set(base.get(APPEND, ())) | set(extra.get(APPEND, ()))
    merged = dict(base)
    for key, value in extra.items():
        old = merged.get(key)
        if key in keys and isinstance(old, list) and isinstance(value, list):
            merged[key] = [*cast(list[Any], old), *cast(list[Any], value)]
        elif isinstance(old, dict) and isinstance(value, dict):
            merged[key] = merge(
                cast(Config, old), cast(Config, value), keys - {APPEND}
            )
        else:
            merged[key] = value
    return merged


def resolve(source: str, seen: frozenset[str] = frozenset()) -> Config:
    """The configuration of ``source`` with everything it extends."""
    source = local(source)
    data = _anchored(cast(Config, yaml.safe_load(_text(source)) or {}), source)
    extends = data.pop("EXTENDS", [])
    result: Config = {}
    for entry in [extends] if isinstance(extends, str) else extends:
        located = local(_located(str(entry), source))
        if located not in seen | {source}:  # a cycle is read once
            result = merge(result, resolve(located, seen | {source}))
    return merge(result, data)


def load(root: Path | None = None) -> Config:
    """The configuration of ``root``: the defaults and its ``plumbcheck.yml``."""
    defaults = resolve(str(DEFAULTS))
    own = root / NAME if root else None
    if own is None or not own.is_file():
        return defaults
    return merge(defaults, resolve(str(own)))


def rules(found: Config) -> str:
    """The ``RULES`` folder of a configuration, or "" without one."""
    return str(found.get("RULES") or "")


def rule_file(root: Path, name: str) -> Path | None:
    """The configuration file ``name`` of a linter in the repository ``root``.

    The repository's own (which inherits the base natively), else the base
    in ``.plumbcheck/``, else none: the tool's own defaults.
    """
    candidates = (root / name, root / BASE / name)
    return next((c for c in candidates if c.is_file()), None)


def _toplevel(folder: Path) -> Path | None:
    found = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=folder,
        capture_output=True,
        text=True,
    )
    return Path(found.stdout.strip()) if found.returncode == 0 else None


def owner(path: Path) -> Path | None:
    """The repository a file belongs to, or None when plumbcheck skips it.

    It is the nearest folder above the file with a plumbcheck.yml, up to the
    file's git toplevel.
    """
    folder = path if path.is_dir() else path.parent
    while not folder.is_dir():
        folder = folder.parent  # a deleted file's folder may be gone too
    top = _toplevel(folder)
    if top is None:
        return None
    for candidate in [folder, *folder.parents]:
        if (candidate / NAME).is_file():
            return candidate
        if candidate == top:
            return None
    return None


def grouped(paths: Iterable[Path]) -> dict[Path, list[str]]:
    """Absolute ``paths`` by their repository, relative to it; others dropped."""
    found: dict[Path, list[str]] = {}
    for path in paths:
        root = owner(path.resolve())
        if root is not None:
            relative = path.resolve().relative_to(root.resolve()).as_posix()
            found.setdefault(root, []).append(relative)
    return found


def linters(config: Config) -> dict[str, Config]:
    """The linters that run, with the repository's overrides applied."""
    enabled = config.get("ENABLE_LINTERS")
    disabled = set(config.get("DISABLE_LINTERS", ()))
    found: dict[str, Config] = {}
    for name, spec in cast(
        dict[str, Config], config.get("LINTERS", {})
    ).items():
        if not spec.get("command") or name in disabled:
            continue
        if enabled is not None and name not in enabled:
            continue
        arguments = config.get(f"{name}_ARGUMENTS", [])
        found[name] = {
            **spec,
            "arguments": [arguments]
            if isinstance(arguments, str)
            else arguments,
            "include": config.get(f"{name}_FILTER_REGEX_INCLUDE", ""),
            "exclude": [
                r
                for r in (
                    config.get("FILTER_REGEX_EXCLUDE", ""),
                    config.get(f"{name}_FILTER_REGEX_EXCLUDE", ""),
                )
                if r
            ],
        }
    return found
