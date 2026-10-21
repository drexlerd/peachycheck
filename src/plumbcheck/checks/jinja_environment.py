"""Report Jinja environments that keep tag lines in the output.

Run as ``python -m plumbcheck.checks.jinja_environment FILE...``; each
call of jinja2's ``Environment``, ``SandboxedEnvironment`` or ``Template``
(which builds an environment) without both ``trim_blocks=True`` and
``lstrip_blocks=True`` is printed as ``path:line: message``. A call that
passes ``**options`` is not judged.
"""

import ast
import sys
from pathlib import Path
from typing import Final

CLASSES: Final = frozenset({"Environment", "SandboxedEnvironment", "Template"})
FLAGS: Final = ("trim_blocks", "lstrip_blocks")


def _jinja2(module: str | None) -> bool:
    """Whether ``module`` is jinja2 or one of its submodules."""
    return (module or "").split(".")[0] == "jinja2"


def _classes(nodes: list[ast.AST]) -> set[str]:
    """The local names of the classes ``from jinja2 import`` binds."""
    return {
        a.asname or a.name
        for n in nodes
        if isinstance(n, ast.ImportFrom) and _jinja2(n.module)
        for a in n.names
        if a.name in CLASSES
    }


def _modules(nodes: list[ast.AST]) -> set[str]:
    """The local names ``import jinja2[...]`` binds."""
    return {
        a.asname or a.name.split(".")[0]
        for n in nodes
        if isinstance(n, ast.Import)
        for a in n.names
        if _jinja2(a.name)
    }


def _root(node: ast.expr) -> str:
    """The name an attribute chain such as ``jinja2.sandbox.X`` starts at."""
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else ""


def _jinja(call: ast.Call, classes: set[str], modules: set[str]) -> bool:
    """Whether ``call`` builds a jinja2 environment."""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id in classes
    return (
        isinstance(func, ast.Attribute)
        and func.attr in CLASSES
        and _root(func) in modules
    )


def _strips(call: ast.Call) -> bool:
    """Whether ``call`` sets both flags to ``True`` or may via ``**``."""
    given = {k.arg: k.value for k in call.keywords}
    if None in given:
        return True
    return all(
        isinstance(given.get(flag), ast.Constant)
        and getattr(given[flag], "value", None) is True
        for flag in FLAGS
    )


def findings(source: str) -> list[int]:
    """The lines of environment calls in ``source`` that keep tag lines."""
    nodes = list(ast.walk(ast.parse(source)))
    classes, modules = _classes(nodes), _modules(nodes)
    return sorted(
        node.lineno
        for node in nodes
        if isinstance(node, ast.Call)
        and _jinja(node, classes, modules)
        and not _strips(node)
    )


def main(paths: list[str]) -> int:
    """Print the findings of each file in ``paths``; 1 if there are any."""
    found = 0
    for path in paths:
        for line in findings(Path(path).read_text()):
            print(
                f"{path}:{line}: a Jinja environment without "
                "trim_blocks=True and lstrip_blocks=True"
            )
            found += 1
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
