"""The engine's own checkers: Jinja block tags and Jinja environments."""

from pathlib import Path

import pytest

from plumbcheck.checks import jinja, jinja_environment


def test_a_line_with_more_than_one_block_tag_is_reported() -> None:
    template = (
        '{%- set m = "a" %}{% if m %}A{% else %}B{% endif %}\n'
        "{% for x in xs %}\n"
        "  {% if x %}\n"
        "- {{ x }}{% if x.note %} ({{ x.note }}){% endif %}\n"
        "  {% endif %}\n"
        "{% endfor %}\n"
        "{# {% if %}{% endif %} #}{{ '{% a %}{% b %}' }}\n"
        "{% if a %}{% endif %}\n"
        "{% include 'x' %} text {% include 'y' %}\n"
        "{% if a %}one{% elif b %}two{% endif %}\n"
        "Done{% if a %} once{% else %} twice{% endif %}.\n"
    )
    assert jinja.findings(template) == [1, 8, 9, 10]


def test_a_multi_line_comment_keeps_the_line_numbers() -> None:
    assert jinja.findings("{#\n\n#}\n{% if a %}{% endif %}\n") == [4]


def test_an_environment_must_strip_tag_lines() -> None:
    source = (
        "import jinja2\n"
        "import jinja2.sandbox as sandbox\n"
        "from jinja2 import Environment, Template as T\n"
        "from string import Template\n"
        "a = jinja2.Environment()\n"
        "b = Environment(trim_blocks=True)\n"
        "c = Environment(trim_blocks=True, lstrip_blocks=True)\n"
        "d = Environment(**options)\n"
        "e = Environment(trim_blocks=True, lstrip_blocks=False)\n"
        "f = T('{{ x }}')\n"
        "g = Template('$x')\n"
        "h = sandbox.SandboxedEnvironment()\n"
        "i = other.Environment()\n"
    )
    assert jinja_environment.findings(source) == [5, 6, 9, 10, 12]


def test_the_checkers_print_findings_and_fail(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    template = tmp_path / "t.md.j2"
    template.write_text("{% if a %}{% endif %}\n")
    module = tmp_path / "m.py"
    module.write_text("import jinja2\njinja2.Environment()\n")
    assert jinja.main([str(template)]) == 1
    assert jinja_environment.main([str(module)]) == 1
    assert capsys.readouterr().out == (
        f"{template}:1: more than one block tag on a line\n"
        f"{module}:2: a Jinja environment without trim_blocks=True and "
        "lstrip_blocks=True\n"
    )
    module.write_text("")
    assert jinja_environment.main([str(module)]) == 0
