"""The guides guidelines derive from: manifest, pinned copies and their trees.

The ``sources.toml`` of the GUIDES folder names each guide with its upstream,
pin, checksum, license, attribution, store and parser. A ``commit`` source keeps its pinned
copy (``source/``) and its generated tree in the repository; a ``cache``
source, whose license allows no public copy, is downloaded into
``~/.cache/plumbcheck/<id>/<pin>/`` and its tree is generated there only.

A tree mirrors the guide's own nesting: one ``index.md`` per level with one
line per child, one leaf per rule holding the rule's complete text verbatim,
and ``index.toml`` with the same tree for scripts. Each parser checks that the
tree is complete against the source, or the generator fails.
"""

import hashlib
import html
import json
import re
import tomllib
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Final, cast, override

MANIFEST: Final = "sources.toml"
SOURCE: Final = "source"  # the folder of a tree that holds the pinned copy
INDEX: Final = "index.md"
INDEX_TOML: Final = "index.toml"
STORES: Final = ("commit", "cache")
SOURCE_FIELDS: Final = {
    "title": str,
    "language": str,
    "url": str,
    "link": str,
    "pin": str,
    "sha256": str,
    "license": str,
    "license_url": str,
    "license_text": str,
    "attribution": str,
    "store": str,
    "parser": str,
    "files": list,
    "levels": list,
    "gaps": list,
}
# A Markdown heading, its ``<a name|id>`` anchors and ``{#id}`` attribute.
HEADING: Final = re.compile(r"^(#{1,6})\s+(.*?)\s*(?:\{#([\w-]+)\})?\s*$")
ANCHOR: Final = re.compile(r'<a (?:name|id)="([^"]+)"\s*>\s*</a>')
ANCHOR_LINE: Final = re.compile(
    r'^\s*(?:<a (?:name|id)="[^"]+"\s*>\s*</a>\s*)+$'
)
FENCE: Final = re.compile(r"^\s*(```|~~~)")
HTML_HEADING: Final = re.compile(r"<h([1-6])\b([^>]*)>(.*?)</h\1>", re.S)
# A rule of the C++ Core Guidelines: ``R.20: ...``, ``SL.con.1: ...``.
CORE_RULE: Final = re.compile(r"^([A-Za-z]+(?:\.[\w+-]+)*\.\d+):")
CORE_LISTED: Final = re.compile(
    r"^\s*\* \[([A-Za-z]+(?:\.[\w+-]+)*\.\d+):[^\]]*\]\(#([^)\s]+)\)", re.M
)
ID: Final = re.compile(r"^([A-Za-z][\w.+-]*):")
NUMBERED: Final = re.compile(r"^(\d+(?:\.\d+)*)\s+(.*)$")
RST_LINE: Final = re.compile(r'^([=\-~^"`+*#])\1{2,}\s*$')
DIATAXIS_KINDS: Final = (
    "tutorials",
    "how-to-guides",
    "reference",
    "explanation",
)


@dataclass(frozen=True, slots=True)
class Source:
    """One guide of ``sources.toml``."""

    id: str
    title: str
    language: str
    store: str
    parser: str
    license: str
    url: str = ""
    link: str = ""
    pin: str = ""
    sha256: str = ""
    license_url: str = ""
    license_text: str = ""
    attribution: str = ""
    files: tuple[str, ...] = ()
    levels: tuple[int, ...] = ()
    gaps: tuple[str, ...] = ()


@dataclass(slots=True)
class Node:
    """A level of a guide's tree; a node without children is a rule's leaf."""

    id: str
    title: str
    anchor: str = ""
    text: str = ""
    level: int = 0
    children: list["Node"] = field(default_factory=list["Node"])
    path: str = ""
    link: str = ""  # when not the source's link and the anchor

    def walk(self) -> Iterator["Node"]:
        """This node and every node below it, depth first."""
        yield self
        for child in self.children:
            yield from child.walk()

    def leaves(self) -> list["Node"]:
        """The rules below this node."""
        return [n for n in self.walk() if not n.children and n is not self]


@dataclass(frozen=True, slots=True)
class Head:
    """A heading that opens a node, and the text up to the next one."""

    level: int
    title: str
    anchor: str
    text: str


Parser = Callable[[Source, Path], tuple[Node, list[str]]]


def manifest(standards: Path) -> dict[str, Source]:
    """The sources of ``sources.toml`` in ``standards``; ``ValueError`` if invalid."""
    path = standards / MANIFEST
    tables = tomllib.loads(path.read_text()) if path.is_file() else {}
    found: dict[str, Source] = {}
    for name, table in tables.items():
        errors = source_errors(name, table)
        if errors:
            raise ValueError(f"{path}: " + "; ".join(errors))
        found[name] = Source(id=name, **_tupled(table))
    return found


def _tupled(table: Mapping[str, Any]) -> dict[str, Any]:
    """A TOML table with its arrays as tuples, for a frozen dataclass."""
    return {
        k: tuple(cast(list[Any], v)) if isinstance(v, list) else v
        for k, v in table.items()
    }


def source_errors(name: str, table: Mapping[str, Any]) -> list[str]:
    """What a table of ``sources.toml`` lacks or gets wrong."""
    errors = [
        f"{name}: unknown field {k!r}" for k in table if k not in SOURCE_FIELDS
    ]
    errors += [
        f"{name}: {k} must be a {SOURCE_FIELDS[k].__name__}"
        for k, v in table.items()
        if k in SOURCE_FIELDS and not isinstance(v, SOURCE_FIELDS[k])
    ]
    for required in ("title", "language", "store", "parser", "license"):
        if not table.get(required):
            errors.append(f"{name}: a source needs a {required}")
    if table.get("store") not in STORES:
        errors.append(f"{name}: store must be one of {STORES}")
    if table.get("parser") not in PARSERS:
        errors.append(f"{name}: parser must be one of {sorted(PARSERS)}")
    return errors


def tree_folder(source: Source, standards: Path, cache: Path) -> Path:
    """Where ``source``'s tree lives: the language's references, or the cache."""
    if source.store == "cache":
        return cache.expanduser() / source.id / (source.pin or "unpinned")
    return standards / "languages" / source.language / "references" / source.id


def digest(folder: Path) -> str:
    """The sha256 of a pinned copy: of its files' sha256 and paths, sorted."""
    listing = "".join(
        f"{hashlib.sha256(p.read_bytes()).hexdigest()}  "
        f"{p.relative_to(folder).as_posix()}\n"
        for p in sorted(folder.rglob("*"))
        if p.is_file()
    )
    return hashlib.sha256(listing.encode()).hexdigest()


def _download(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "plumbcheck"})
    with urllib.request.urlopen(request, timeout=60) as response:
        data: bytes = response.read()
    return data


def _fetch_files(source: Source, folder: Path) -> None:
    for name in source.files:
        target = folder / name
        if not target.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(
                _download(source.url.format(pin=source.pin) + name)
            )


def fetch(source: Source, folder: Path) -> Path:
    """The pinned copy of ``source`` in ``folder``, downloaded if missing.

    Raises:
        ValueError: The copy does not match the pinned sha256.
    """
    copy = folder / SOURCE
    if not copy.is_dir() or not any(copy.iterdir()):
        copy.mkdir(parents=True, exist_ok=True)
        FETCHERS.get(source.parser, _fetch_files)(source, copy)
    found = digest(copy)
    if source.sha256 and found != source.sha256:
        raise ValueError(
            f"{source.id}: the copy in {copy} has sha256 {found}, "
            f"not the pinned {source.sha256}"
        )
    return copy


def slug(title: str) -> str:
    """A title as lowercase words joined by hyphens."""
    plain = re.sub(r"<[^>]+>|\{#[^}]*\}|[`*]", "", title).lower()
    return re.sub(r"[^a-z0-9]+", "-", plain).strip("-")


def _plain(title: str) -> str:
    """A heading's title without anchors, attributes or tags."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>|\{#[^}]*\}", "", title)).strip()


# Markdown -----------------------------------------------------------------


def _markdown_heads(text: str, levels: Sequence[int]) -> tuple[str, list[Head]]:
    """The text before the first tree heading, and each tree heading's text.

    Headings inside fenced code are code. Anchor lines right above a heading
    belong to it.
    """
    lines = text.splitlines()
    starts: list[tuple[int, int, str, str]] = []
    for i, found in _headings(lines):
        if len(found[1]) in levels:
            start = _with_anchors(lines, i)
            names = ANCHOR.findall("\n".join(lines[start : i + 1]))
            anchor = found[3] or (names[0] if names else "") or slug(found[2])
            starts.append((start, len(found[1]), _plain(found[2]), anchor))
    ends = [s for s, *_ in starts[1:]] + [len(lines)]
    preamble = "\n".join(lines[: starts[0][0]] if starts else lines)
    return preamble, [
        Head(level, title, anchor, "\n".join(lines[start:end]).strip("\n"))
        for (start, level, title, anchor), end in zip(starts, ends, strict=True)
    ]


def _headings(lines: Sequence[str]) -> Iterator[tuple[int, re.Match[str]]]:
    """Each Markdown heading with its line, skipping fenced code."""
    fenced = False
    for i, line in enumerate(lines):
        if FENCE.match(line):
            fenced = not fenced
        elif not fenced and (found := HEADING.match(line)):
            yield i, found


def _with_anchors(lines: list[str], i: int) -> int:
    """The first line of the anchors and blank lines right above line ``i``."""
    start = i
    while start > 0 and (
        not lines[start - 1].strip() or ANCHOR_LINE.match(lines[start - 1])
    ):
        start -= 1
    while start < i and not lines[start].strip():
        start += 1
    return start


def _counted(text: str, levels: Sequence[int]) -> int:
    """Headings of ``levels`` outside fenced code, counted line by line."""
    return sum(len(h[1]) in levels for _, h in _headings(text.splitlines()))


def _nest(
    root: Node, heads: Sequence[Head], ids: Callable[[Head], str]
) -> Node:
    """Hang each head below the nearest preceding head of a lower level."""
    stack = [root]
    used: set[str] = set()
    for head in heads:
        while len(stack) > 1 and stack[-1].level >= head.level:
            stack.pop()
        name = ids(head)
        if name in used:
            name = (
                head.anchor
                if head.anchor not in used
                else f"{name}-{len(used)}"
            )
        used.add(name)
        node = Node(name, head.title, head.anchor, head.text, head.level)
        stack[-1].children.append(node)
        stack.append(node)
    return root


def _missing_headings(
    text: str, levels: Sequence[int], root: Node
) -> list[str]:
    nodes = len(list(root.walk())) - 1
    counted = _counted(text, levels)
    if counted == nodes:
        return []
    return [f"{counted} headings of levels {list(levels)}, but {nodes} nodes"]


def _core_id(head: Head) -> str:
    """``R.20`` or ``R`` from the title, else the anchor (``Discussion: ...``)."""
    found = ID.match(head.title)
    if found and ("." in found[1] or head.level == 1):
        return found[1]
    return head.anchor or slug(head.title)


def _core_guidelines(source: Source, copy: Path) -> tuple[Node, list[str]]:
    """The C++ Core Guidelines: sections, subsections, rules.

    Complete when every rule a section's summary lists, with an anchor in
    the guide, has a leaf, and the rule headings equal the rule leaves.
    """
    text = (copy / source.files[0]).read_text()
    levels = source.levels or (1, 2, 3)
    preamble, heads = _markdown_heads(text, levels)
    root = _nest(Node(source.id, source.title, text=preamble), heads, _core_id)
    problems = _missing_headings(text, levels, root)
    leaves = {n.anchor: n for n in root.leaves()}
    anchors = set(ANCHOR.findall(text))
    problems += [
        f"{rule} (#{anchor}) is listed but has no leaf"
        for rule, anchor in CORE_LISTED.findall(text)
        if anchor in anchors and anchor not in leaves
    ]
    rule_lines = len(
        re.findall(
            r'^#{3} (?:<a name="[^"]*"></a>)?\s*[A-Za-z]+(?:\.[\w+-]+)*\.\d+:',
            text,
            re.M,
        )
    )
    rules = sum(bool(CORE_RULE.match(n.title)) for n in leaves.values())
    if rule_lines != rules:
        problems.append(f"{rule_lines} rule headings, but {rules} rule leaves")
    return root, problems


def _numbered_id(head: Head) -> str:
    found = NUMBERED.match(head.title)
    return found[1] if found else head.anchor


def _google_markdown(source: Source, copy: Path) -> tuple[Node, list[str]]:
    """A numbered Google guide in Markdown, such as the Python guide.

    Complete when each section's rules are numbered without gaps, except the
    ``gaps`` the manifest names (sections removed upstream).
    """
    text = (copy / source.files[0]).read_text()
    levels = source.levels or (2, 3)
    preamble, heads = _markdown_heads(text, levels)
    root = _nest(
        Node(source.id, source.title, text=preamble), heads, _numbered_id
    )
    problems = _missing_headings(text, levels, root)
    for node in root.walk():
        problems += _gaps(node, set(source.gaps))
    return root, problems


def _gaps(node: Node, known: set[str]) -> list[str]:
    """Numbers missing among the children of ``node``."""
    numbers = [
        int(c.id.rsplit(".", 1)[-1])
        for c in node.children
        if NUMBERED.match(c.title)
    ]
    prefix = f"{node.id}." if node.level else ""
    return [
        f"section {prefix}{n} is missing"
        for n in range(1, max(numbers, default=0) + 1)
        if n not in numbers and f"{prefix}{n}" not in known
    ]


def _markdown_pages(source: Source, copy: Path) -> tuple[Node, list[str]]:
    """Markdown pages, such as Verdog's docs: each file's headings."""
    levels = source.levels or (1, 2)
    root = Node(source.id, source.title)
    problems: list[str] = []
    for name in source.files:
        text = (copy / name).read_text()
        _, heads = _markdown_heads(text, levels)
        page = _nest(Node("", ""), heads, lambda h: h.anchor)
        base = f"{source.link}/{Path(name).stem}.html"
        for node in page.walk():
            node.link = f"{base}#{node.anchor}" if node.level > 1 else base
        root.children += page.children
        problems += _missing_headings(text, levels, page)
    return root, problems


# HTML ---------------------------------------------------------------------


class _Markdown(HTMLParser):
    """HTML as Markdown: headings, paragraphs, lists, code, links, tables."""

    OPEN: Final = {
        **{f"h{n}": "\n\n" + "#" * n + " " for n in range(1, 7)},
        **dict.fromkeys(
            ("p", "div", "section", "table", "dl", "figure"), "\n\n"
        ),
        **dict.fromkeys(("ul", "ol", "tr", "br", "dt"), "\n"),
        **dict.fromkeys(("em", "i"), "*"),
        **dict.fromkeys(("strong", "b"), "**"),
        "td": "| ",
        "th": "| ",
        "dd": "\n  ",
        "blockquote": "\n\n> ",
        "hr": "\n\n---\n\n",
    }
    CLOSE: Final = {
        **dict.fromkeys(
            (*(f"h{n}" for n in range(1, 7)), "p", "table"), "\n\n"
        ),
        **dict.fromkeys(
            ("ul", "ol", "div", "section", "dl", "blockquote"), "\n"
        ),
        **dict.fromkeys(("em", "i"), "*"),
        **dict.fromkeys(("strong", "b"), "**"),
        "td": " ",
        "th": " ",
        "tr": "|",
    }
    SKIP: Final = frozenset(
        ("script", "style", "svg", "button", "template", "head")
    )

    def __init__(self, base: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base = base
        self.out: list[str] = []
        self.pre = 0
        self.skip = 0
        self.lists = 0
        self.links: list[str] = []

    @override
    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        """Open ``tag``."""
        if self.skip or tag in self.SKIP:
            self.skip += tag in self.SKIP
            return
        if self.pre:
            return
        self.out.append(
            self._special_start(tag, dict(attrs)) or self.OPEN.get(tag, "")
        )

    def _special_start(self, tag: str, attrs: dict[str, str | None]) -> str:
        if tag == "pre":
            self.pre += 1
            return "\n\n```\n"
        if tag == "li":
            return "\n" + "  " * max(self.lists - 1, 0) + "- "
        if tag in ("ul", "ol"):
            self.lists += 1
        if tag == "code":
            return "`"
        if tag == "a":
            href = attrs.get("href") or ""
            self.links.append(
                urllib.parse.urljoin(self.base, href) if href else ""
            )
            return "[" if href else ""
        return ""

    @override
    def handle_endtag(self, tag: str) -> None:
        """Close ``tag``."""
        if self.skip:
            self.skip -= tag in self.SKIP
            return
        if tag == "pre" and self.pre:
            self.pre -= 1
            self.out.append("\n```\n\n")
        elif not self.pre:
            self.out.append(self._special_end(tag) or self.CLOSE.get(tag, ""))

    def _special_end(self, tag: str) -> str:
        if tag in ("ul", "ol"):
            self.lists = max(self.lists - 1, 0)
        if tag == "code":
            return "`"
        if tag == "a" and self.links:
            href = self.links.pop()
            return f"]({href})" if href else ""
        return ""

    @override
    def handle_data(self, data: str) -> None:
        """Keep text; collapse its whitespace outside ``pre``."""
        if not self.skip:
            self.out.append(data if self.pre else re.sub(r"\s+", " ", data))

    def text(self) -> str:
        """The Markdown, with blank-line runs and edge spaces removed."""
        lines: list[str] = []
        fenced = False
        for line in "".join(self.out).splitlines():
            if line.startswith("```"):
                fenced = not fenced
            lines.append(
                line.rstrip()
                if fenced
                else re.sub(r"^ +(?![-|])", "", line.rstrip())
            )
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip() + "\n"


def markdown(html: str, base: str = "") -> str:
    """``html`` as Markdown; relative links resolve against ``base``."""
    parser = _Markdown(base)
    parser.feed(html)
    parser.close()
    return parser.text()


def _html_heads(
    html: str, levels: Sequence[int], base: str
) -> tuple[str, list[Head]]:
    """The text before the first tree heading, and each tree heading's text."""
    found = [m for m in HTML_HEADING.finditer(html) if int(m[1]) in levels]
    ends = [m.start() for m in found[1:]] + [len(html)]
    preamble = markdown(html[: found[0].start()] if found else html, base)
    return preamble, [
        Head(
            int(m[1]),
            _plain(markdown(m[3])),
            (re.findall(r'id="([^"]+)"', m[2]) or [slug(_plain(m[3]))])[0],
            markdown(html[m.start() : end], base).strip("\n"),
        )
        for m, end in zip(found, ends, strict=True)
    ]


def _google_html(source: Source, copy: Path) -> tuple[Node, list[str]]:
    """A Google guide in HTML, such as TypeScript's: its numbered headings.

    Complete when every ``numbered`` heading of the tree's levels is a node.
    """
    html = (copy / source.files[0]).read_text()
    levels = source.levels or (2, 3)
    preamble, heads = _html_heads(html, levels, source.link)
    root = _nest(
        Node(source.id, source.title, text=preamble), heads, lambda h: h.anchor
    )
    numbered = sum(
        int(m[1]) in levels and "numbered" in m[2]
        for m in HTML_HEADING.finditer(html)
    )
    nodes = len(list(root.walk())) - 1
    problems = (
        []
        if numbered == nodes
        else [f"{numbered} numbered headings, but {nodes} nodes"]
    )
    return root, problems


# The Google developer documentation style guide, a site without sources.
DEVSITE_NAV: Final = re.compile(
    r'devsite-nav-heading">.*?<span class="devsite-nav-text"[^>]*>\s*([^<]*?)\s*</span>'
    r'|<a href="/style(/[\w-]*)?"[^>]*>\s*'
    r'<span class="devsite-nav-text"[^>]*>\s*([^<]*?)\s*</span>',
    re.S,
)
DEVSITE_TITLE: Final = re.compile(
    r'<h1 class="devsite-page-title"[^>]*>(.*?)</h1>', re.S
)
DEVSITE_BODY: Final = re.compile(
    r'<div class="devsite-article-body[^"]*"[^>]*>(.*?)<devsite-(?:recommendations|thumb-rating)',
    re.S,
)
# A summary the site generates with AI, not part of the guide.
DEVSITE_GENERATED: Final = re.compile(
    r"<devsite-key-takeaways-panel.*?</devsite-key-takeaways-panel>", re.S
)


def _fetch_devsite(source: Source, copy: Path) -> None:
    """The site's table of contents and each page's article, as fetched."""
    landing = _download(source.url).decode()
    nav = landing[
        landing.index("<devsite-book-nav") : landing.index(
            "</devsite-book-nav>"
        )
    ]
    toc: list[str] = []
    pages: list[str] = []
    for heading, page, title in DEVSITE_NAV.findall(nav):
        name = (page or "/index").lstrip("/")
        if heading:
            toc.append(f"\n## {html.unescape(heading)}\n")
        elif toc and name not in pages:  # links above the first heading: tabs
            pages.append(name)
            toc.append(f"- [{html.unescape(title)}]({name}.html)")
    (copy / "toc.md").write_text(f"# {source.title}\n" + "\n".join(toc) + "\n")
    for name in pages:
        url = source.url if name == "index" else f"{source.url}/{name}"
        page = _download(url).decode()
        title, body = DEVSITE_TITLE.search(page), DEVSITE_BODY.search(page)
        if title is None or body is None:
            raise ValueError(f"{url}: no page title or article body")
        article = DEVSITE_GENERATED.sub("", body[1]).strip()
        heading = (
            title[1].split("<", 1)[0].strip()
        )  # without the page's buttons
        (copy / f"{name}.html").write_text(f"<h1>{heading}</h1>\n{article}\n")


def _devsite(source: Source, copy: Path) -> tuple[Node, list[str]]:
    """The style guide's sections of its table of contents, then its pages.

    Complete when every page of the table of contents has a leaf.
    """
    root = Node(source.id, source.title)
    section = root
    problems: list[str] = []
    for line in (copy / "toc.md").read_text().splitlines():
        if line.startswith("## "):
            section = Node(slug(line[3:]), line[3:].strip(), level=1)
            root.children.append(section)
        elif found := re.match(r"^- \[(.*)\]\(([\w-]+)\.html\)$", line):
            page = copy / f"{found[2]}.html"
            if not page.is_file():
                problems.append(f"page {found[2]} of the contents is missing")
                continue
            text = markdown(page.read_text(), f"{source.url}/{found[2]}")
            leaf = Node(found[2], found[1], found[2], text, 2)
            leaf.link = (
                source.url
                if found[2] == "index"
                else f"{source.url}/{found[2]}"
            )
            section.children.append(leaf)
    return root, problems


# reStructuredText -----------------------------------------------------------


def _rst_title(text: str) -> str:
    lines = text.splitlines()
    for i, line in enumerate(lines[1:], 1):
        above = lines[i - 1].strip()
        underlined = (
            RST_LINE.match(line) and above and not RST_LINE.match(above)
        )
        if underlined and not above.startswith(".."):
            return above
    return ""


def _diataxis(source: Source, copy: Path) -> tuple[Node, list[str]]:
    """Diátaxis: its home page, then one leaf per kind of documentation.

    Complete when all four kinds are present.
    """
    home = (
        (copy / "index.rst").read_text()
        if (copy / "index.rst").is_file()
        else ""
    )
    root = Node(source.id, source.title, text=home)
    for name in source.files:
        if Path(name).stem == "index":
            continue
        text = (copy / name).read_text()
        stem = Path(name).stem
        kind = Node(stem, _rst_title(text) or stem, stem, text, 1)
        kind.link = f"{source.link}/{stem}/"
        root.children.append(kind)
    kinds = {c.id for c in root.children}
    return root, [
        f"the kind {k} is missing" for k in DIATAXIS_KINDS if k not in kinds
    ]


PARSERS: Final[dict[str, Parser]] = {
    "core-guidelines": _core_guidelines,
    "google-markdown": _google_markdown,
    "google-html": _google_html,
    "devsite": _devsite,
    "diataxis": _diataxis,
    "markdown": _markdown_pages,
}
FETCHERS: Final[dict[str, Callable[[Source, Path], None]]] = {
    "devsite": _fetch_devsite
}


# The tree -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Rule:
    """An entry of a source's index: a section or a rule."""

    id: str
    title: str
    path: str
    parent: str
    leaf: bool
    link: str
    text: str
    section: tuple[str, ...] = ()
    rules: int = 0
    checked: int = 0
    checked_by: tuple[str, ...] = ()
    quoted_bad: tuple[str, ...] = ()
    quoted_good: tuple[str, ...] = ()


def _component(node: Node, parent: Node) -> str:
    """A node's name in its parent's folder: ``smart``, ``2.1-lint``."""
    name = node.id
    if node.children and parent.level and name.startswith(f"{parent.id}."):
        name = name[len(parent.id) + 1 :]
    if re.fullmatch(r"[\d.]+", name):
        rest = NUMBERED.match(node.title)
        name += f"-{slug(rest[2])}" if rest else ""
    return re.sub(r"[^\w.+-]", "-", name)


def _assign_paths(node: Node, folder: str = "") -> None:
    for child in node.children:
        name = _component(child, node)
        if child.children:
            child.path = f"{folder}{name}/{INDEX}"
            _assign_paths(child, f"{folder}{name}/")
        else:
            child.path = f"{folder}{name}.md"


def _rest(node: Node) -> str:
    """The title without its leading id: ``R.20: Use ...`` -> ``Use ...``."""
    for prefix in (f"{node.id}:", f"{node.id} "):
        if node.title.startswith(prefix):
            return node.title[len(prefix) :].strip()
    return node.title


def _prose(lines: Iterator[str]) -> Iterator[str | None]:
    """Each line of prose, and ``None`` for every other line.

    Code, headings, anchors, directives, lists and tables are no prose.
    """
    fenced = False
    for line in lines:
        fence = FENCE.match(line) is not None
        fenced = fenced != fence
        plain = line.strip()
        other = fenced or fence or line.startswith("    ")
        markup = plain.startswith(("#", "<a ", "..", "*", "-", "|"))
        yield None if other or not plain or markup else plain


def _first_sentence(text: str) -> str:
    """The first sentence of the first paragraph of prose."""
    paragraph: list[str] = []
    for line in _prose(iter(text.splitlines())):
        if line is None and paragraph:
            break
        if line is not None:
            paragraph.append(line)
    words = " ".join(paragraph)
    found = re.match(r"(.+?[.!?])(?:\s|$)", words)
    return found[1] if found else words


def summary(node: Node, parser: str) -> str:
    """An entry's text: the rule's title, or its title and a sentence.

    For a guide of prose the sentence is the first of the rule's Decision,
    or of its text.
    """
    if parser == "core-guidelines":
        return node.title
    decision = re.search(r"^#+ (?:[\d.]+ )?Decision\s*$", node.text, re.M)
    body = (
        node.text[decision.end() :]
        if decision
        else node.text.split("\n", 1)[-1]
    )
    sentence = _first_sentence(body)
    return f"{node.title}: {sentence}" if sentence else node.title


def _quoted(node: Node, parser: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Code the guide marks bad and good, quoted and never compiled.

    The C++ guide marks it ``Example, bad``; Python ``badcode``.
    """
    if parser == "core-guidelines":
        blocks = re.findall(
            r"^#{5} Example,? ?(bad|good)?[^\n]*\n((?:\s*\n|    [^\n]*\n)+)",
            node.text + "\n",
            re.M,
        )
        bad = tuple(_dedent(b) for kind, b in blocks if kind == "bad")
        good = tuple(_dedent(b) for kind, b in blocks if kind == "good")
        return bad, good
    marked = re.findall(
        r"<div class='(good|bad)code'></div>\s*```\w*\n(.*?)^```",
        node.text,
        re.M | re.S,
    )
    bad = tuple(code.strip() for kind, code in marked if kind == "bad")
    good = tuple(code.strip() for kind, code in marked if kind == "good")
    return bad, good


def _dedent(block: str) -> str:
    return "\n".join(
        line[4:] for line in block.strip("\n").splitlines()
    ).strip()


def entries(
    source: Source, root: Node, overlay: Mapping[str, tuple[str, ...]]
) -> list[Rule]:
    """The index of ``root``: one entry per node, parents first.

    ``overlay`` holds the checkers the overlay maps to a rule, by key.
    """
    _assign_paths(root)
    found: list[Rule] = []

    def visit(node: Node, parent: str, section: tuple[str, ...]) -> None:
        key = f"{source.id}/{node.id}"
        leaves = node.leaves()
        checked = sum(bool(overlay.get(f"{source.id}/{n.id}")) for n in leaves)
        bad, good = (
            _quoted(node, source.parser) if not node.children else ((), ())
        )
        found.append(
            Rule(
                id=key,
                title=node.title,
                path=node.path,
                parent=parent,
                leaf=not node.children,
                link=node.link or _link(source, node),
                text=summary(node, source.parser),
                section=section,
                rules=len(leaves),
                checked=checked,
                checked_by=overlay.get(key, ()),
                quoted_bad=bad,
                quoted_good=good,
            )
        )
        for child in node.children:
            visit(child, key, (*section, node.title) if node.level else section)

    for child in root.children:
        visit(child, "", ())
    return found


def _link(source: Source, node: Node) -> str:
    return f"{source.link}#{node.anchor}" if node.anchor else source.link


def enforcement(checked_by: Sequence[str]) -> str:
    """``[checked by clang-tidy cppcoreguidelines-owning-memory]`` or ``[prose]``."""
    if not checked_by:
        return "[prose]"
    return (
        "[checked by "
        + ", ".join(c.replace(":", " ", 1) for c in checked_by)
        + "]"
    )


def index_line(node: Node, rule: Rule) -> str:
    """A node's line in its parent's ``index.md``."""
    if rule.leaf:
        return f"- {node.id}: {_rest(node)} {enforcement(rule.checked_by)} → {node.path}"
    rules = f"{rule.rules} rule{'s' if rule.rules != 1 else ''}"
    return f"- {node.id}: {_rest(node)} — {rules}, {rule.checked} checked → {node.path}"


def notice(source: Source) -> str:
    """The source's attribution, version and license, for every page."""
    version = f", {source.pin}" if source.pin else ""
    license_ = (
        f"[{source.license}]({source.license_url})"
        if source.license_url
        else source.license
    )
    return (
        f"From {source.title}{version} ({source.link or source.url}). "
        f"{source.attribution} License: {license_}; see `LICENSE` at the top of this tree."
    ).replace("  ", " ")


TREE_NOTE: Final = (
    "Paths are relative to this guide's folder, the one with the top `index.md`. "
    "Open a section's `index.md` for its rules, a rule's file for its full text."
)


def _index_page(source: Source, node: Node, rules: Mapping[str, Rule]) -> str:
    title = source.title if node.level == 0 else node.title
    lines = [f"# {title}", "", notice(source), "", TREE_NOTE, ""]
    lines += [
        index_line(c, rules[f"{source.id}/{c.id}"]) for c in node.children
    ]
    own = node.text.strip()
    if own:
        lines += ["", "## Text of this section", "", own]
    return "\n".join(lines) + "\n"


def _leaf_page(source: Source, node: Node, rule: Rule) -> str:
    header = [
        f"# {node.title}",
        "",
        f"- id: `{rule.id}`",
        f"- section: {' › '.join(rule.section) or source.title}",
        f"- upstream: {rule.link}",
        f"- enforcement: {enforcement(rule.checked_by)}",
    ]
    header += [f"- {notice(source)}", "", "---", "", node.text.strip()]
    return "\n".join(header) + "\n"


def _toml(rules: Sequence[Rule], source: Source) -> str:
    """``index.toml``: the tree as one ``[[rule]]`` table per entry."""
    lines = [
        f"source = {json.dumps(source.id)}",
        f"title = {json.dumps(source.title)}",
        f"license = {json.dumps(source.license)}",
        f"pin = {json.dumps(source.pin)}",
    ]
    for rule in rules:
        lines.append("\n[[rule]]")
        for name, value in asdict(rule).items():
            if value or name in ("id", "path", "leaf"):
                lines.append(
                    f"{name} = {json.dumps(value, ensure_ascii=False)}"
                )
    return "\n".join(lines) + "\n"


def _license_page(source: Source) -> str:
    lines = [f"# License of this tree: {source.license}", ""]
    if source.license_url:
        lines += [
            f"The text derives from {source.title} and is licensed under "
            f"{source.license}: <{source.license_url}>.",
            "",
        ]
    if source.license_text:
        lines += [source.license_text.strip(), ""]
    return "\n".join(lines)


def _attribution_page(source: Source) -> str:
    return "\n".join(
        [
            f"# {source.title}",
            "",
            f"- upstream: {source.link or source.url}",
            f"- pinned: {source.pin or 'the committed copy in `source/`'}",
            f"- sha256 of the copy: `{source.sha256}`",
            f"- license: {source.license} {source.license_url}".rstrip(),
            "",
            source.attribution,
            "",
            "Generated by `plumbcheck index` from the copy in `source/`: "
            "the index pages and the leaves' headers are added; each leaf holds "
            "its rule's text as the source has it, converted to Markdown where "
            "the source is HTML.",
            "",
        ]
    )


def render(
    source: Source,
    copy: Path,
    overlay: Mapping[str, tuple[str, ...]],
) -> tuple[dict[str, str], list[Rule]]:
    """The files of ``source``'s tree, by path, and its index.

    Raises:
        ValueError: The tree is incomplete against the source.
    """
    root, problems = PARSERS[source.parser](source, copy)
    if problems:
        raise ValueError(f"{source.id} is incomplete: " + "; ".join(problems))
    rules = entries(source, root, overlay)
    by_id = {r.id: r for r in rules}
    files = {
        INDEX: _index_page(source, root, by_id),
        INDEX_TOML: _toml(rules, source),
        "LICENSE": _license_page(source),
        "ATTRIBUTION.md": _attribution_page(source),
    }
    for node in root.walk():
        if node is root:
            continue
        key = f"{source.id}/{node.id}"
        files[node.path] = (
            _index_page(source, node, by_id)
            if node.children
            else _leaf_page(source, node, by_id[key])
        )
    return files, rules


def write(folder: Path, files: Mapping[str, str]) -> None:
    """Replace the generated files of ``folder`` by ``files``; keep ``source/``."""
    for old in sorted(folder.rglob("*"), reverse=True):
        relative = old.relative_to(folder)
        if relative.parts[0] == SOURCE:
            continue
        if old.is_file() and relative.as_posix() not in files:
            old.unlink()
        elif old.is_dir() and not any(old.iterdir()):
            old.rmdir()
    for path, text in files.items():
        target = folder / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.is_file() or target.read_text() != text:
            target.write_text(text)


def stale(folder: Path, files: Mapping[str, str]) -> list[str]:
    """Generated files of ``folder`` that differ from ``files``, or are extra."""
    found = [
        path
        for path, text in files.items()
        if not (folder / path).is_file() or (folder / path).read_text() != text
    ]
    found += [
        p.relative_to(folder).as_posix()
        for p in folder.rglob("*")
        if p.is_file()
        and p.relative_to(folder).parts[0] != SOURCE
        and p.relative_to(folder).as_posix() not in files
    ]
    return sorted(found)


def read_index(folder: Path) -> list[Rule]:
    """The entries of a tree's ``index.toml``."""
    tables: list[dict[str, Any]] = tomllib.loads(
        (folder / INDEX_TOML).read_text()
    ).get("rule", [])
    return [Rule(**_tupled({**_DEFAULTS, **t})) for t in tables]


_DEFAULTS: Final = {"title": "", "parent": "", "link": "", "text": ""}


def depth(rules: Sequence[Rule]) -> int:
    """The tree's depth: the most levels from the top index down to a leaf."""
    return max((len(r.path.split("/")) for r in rules if r.leaf), default=0)
