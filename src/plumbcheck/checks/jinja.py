"""Report Jinja template lines with more than one block tag.

Run as ``python -m verdog_standards.checks.jinja FILE...``; each finding is
printed as ``path:line: message``. Comments ``{# ... #}`` and expressions
``{{ ... }}`` do not count, and a construct such as ``{% if %}...{% endif %}``
that opens and closes inside one line of prose, with text outside it, is
allowed.
"""

import re
import sys
from pathlib import Path
from typing import Final

COMMENT: Final = re.compile(r"\{#.*?#\}", re.S)
EXPRESSION: Final = re.compile(r"\{\{.*?\}\}", re.S)
TAG: Final = re.compile(r"\{%-?\s*(\w*)(.*?)[-+]?%\}", re.S)
OPENERS: Final = frozenset(
    {"if", "for", "macro", "call", "filter", "block", "with", "autoescape"}
)
MIDDLES: Final = frozenset({"elif", "else"})


def _blank(match: re.Match[str], fill: str = " ") -> str:
    """``match`` with every character but newlines replaced by ``fill``."""
    return re.sub(r"[^\n]", fill, match.group())


def _depth(depth: int, name: str) -> int:
    """The nesting after tag ``name`` at ``depth``; -1 if it may not nest."""
    if name in OPENERS:
        return depth + 1
    if name.startswith("end") and depth > 0:
        return depth - 1
    return depth if name in MIDDLES and depth > 0 else -1


def _inline(tags: list[re.Match[str]], plain: str) -> bool:
    """Whether ``tags`` form constructs closed inside a line of prose.

    The line needs text outside every construct, as in
    ``- {{ x }}{% if y %} (y){% endif %}``.
    """
    depth, outside = 0, False
    position = plain.rfind("\n", 0, tags[0].start()) + 1
    for tag in tags:
        outside |= depth == 0 and bool(plain[position : tag.start()].strip())
        depth = _depth(depth, tag.group(1))
        if depth < 0:
            return False
        position = tag.end()
    end = plain.find("\n", position)
    rest = plain[position : end if end >= 0 else len(plain)]
    return depth == 0 and (outside or bool(rest.strip()))


def findings(text: str) -> list[int]:
    """The 1-based lines of ``text`` with more than one block tag."""
    plain = COMMENT.sub(_blank, text)
    plain = EXPRESSION.sub(lambda m: _blank(m, "x"), plain)
    by_line: dict[int, list[re.Match[str]]] = {}
    for tag in TAG.finditer(plain):
        by_line.setdefault(plain.count("\n", 0, tag.start()), []).append(tag)
    return [
        number + 1
        for number, tags in sorted(by_line.items())
        if len(tags) > 1 and not _inline(tags, plain)
    ]


def main(paths: list[str]) -> int:
    """Print the findings of each file in ``paths``; 1 if there are any."""
    found = 0
    for path in paths:
        for line in findings(Path(path).read_text()):
            print(f"{path}:{line}: more than one block tag on a line")
            found += 1
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
