# verdog-ai standards

The coding and documentation standards of every verdog-ai repository, the
linters that enforce them and the hooks that apply them while Claude Code or
Codex works. Every repository uses the same rules, linters and tool
configurations; a repository's `standards.yml` may override what it must.

## What is here

- `standards/`: the instance. `general/` applies to every file; each folder of
  `languages/` (C++, Jinja, Python, reStructuredText, TypeScript, Verdog) holds its
  `language.toml` and `overlay.toml` (the only hand-written guideline file,
  see [Overlay](#overlay)). `metrics.toml` declares the code metrics,
  `general/tools.toml` their measurers.
- `standards/sources.toml`: the guides the guidelines derive from, each with
  its upstream, pin, sha256, license, attribution, store and parser.
  `verdog-standards index` turns each pinned copy into a tree that mirrors the
  guide's nesting: an `index.md` per level with one line per child, one leaf
  per rule with the rule's complete text, and `index.toml` for scripts (see
  [Guides](#guides)).
- `standards.yml`: the organization's configuration, in MegaLinter's style
  (see [standards.yml](#standardsyml)): the `LINTERS`, the pinned `VERSIONS`
  of the downloaded ones, the `PROJECT_TYPES` (markers and commands) and the
  `FILES` each type shares.
- `rules/`: the tool configurations (`ruff.toml`, `pyrightconfig.json`,
  `eslint.config.mjs`, `.clang-tidy`, `.vale.ini` and Vale's styles).
- `src/verdog_standards/`: the engine and the CLI `verdog-standards`;
  `checks/` holds the linters the instance runs itself (Jinja block tags,
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

The workflow installs `verdog-standards` and its linters, then fails when

1. a root tool configuration differs from `rules/` (`sync --check`),
2. a command of the repository's types fails (`run`),
3. a linter finds anything in a changed file (`lint --base`), or anywhere on
   a push to the default branch (`lint --all`). Repositories are kept
   finding-free; there is no baseline.

Locally:

```sh
uv tool install "verdog-standards @ git+https://github.com/verdog-ai/standards@main"
verdog-standards install        # download vale and gitleaks, pinned
verdog-standards types          # the recognised types
verdog-standards sync           # write the shared tool configurations
verdog-standards lint [--fix]   # findings in files changed since the merge base
verdog-standards lint --all     # findings anywhere
verdog-standards hooks install  # lint after each edit and before Claude/Codex stop
verdog-standards show FILE...   # the guidelines that apply to files
verdog-standards show --html map.html   # the browsable map of the standards
verdog-standards index          # generate the guides' trees from their copies
verdog-standards show --coverage   # rules with a checker vs. prose, and gaps
```

## Develop

```sh
uv sync
uv run verdog-standards check   # lint the instance
uv run pytest
```

auto-se uses the engine as a library: `linting.lint_errors(root, base)` for
its checks, and its own loop metrics, retriever and prompts through
`engine.LAYERS`, `LOOPS` and `CHECKS`.

## standards.yml

A repository's `standards.yml` extends the organization's file at a commit
(`sync` writes it) and overrides keys, as MegaLinter's configuration does:

```yaml
EXTENDS: https://raw.githubusercontent.com/verdog-ai/standards/<commit>/standards.yml
DISABLE_LINTERS: [SPELL_VALE]
FILTER_REGEX_EXCLUDE: ^vendor/
PYTHON_RUFF_ARGUMENTS: [--preview]
```

Later files win and mappings merge key by key; the keys
`CONFIG_PROPERTIES_TO_APPEND` names append their lists. `ENABLE_LINTERS`,
`DISABLE_LINTERS`, `FILTER_REGEX_EXCLUDE` and per linter
`<LINTER>_FILTER_REGEX_INCLUDE`, `<LINTER>_FILTER_REGEX_EXCLUDE` and
`<LINTER>_ARGUMENTS` select what runs. A linter passes the repository's own
configuration file (`ruff.toml`, `.clang-tidy`, `.vale.ini`) if it has one,
else the one in `rules/`.

| linter | files | tool |
|---|---|---|
| `PYTHON_RUFF`, `PYTHON_RUFF_FORMAT` | `.py` | ruff (pinned dependency) |
| `PYTHON_JINJA_ENVIRONMENT`, `JINJA_TAGS` | `.py`, `.j2` | `checks/` |
| `RST_DOC8`, `SPELL_VALE`, `SPELL_CODESPELL` | `.rst` | doc8, vale (downloaded), codespell |
| `TYPESCRIPT_ESLINT` | `.ts`, `.tsx` | the repository's `node_modules/.bin/eslint` |
| `CPP_CLANG_TIDY` | C++ | clang-tidy (the PyPI wheel), on `build/standards/compile_commands.json` |
| `REPOSITORY_GITLEAKS` | every file | gitleaks (downloaded) |

## Hooks

`verdog-standards hooks install` adds two hooks to `~/.claude/settings.json`
and `~/.codex/hooks.json` (it prints the diff; trust Codex's once with
`/hooks`):

- after each edit (`Edit|Write|MultiEdit`, Codex's `apply_patch`),
  `hook post-edit` fixes and lints the edited files and feeds the findings
  back to the agent;
- before the agent stops, `hook stop` lints the files changed since the merge
  base and runs the types' check commands (not their `setup`: installs, syncs,
  and the CMake configure once `build/standards` exists); it blocks the stop
  on a failure, at most twice in a row, then lets the agent stop and shows
  what remains.

Both act only in a repository whose root has a `standards.yml`, so one
install serves the whole user; elsewhere they do nothing. Set
`VERDOG_STANDARDS_HOOKS=off` to turn them off, and run
`verdog-standards hooks uninstall [--claude] [--codex]` to remove exactly
these entries again.

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

`lint` (and the hooks, and auto-se's gate) names each finding with the
guidelines its rule maps to:

```text
a.cpp has 1 findings; fix each one and follow the guideline it breaks:
  a.cpp:2: CPP_CLANG_TIDY:cppcoreguidelines-owning-memory returning a newly created resource ...
    - cpp-core-guidelines/R.20: R.20: <the rule's title> [checked by clang-tidy cppcoreguidelines-owning-memory] → .../R/smart/R.20.md
    - cpp-core-guidelines/I.11: I.11: <the rule's title> [checked by clang-tidy cppcoreguidelines-owning-memory] → .../I/I.11.md
```

`checked_by` names a linter by one of its `aliases` in `standards.yml`. A
rule no guideline maps is shown with the linter's documentation, and
`show --coverage` lists it when the linter's `list` command prints it. Other repositories read the trees from
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
Vale's `Google` style in `rules/vale/` comes from
[errata-ai/Google](https://github.com/errata-ai/Google) (MIT).
