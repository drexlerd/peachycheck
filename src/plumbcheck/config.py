"""``standards.yml``: the organization's file and a repository's overrides.

A file may ``EXTENDS`` others (URLs or paths, resolved first); later files
win, mappings merge key by key, and the keys ``CONFIG_PROPERTIES_TO_APPEND``
names append their lists, as in MegaLinter's configuration. A repository
without a ``standards.yml`` gets the organization's file.
"""

import hashlib
import re
import urllib.request
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Final, cast

import yaml

NAME: Final = "standards.yml"
APPEND: Final = "CONFIG_PROPERTIES_TO_APPEND"
CACHE: Final = Path("~/.cache/verdog-standards/extends")


def data(name: str) -> Path:
    """A data file or folder: packaged next to this module, or at the root."""
    packaged = Path(__file__).parent / name
    return (
        packaged
        if packaged.exists()
        else Path(__file__).resolve().parents[2] / name
    )


ORGANIZATION: Final = data(NAME)
RULES: Final = data("rules")

type Config = dict[str, Any]


def _text(source: str) -> str:
    """A file's text; a URL is fetched once and cached by its address.

    ponytail: cached forever, so EXTENDS should pin a commit; delete the cache
    to refetch a moving ref.
    """
    if not re.match(r"https?://", source):
        return Path(source).read_text()
    cached = CACHE.expanduser() / hashlib.sha256(source.encode()).hexdigest()
    if not cached.is_file():
        with urllib.request.urlopen(source, timeout=30) as response:
            text = cast(bytes, response.read()).decode()
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_text(text)
    return cached.read_text()


def _located(entry: str, parent: str) -> str:
    """``entry`` of ``EXTENDS``, a path relative to the file naming it."""
    if re.match(r"https?://", entry) or Path(entry).is_absolute():
        return entry
    if re.match(r"https?://", parent):
        return parent.rsplit("/", 1)[0] + "/" + entry
    return str(Path(parent).parent / entry)


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
    data = cast(Config, yaml.safe_load(_text(source)) or {})
    extends = data.pop("EXTENDS", [])
    result: Config = {}
    for entry in [extends] if isinstance(extends, str) else extends:
        located = _located(str(entry), source)
        if located not in seen | {source}:  # a cycle is read once
            result = merge(result, resolve(located, seen | {source}))
    return merge(result, data)


def load(root: Path | None = None) -> Config:
    """The configuration of ``root``: its ``standards.yml``, else the org's."""
    own = root / NAME if root else None
    return resolve(str(own if own and own.is_file() else ORGANIZATION))


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
