# verdog-ai standards

The coding and documentation standards of every verdog-ai repository, and the
one check that enforces them. There is no repository-local specialization:
each repository uses the same rules, checkers and tool configurations.

## What is here

- `standards/`: the instance. `general/` applies to every file; each folder of
  `languages/` (C++, Jinja, Python, reStructuredText, TypeScript, Verdog) holds its
  `language.toml`, `overlay.toml` (the only hand-written guideline file, see
  [Overlay](#overlay)) and `tools.toml` (its checkers). `metrics.toml`
  declares the code metrics, `literature.bib` what guidelines and metrics
  cite.
- `standards/sources.toml`: the guides the guidelines derive from, each with
  its upstream, pin, sha256, license, attribution, store and parser.
  `verdog-standards index` turns each pinned copy into a tree that mirrors the
  guide's nesting: an `index.md` per level with one line per child, one leaf
  per rule with the rule's complete text, and `index.toml` for scripts (see
  [Guides](#guides)).
- `templates/<type>/`: the project types. `type.toml` names the marker files
  that recognise a type and the commands its check runs; `files/` holds the
  root tool configurations (`ruff.toml`, `pyrightconfig.json`,
  `eslint.config.mjs`, `.clang-tidy`) every repository
  of that type shares.
- `src/verdog_standards/`: the engine and the CLI `verdog-standards`;
  `checks/` holds the checkers the instance runs itself (Jinja block tags,
  Jinja environments in Python).
- `.github/workflows/check.yml`: the reusable workflow every repository calls.

| type | markers | commands |
|---|---|---|
| python-package | `pyproject.toml`, `uv.lock` | uv sync, ruff check, ruff format, pyright, pytest |
| verdog-project | `project.json` | verdog check and sync, ruff, pyright, pytest |
| typescript | `package.json` | npm ci, `npm run check`, `npm test` |
| cpp | `CMakeLists.txt` | cmake configure, build, ctest |

A repository may have several types, such as C++ with Python bindings. Three
rules let one check build them all:

1. **Order.** A type runs after the types its `after` names: `cpp` runs after
   `python-package` and `verdog-project`, whose `uv sync` installs native
   dependencies from wheels.
2. **Siblings.** Before the commands, the check clones each uv path source
   `../<name>` for which `verdog-ai/<name>` exists next to the repository, on
   the same-named branch if there is one (`verdog-standards siblings` lists
   them). Private ones need `SUBMODULES_TOKEN`.
3. **Presets.** A `standards` preset in `CMakePresets.json` or
   `CMakeUserPresets.json` replaces the plain configure, build or test command
   of its kind, for project-specific options. Its binary directory stays
   `build/standards` with `CMAKE_EXPORT_COMPILE_COMMANDS=ON`, or `run` fails.

The uv cache and ccache keep the second run fast.

## Use it in a repository

```yaml
jobs:
  standards:
    uses: verdog-ai/standards/.github/workflows/check.yml@main
```

The workflow installs `verdog-standards`, then fails when

1. a root tool configuration differs from its template (`sync --check`),
2. a command of the repository's types fails (`run`),
3. a changed file gains findings of its language's checkers, or a new or
   changed Python function outside tests has a cyclomatic complexity above 10
   (`check-branch`).

Locally:

```sh
uv tool install "verdog-standards @ git+https://github.com/verdog-ai/standards@main"
verdog-standards types          # the recognised types
verdog-standards sync           # write the shared tool configurations
verdog-standards check-branch   # new findings since the merge base
verdog-standards show FILE...   # the guidelines that apply to files
verdog-standards show --html map.html   # the browsable map of the standards
verdog-standards index          # generate the guides' trees from their copies
verdog-standards show --coverage   # rules with a checker vs. prose, and gaps
```

## Develop

```sh
uv sync
uv run verdog-standards check --tests   # lint the instance, test the examples
uv run pytest
```

Checkers run isolated from a repository's own configuration, and only new
findings in changed files count, so a repository adopts the standards without
fixing its history first.

auto-se uses the engine as a library and adds its own loop metrics, retriever
and prompts through `engine.LAYERS`, `LOOPS`, `CATEGORIES` and `CHECKS`.

## Guides

Each guide of `standards/sources.toml` becomes a tree an agent reads top-down,
opening only the branch it needs:

```text
standards/languages/python/references/google-python/
  source/pyguide.md                    # the pinned copy, checked by sha256
  index.md                             # one line per section
  index.toml                           # the same tree, machine-readable
  LICENSE  ATTRIBUTION.md
  3-python-style-rules/
    index.md                           # one line per rule of section 3
    3.8-comments-and-docstrings.md     # a leaf: the rule's complete text
~/.cache/verdog-standards/cpp-core-guidelines/<pin>/   # store = "cache"
  index.md  R/index.md  R/smart/index.md  R/smart/R.20.md  ...
```

A leaf is a rule: an anchored rule heading of the C++ Core Guidelines
(`R.20: ...`), a numbered section of a Google guide, a page of the Google
developer documentation style guide, one of Diátaxis's four kinds. Headings
below a rule (Reason, Example, Enforcement; Pros, Cons, Decision) stay in its
leaf. Each parser checks the tree against its source and the generator fails
when a rule is missing: the rules a Core Guidelines section lists, numbered
sections without gaps but the manifest's `gaps`, every page of the style
guide's contents, all four Diátaxis kinds. `verdog-standards check` fails when
a committed tree differs from what `index` generates, and when a cache-only
guide is committed.

## Overlay

A language's `overlay.toml` holds what the guides cannot say, keyed by index
id:

```toml
["cpp-core-guidelines/R.20"]
checked_by = ["clang-tidy:cppcoreguidelines-owning-memory"]
bad = "examples/ownership_bad.cpp"   # the checker must flag it (check --tests)
priority = 1                          # listed in the prompt, lowest first

["house/independent-tests"]          # a house rule: no guide states it
text = "Keep tests independent; give each a single observable behavior to check."
advisory = true
priority = 11
```

- `checked_by` maps a rule to `tool` or `tool:check`, a glob such as
  `ruff-docstrings:D*`; one check may map to several rules. The C++ table maps
  every `cppcoreguidelines-*` check of clang-tidy by hand to the rules its
  documentation enforces.
- `text` replaces the derived text where it is poor; a house rule needs one,
  and its `see` may name the rule whose leaf explains it.
- Every key other than `house/<id>` must be an id of the language's guides'
  indexes, or `check` fails.

The prompt shows per active guide the lines of its top `index.md` (with its
license and attribution), and one line per guideline with a `priority` (its
id, rule, enforcement and leaf); the agent opens the `index.md` below, down
to a rule's leaf.

On failure, `check-branch` (and auto-se's gate, which runs the same check)
names each new finding with the guidelines its check maps to:

```text
a.cpp gains clang-tidy findings (0 -> 1); fix each new one and follow the guideline it breaks:
  a.cpp:2: warning: returning a newly created resource ... [cppcoreguidelines-owning-memory]
    - cpp-core-guidelines/R.20: R.20: (a rule title) [checked by clang-tidy cppcoreguidelines-owning-memory] → .../R/smart/R.20.md
    - cpp-core-guidelines/I.11: I.11: (a rule title) [checked by clang-tidy cppcoreguidelines-owning-memory] → .../I/I.11.md
```

Each checker's `check` pattern in `tools.toml` finds a finding's check id. A
finding is new when the base has none of the same file, check and message,
wherever its line moved. A check no guideline maps is shown with the tool's
documentation, and `show --coverage` lists it when the tool's `list` command
prints it. Other repositories read the trees from
their worktree's `.verdog-standards/references/<guide>/`, where
`engine.copy_references` puts them.

## Licenses

`standards/` quotes or pins third-party guides; `standards/sources.toml`
names each with its license and attribution, and every tree carries `LICENSE`
and `ATTRIBUTION.md`. The Google Python and TypeScript style guides are CC BY
3.0, the Google developer documentation style guide CC BY 4.0 (a snapshot of
its pages' articles, since it has no source repository) and Diátaxis CC BY-SA
4.0, so its tree is CC BY-SA 4.0 too; all are committed with attribution. The
C++ Core Guidelines allow copies for personal or internal business use only:
they are pinned by commit and sha256 and fetched, and their tree generated,
into `~/.cache/verdog-standards/` only, never into this public repository.
Vale's `Google` style in `standards/languages/rst/vale/` comes from
[errata-ai/Google](https://github.com/errata-ai/Google) (MIT).
