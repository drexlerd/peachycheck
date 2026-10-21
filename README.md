# plumbcheck

One `plumbcheck.yml` per repository runs the native linters (ruff, eslint,
clang-tidy, vale, doc8, codespell, j2lint, gitleaks), the project's own checks
(build, tests, type checks) and the hooks that apply them while Claude Code
or Codex works. Locally, in the agents' hooks and in CI it is the same
command with the same configuration, so a change that passes one passes all.

Why: every repository otherwise grows its own mix of pre-commit hooks, CI
steps and editor settings that drift apart. plumbcheck keeps the shared part
in one file an organization publishes, and each repository `EXTENDS` it,
overriding only what it must.

- **No opinions.** plumbcheck only knows how to run and read each linter
  and project type; every linter runs with its own defaults until an
  organization or a repository configures it.
- **Native linters, native inheritance.** Each linter reads its own
  configuration file. `sync` writes an organization's base configurations
  into `.plumbcheck/`; the repository's own `ruff.toml`,
  `pyrightconfig.json` or `eslint.config.mjs` extends them and adds only
  its deviations. A finding names the linter, its rule and the rule's
  documentation.
- **Per file.** Each file belongs to the nearest folder above it with a
  `plumbcheck.yml`, and is judged by that repository's configuration; one
  edit or one `lint` may span repositories.
- **Finding-free.** Every finding fails; there is no baseline. On a pull
  request only the changed files count, on the default branch every file.
- **Guides, optional.** An organization may point `GUIDES` at its style
  guides; findings then cite the guideline their rule maps to (see
  [Guides](#guides)).

## Install

```sh
uv tool install plumbcheck     # or: pipx install plumbcheck
plumbcheck --version
plumbcheck install             # download the pinned vale and gitleaks
plumbcheck hooks install       # lint after each edit and before Claude/Codex stop
```

ruff, codespell, doc8, clang-tidy and j2lint are pinned dependencies; vale and
gitleaks are downloaded into `~/.cache/plumbcheck/bin`, checked against their
sha256 (Linux x64); eslint is the repository's own (`node_modules`).

## Set a repository up

```sh
plumbcheck init [--extends URL]    # once per repository
plumbcheck upgrade [--to 0.2.0]    # later: move it to another version
```

`init` prints what it did; run again, it changes nothing:

1. `plumbcheck.yml`, extending `--extends` (your organization's file, if
   any) and pinning `PLUMBCHECK_VERSION`: the installed release, or the
   installed commit for a git install;
2. `.github/workflows/plumbcheck.yml`, a job that runs the action below
   (passing `SUBMODULES_TOKEN` when the repository has a `.gitmodules`);
3. the base configurations in `.plumbcheck/` (`sync`) and, where the
   repository has none, its own `ruff.toml`, `pyrightconfig.json` or
   `eslint.config.mjs` holding just the line that extends the base;
4. the pinned vale and gitleaks (`install`);
5. for a Python project with a `pyproject.toml`, `plumbcheck==<version>` in
   the `dev` dependency group (`uv add --dev`), when installed from PyPI.

`upgrade` moves `PLUMBCHECK_VERSION`, the refs of the action and the
reusable workflow in `.github/workflows/` and an existing dev pin to the
installed version (or `--to VERSION`) and syncs the configurations again.

### CI

The action (`action.yml`) is the check as one step of a job:

```yaml
jobs:
  plumbcheck:
    runs-on: ubuntu-latest
    steps:
      - uses: drexlerd/plumbcheck@v0.1.0
        # with:
        #   version: 0.1.0                          # see below
        #   token: ${{ secrets.SUBMODULES_TOKEN }}  # private submodules/siblings
```

The reusable workflow does the same in three lines:

```yaml
jobs:
  plumbcheck:
    uses: drexlerd/plumbcheck/.github/workflows/check.yml@v0.1.0
    secrets: inherit
```

It checks the repository out and installs plumbcheck: the `version` input,
else the repository's `PLUMBCHECK_VERSION`, else the action's own code (so
`@<commit>` installs that commit). A version such as `0.1.0` comes from
PyPI, anything else is a git ref of this repository. Then it fails when

1. a tool configuration differs from what the configuration shares
   (`sync --check`),
2. a command of the repository's types fails (`run`),
3. a linter finds anything in a changed file (`lint --base`), or anywhere on
   a push to the default branch (`lint --all`).

Before the commands it checks out submodules and, for each uv path source
`../<name>`, the repository `<owner>/<name>` next to it, on the same-named
branch if it has one (`plumbcheck siblings` lists them), so cross-repository
changes test together. Private ones need the token. An organization can
wrap the action in a reusable workflow of its own that pins one plumbcheck
for all its repositories; a job that runs the action keeps its name, so a
required status check stays the same.

### Locally

```sh
plumbcheck types          # the recognised project types
plumbcheck sync [--check] # write (or compare) .plumbcheck/, the base configurations
plumbcheck run            # the types' setup and check commands
plumbcheck lint [--fix]   # findings in files changed since the merge base
plumbcheck lint --all     # findings anywhere
plumbcheck lint FILE...   # findings in these files
```

## Configuration

A repository's `plumbcheck.yml` extends plumbcheck's defaults
([src/plumbcheck/defaults/plumbcheck.yml](src/plumbcheck/defaults/plumbcheck.yml)),
then the files it `EXTENDS`, then its own keys, in MegaLinter's style:

```yaml
EXTENDS: https://raw.githubusercontent.com/my-org/standards/<commit>/plumbcheck.yml
PLUMBCHECK_VERSION: 0.1.0
DISABLE_LINTERS: [SPELL_VALE]
FILTER_REGEX_EXCLUDE: ^vendor/
PYTHON_RUFF_ARGUMENTS: [--preview]
```

- **Merging.** Later files win, mappings merge key by key, and the keys
  `CONFIG_PROPERTIES_TO_APPEND` names append their lists. `EXTENDS` takes a
  path or URL, or a list of them; a relative one resolves against the file
  that names it.
- **Paths.** `RULES`, `GUIDES` and the sources of `FILES` are paths relative
  to the file that names them. A file extended by its GitHub raw URL
  (`https://raw.githubusercontent.com/<owner>/<repo>/<ref>/<path>`) is read
  from a snapshot of that repository at that ref, cached in
  `~/.cache/plumbcheck/extends/`, so an organization's folders (rules, Vale
  styles, guides) resolve too. Pin a commit or tag: a cached ref is never
  fetched again. Other URLs work for single files without relative paths.
- **Selecting linters.** `ENABLE_LINTERS` (only these), `DISABLE_LINTERS`,
  `FILTER_REGEX_EXCLUDE` (a path regex for every linter) and, per linter,
  `<LINTER>_FILTER_REGEX_INCLUDE`, `<LINTER>_FILTER_REGEX_EXCLUDE` and
  `<LINTER>_ARGUMENTS` (appended to its command).

| key | what |
|---|---|
| `EXTENDS` | the files this one extends, resolved first |
| `PLUMBCHECK_VERSION` | the plumbcheck the CI action installs |
| `LINTERS` | one entry per linter: language, extensions, command, fix, config, output parser, ... |
| `VERSIONS` | the downloaded tools, by version, URL and sha256 |
| `PROJECT_TYPES` | markers, setup and check commands of each project type |
| `FILES` | per project type (or `general`), the base files `sync` writes into `.plumbcheck/` |
| `RULES` | a folder the linters' `list` commands may name as `{rules}` |
| `GUIDES` | the folder of the organization's guides (optional) |

### Base and overrides

`FILES` names, per project type or under `general` for every repository,
the files `sync` writes into the repository's `.plumbcheck/` folder (a
folder, such as Vale's styles, brings every file below it). The folder is
generated and committed, so editors and CI read it offline; `sync --check`
fails when it differs. A base file is read from `.plumbcheck/`, so its
paths start there: `"../**/tests/**"` for ruff's per-file ignores,
`"extraPaths": ["../src"]` for pyright.

A repository overrides a base setting in its own configuration, which
inherits the base natively:

| tool | the repository's own file |
|---|---|
| ruff | `ruff.toml` (or `[tool.ruff]`): `extend = ".plumbcheck/ruff.toml"` |
| pyright | `pyrightconfig.json`: `"extends": ".plumbcheck/pyrightconfig.json"` |
| ESLint | `eslint.config.mjs`: `import base from "./.plumbcheck/eslint.config.mjs"; export default [...base, ...]` |
| Vale | `.vale.ini` with `StylesPath = .plumbcheck/styles` and the base's styles |
| clang-tidy | `.clang-tidy` with deviations only: `lint` lays it over the base (`Checks` joined, `CheckOptions` merged) |

A linter gets the repository's own file, else the base, else none: then
it runs with its defaults, or, for Vale and ESLint, which cannot run
without a configuration, it is skipped. Coarse switches stay in
`plumbcheck.yml`: `DISABLE_LINTERS`, `<LINTER>_FILTER_REGEX_EXCLUDE`,
`<LINTER>_ARGUMENTS`.

### Linters

| linter | files | tool |
|---|---|---|
| `PYTHON_RUFF`, `PYTHON_RUFF_FORMAT` | `.py` | ruff |
| `J2LINT` | `.j2`, `.jinja`, `.jinja2` | j2lint |
| `RST_DOC8`, `SPELL_VALE`, `SPELL_CODESPELL` | `.rst` | doc8, vale, codespell |
| `TYPESCRIPT_ESLINT` | `.ts`, `.tsx` | the repository's `node_modules/.bin/eslint` |
| `CPP_CLANG_TIDY` | C++ | clang-tidy on `build/standards/compile_commands.json` |
| `REPOSITORY_GITLEAKS` | every file | gitleaks |

### Project types

| type | markers | commands |
|---|---|---|
| python-package | `pyproject.toml`, `uv.lock` | uv sync, ruff check, ruff format, pyright, pytest |
| typescript | `package.json` | npm ci, `npm run check`, `npm test` |
| cpp | `CMakeLists.txt` | cmake configure, build, ctest |

An organization may add its own types (with `replaces` to supersede one).
A repository may have several types, such as C++ with Python bindings. A
type runs after the types its `after` names (`cpp` after `python-package`,
whose `uv sync` installs native dependencies from wheels). A `standards`
preset in `CMakePresets.json` or `CMakeUserPresets.json` replaces the plain
configure, build or test command of its kind; its binary directory stays
`build/standards` with `CMAKE_EXPORT_COMPILE_COMMANDS=ON`, or `run` fails.

## Hooks

`plumbcheck hooks install` adds two hooks to `~/.claude/settings.json` and
`~/.codex/hooks.json` and prints the diff (trust Codex's once with `/hooks`):

- after each edit (`Edit|Write|MultiEdit`, Codex's `apply_patch`),
  `plumbcheck hook post-edit` fixes and lints the edited files and feeds the
  findings back to the agent;
- before the agent stops, `plumbcheck hook stop` lints the files changed
  since the merge base and runs the types' check commands (not their
  `setup`); it blocks the stop on a failure, at most twice in a row, then
  lets the agent stop and shows what remains.

Each edited file counts for its own repository (the nearest folder above it
with a `plumbcheck.yml`), so a Codex patch that spans repositories is
judged file by file. The Stop hook checks every repository with changes at
or below the session's folder (its own, and side-by-side worktrees under a
parent folder, three levels deep), each with its own lint and commands.
Files of no such repository are left alone, so one install serves the
whole user. A repository
whose `.venv` has another version of plumbcheck (its dev pin) answers with
that version. `PLUMBCHECK_HOOKS=off` turns them off;
`plumbcheck hooks uninstall [--claude] [--codex]` removes exactly these
entries. A reinstall also replaces the hooks of plumbcheck's earlier name,
`verdog-standards`.

## Guides

`GUIDES` points to a folder of an organization's style guides; plumbcheck
ships none. Its layout:

```text
sources.toml                 # the guides: upstream, pin, sha256, license, parser
metrics.toml                 # code metrics (optional)
general/                     # language.toml, overlay.toml, tools.toml
languages/<language>/
  language.toml              # title, extensions, marker, test globs
  overlay.toml               # the guidelines: rules of the guides and house rules
  references/<guide>/        # the generated tree of a committed guide
```

`plumbcheck index` turns each pinned copy into a tree an agent reads
top-down: an `index.md` per level with one line per child, one leaf per rule
with the rule's complete text, and `index.toml` for scripts. A guide whose
license allows no public copy (`store = "cache"`) is fetched and generated
into `~/.cache/plumbcheck/` only. The parsers cover Markdown guides with
numbered sections, HTML guides, the C++ Core Guidelines' rule anchors, the
Google developer documentation site and Diátaxis.

A language's `overlay.toml` maps rules to the linters that check them:

```toml
["some-guide/3.16"]
checked_by = ["ruff:N*"]          # an alias of a linter, and a glob of its rules
priority = 1                      # listed in an agent's prompt, lowest first

["house/independent-tests"]       # a house rule: no guide states it
text = "Keep tests independent."
```

`lint` then names each finding with the guidelines its rule maps to and the
leaf that explains it:

```text
a.py has 1 findings; fix each one and follow the guideline it breaks:
  a.py:17: PYTHON_RUFF:N802 Function name `mixedCase` should be lowercase
    - some-guide/3.16: 3.16 Naming: ... [checked by ruff N*] → .../3.16-naming.md
```

```sh
plumbcheck check                  # lint the guides: overlays, trees, metrics
plumbcheck index [GUIDE...]       # generate the guides' trees
plumbcheck show FILE...           # the guidelines that apply to files
plumbcheck show --coverage        # rules with a checker vs. prose, and gaps
plumbcheck show --html map.html   # the browsable map of the guides
```

`check` fails when a committed tree differs from what `index` generates and
when a tracked file holds a cache-only guide or quotes its rules' titles.
A tool such as an agent loop can use the engine as a library:
`linting.lint_errors(root, base)` for its checks, and `engine.LAYERS`,
`LOOPS` and `CHECKS` for its own metrics, retrievers and prompts.

## Develop

```sh
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check . && uv run pyright
uv run plumbcheck lint --all
```

The tests use the sample organization in `tests/fixtures/org`.

## Release

Set `version` in `pyproject.toml`, merge, then tag the merge commit:

```sh
git tag v0.1.0 && git push origin v0.1.0
```

Before tagging, point `.github/workflows/check.yml` at the action of the
new release (`uses: drexlerd/plumbcheck@v<version>`).
`.github/workflows/release.yml` checks that the tag matches the version and
that `check.yml` uses its action, runs the checks, builds with `uv build`, publishes to PyPI by trusted publishing
(environment `pypi`) and creates the GitHub release with the distributions.

Once, before the first release, add a pending publisher on PyPI (Account
settings, Publishing, "Add a new pending publisher", GitHub): project
`plumbcheck`, owner `drexlerd`, repository `plumbcheck`, workflow
`release.yml`, environment `pypi`; and create the environment `pypi` in this
repository's settings (Environments).

## License

plumbcheck is licensed under the GNU Affero General Public License v3.0 only
(`AGPL-3.0-only`, see `LICENSE`). `graph.html` loads Cytoscape.js, elkjs and
cytoscape-elk from a CDN when viewed; none is bundled. The sample guide in
`tests/fixtures/org` was written for the tests and is dedicated to the
public domain (CC0 1.0).
