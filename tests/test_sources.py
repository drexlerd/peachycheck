"""The guides' manifest, parsers, trees and their completeness."""

import re
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from verdog_standards import engine
from verdog_standards import sources as s

RULE_1 = """### <a name="rr-raii"></a>R.1: Hold resources in handles

##### Reason

To avoid leaks.

##### Example, bad

    void f() { int* p = new int; }

##### Example, good

    void f() { auto p = std::make_unique<int>(); }

##### Enforcement

Flag every `new`."""
CORE = f"""# <a name="s-resource"></a>R: Resource management

Rules about resources.

* Resource management rule summary:

  * [R.1: Hold resources in handles](#rr-raii)
  * [R.20: Give an owner its own type](#rr-owner)

{RULE_1}

## <a name="ss-smart"></a>R.smart: Smart pointers

### <a name="rr-owner"></a>R.20: Give an owner its own type

##### Reason

They prevent leaks.
"""
PYTHON = """# Guide

<a id="s1-rules"></a>
## 1 Rules

<a id="s1.1-lint"></a>
### 1.1 Lint

#### 1.1.1 Pros

Finds bugs.

#### 1.1.2 Decision

Run the linter. Fix what it finds.

```python
# Not a heading.
```

### 1.3 Imports

Import modules.
"""


def _source(
    tmp_path: Path,
    parser: str,
    files: dict[str, str],
    levels: tuple[int, ...] = (),
) -> tuple[s.Source, Path]:
    copy = tmp_path / s.SOURCE
    for name, text in files.items():
        (copy / name).parent.mkdir(parents=True, exist_ok=True)
        (copy / name).write_text(text)
    source = s.Source(
        id="guide",
        title="Guide",
        language="x",
        store="commit",
        parser=parser,
        license="CC-BY-3.0",
        link="https://example.org/guide",
        files=tuple(files),
        levels=levels,
    )
    return source, copy


def test_a_core_guidelines_rule_keeps_its_complete_text_in_its_leaf(
    tmp_path: Path,
) -> None:
    source, copy = _source(
        tmp_path, "core-guidelines", {"g.md": CORE}, levels=(1, 2, 3)
    )
    files, rules = s.render(source, copy, {})
    by_id = {r.id: r for r in rules}
    raii = by_id["guide/R.1"]
    assert raii.path == "R/R.1.md" and raii.section == (
        "R: Resource management",
    )
    assert RULE_1 in files[raii.path]
    assert raii.link == "https://example.org/guide#rr-raii"
    assert raii.quoted_bad == ("void f() { int* p = new int; }",)
    assert raii.quoted_good == (
        "void f() { auto p = std::make_unique<int>(); }",
    )
    assert by_id["guide/R.20"].path == "R/smart/R.20.md"
    assert s.depth(rules) == 3
    # Every level lists its children, each with the path of its file.
    assert (
        "- R: Resource management — 2 rules, 0 checked → R/index.md"
        in files[s.INDEX]
    )
    section = files["R/index.md"]
    assert "- R.1: Hold resources in handles [prose] → R/R.1.md" in section
    assert (
        "- R.smart: Smart pointers — 1 rule, 0 checked → R/smart/index.md"
        in section
    )
    for rule in rules:
        assert rule.path in files
    s.write(tmp_path, files)
    assert s.read_index(tmp_path) == rules
    assert s.stale(tmp_path, files) == []


def test_a_rule_missing_from_its_section_s_summary_fails(
    tmp_path: Path,
) -> None:
    broken = CORE.replace(
        '### <a name="rr-owner"></a>R.20:', 'Text <a name="rr-owner"></a>R.20:'
    )
    source, copy = _source(
        tmp_path, "core-guidelines", {"g.md": broken}, levels=(1, 2, 3)
    )
    with pytest.raises(
        ValueError, match=r"R\.20 \(#rr-owner\) is listed but has no leaf"
    ):
        s.render(source, copy, {})


def test_a_google_guide_numbers_its_rules_without_gaps(tmp_path: Path) -> None:
    source, copy = _source(
        tmp_path, "google-markdown", {"g.md": PYTHON}, levels=(2, 3)
    )
    with pytest.raises(ValueError, match=r"section 1\.2 is missing"):
        s.render(source, copy, {})
    known = replace(source, gaps=("1.2",))
    files, rules = s.render(known, copy, {})
    lint = next(r for r in rules if r.id == "guide/1.1")
    assert lint.text == "1.1 Lint: Run the linter."
    assert lint.path == "1-rules/1.1-lint.md" and s.depth(rules) == 2
    assert lint.link == "https://example.org/guide#s1.1-lint"
    assert (
        "#### 1.1.1 Pros" in files[lint.path]
        and "# Not a heading." in files[lint.path]
    )


def test_an_html_guide_becomes_markdown_leaves(tmp_path: Path) -> None:
    html = (
        "<h1>Guide</h1><p>About.</p>"
        '<h2 id="features" class="numbered">Features</h2>'
        '<h3 id="vars" class="numbered">Variables</h3>'
        '<p>Use <code>const</code>, see <a href="#other">other</a>.</p>'
        '<h4 id="let" class="numbered">Let</h4><pre>let x = 1;\nx = 2;</pre>'
    )
    source, copy = _source(
        tmp_path, "google-html", {"g.html": html}, levels=(2, 3)
    )
    files, rules = s.render(source, copy, {})
    (leaf,) = [r for r in rules if r.leaf]
    assert leaf.path == "features/vars.md"
    text = files[leaf.path]
    assert "Use `const`, see [other](https://example.org/guide#other)." in text
    assert "#### Let\n\n```\nlet x = 1;\nx = 2;\n```" in text


def test_diataxis_needs_all_four_kinds(tmp_path: Path) -> None:
    kinds = {
        f"{k}.rst": f"{k.title()}\n=====\n\nText.\n" for k in s.DIATAXIS_KINDS
    }
    source, copy = _source(
        tmp_path, "diataxis", {"index.rst": "Home\n====\n", **kinds}
    )
    _, rules = s.render(source, copy, {})
    assert [r.id for r in rules] == [f"guide/{k}" for k in s.DIATAXIS_KINDS]
    del kinds["reference.rst"]
    source, copy = _source(
        tmp_path / "less", "diataxis", {"index.rst": "", **kinds}
    )
    with pytest.raises(ValueError, match="the kind reference is missing"):
        s.render(source, copy, {})


def test_a_copy_must_match_its_pinned_sha256(tmp_path: Path) -> None:
    source, copy = _source(tmp_path, "markdown", {"a.md": "# A\n\n## B\n"})
    pinned = replace(source, sha256=s.digest(copy))
    assert s.fetch(pinned, tmp_path) == copy
    (copy / "a.md").write_text("# A\n\n## C\n")
    with pytest.raises(ValueError, match="not the pinned"):
        s.fetch(pinned, tmp_path)


def test_every_committed_tree_is_generated_and_no_cached_guide_is_committed() -> (
    None
):
    assert engine.source_errors() == []
    found = engine.guides()
    assert {g.store for g in found.values()} == {"commit", "cache"}
    for source in found.values():
        folder = engine.tree(source)
        for name in ("index.md", "index.toml", "LICENSE", "ATTRIBUTION.md"):
            assert (folder / name).is_file(), (source.id, name)
    rules = s.read_index(engine.tree(found["cpp-core-guidelines"]))
    assert s.depth(rules) == 3
    assert s.depth(s.read_index(engine.tree(found["google-python"]))) == 2
    # No text of the cached guide is committed: not its files, nor its titles.
    repository = engine.STANDARDS.parent
    tracked = subprocess.run(
        ["git", "-C", str(repository), "ls-files"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    # A rule's title after its id, such as "Use `unique_ptr` ...", from R.20.
    titles = {r.title.partition(": ")[2] for r in rules} - {""}
    quoted = re.compile("|".join(re.escape(t) for t in titles if len(t) > 24))
    # Only paths: a failure must not print the guide's text in a public log.
    found = [
        path
        for path in tracked
        if "cpp-core-guidelines" in Path(path).parts
        or (
            (repository / path).is_file()
            and quoted.search(_text(repository / path))
        )
    ]
    assert found == []


def _text(path: Path) -> str:
    return path.read_text(errors="ignore") if path.stat().st_size < 4e6 else ""
