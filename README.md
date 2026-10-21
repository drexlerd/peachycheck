# plumbcheck

One `plumbcheck.yml` per repository runs the native linters (ruff, eslint,
clang-tidy, vale, doc8, codespell, gitleaks), the project's own checks
(build, tests, type checks) and the hooks that apply them while Claude Code
or Codex works. Locally, in the agents' hooks and in CI it is the same
command with the same configuration, so a change that passes one passes all.

Why: every repository otherwise grows its own mix of pre-commit hooks, CI
steps and editor settings that drift apart. plumbcheck keeps the shared part
in one file an organization publishes, and each repository `EXTENDS` it,
overriding only what it must.

- **Native linters, no wrappers.** Each linter runs with its own
  configuration file (`ruff.toml`, `.clang-tidy`, `.vale.ini`, ...); a
  finding names the linter, its rule and the rule's documentation.
- **Deviations only.** A configuration file holds only the settings that
  differ from the tool's defaults, each with its reason.
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

ruff, codespell, doc8 and clang-tidy are pinned dependencies; vale and
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
2. `.github/workflows/plumbcheck.yml`, which calls the reusable workflow
   below (with `secrets: inherit` when the repository has a `.gitmodules`);
3. the shared tool configurations (`sync`) and the pinned vale and gitleaks
   (`install`);
4. for a Python project with a `pyproject.toml`, `plumbcheck==<version>` in
   the `dev` dependency group (`uv add --dev`), when installed from PyPI.

`upgrade` moves `PLUMBCHECK_VERSION`, the reusable workflow's ref in
`.github/workflows/` and an existing dev pin to the installed version (or
`--to VERSION`) and syncs the configurations again.

### CI

```yaml
jobs:
  plumbcheck:
    uses: drexlerd/plumbcheck/.github/workflows/check.yml@v0.1.0
    # with:
    #   version: 0.1.0     # default: PLUMBCHECK_VERSION of plumbcheck.yml
    # secrets: inherit     # SUBMODULES_TOKEN for private submodules/siblings
```

The workflow installs plumbcheck (the `version` input, else the repository's
`PLUMBCHECK_VERSION`, else the latest commit; a version such as `0.1.0` comes
from PyPI, anything else is a git ref of this repository), then fails when

1. a tool configuration differs from what the configuration shares
   (`sync --check`),
2. a command of the repository's types fails (`run`),
3. a linter finds anything in a changed file (`lint --base`), or anywhere on
   a push to the default branch (`lint --all`).

Before the commands it checks out submodules and, for each uv path source
`../<name>`, the repository `<owner>/<name>` next to it, on the same-named
branch if it has one (`plumbcheck siblings` lists them), so cross-repository
changes test together. Private ones need a `SUBMODULES_TOKEN` secret. An
organization can wrap the workflow in its own, pinning the version for all
its repositories.

### Locally

```sh
plumbcheck types          # the recognised project types
plumbcheck sync [--check] # write (or compare) the shared tool configurations
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
| `PLUMBCHECK_VERSION` | the plumbcheck the CI workflow installs |
| `LINTERS` | one entry per linter: language, extensions, command, fix, config, output parser, ... |
| `VERSIONS` | the downloaded tools, by version, URL and sha256 |
| `PROJECT_TYPES` | markers, setup and check commands of each project type |
| `FILES` | the tool configurations `sync` writes into a repository of each type |
| `RULES` | the folder of the linters' fallback configurations |
| `GUIDES` | the folder of the organization's guides (optional) |

A linter's configuration file is the repository's own (`ruff.toml`,
`.clang-tidy`, `.vale.ini`, `eslint.config.mjs`), else the one in `RULES`,
else the one in plumbcheck's defaults, else none (the tool's defaults).

### Linters

| linter | files | tool |
|---|---|---|
| `PYTHON_RUFF`, `PYTHON_RUFF_FORMAT` | `.py` | ruff |
| `PYTHON_JINJA_ENVIRONMENT` | `.py` | `checks/`: Jinja environments strip their tag lines |
| `JINJA_TAGS` | `.j2`, `.jinja`, `.jinja2` | `checks/`: at most one block tag per line |
| `RST_DOC8`, `SPELL_VALE`, `SPELL_CODESPELL` | `.rst` | doc8, vale, codespell |
| `TYPESCRIPT_ESLINT` | `.ts`, `.tsx` | the repository's `node_modules/.bin/eslint` |
| `CPP_CLANG_TIDY` | C++ | clang-tidy on `build/standards/compile_commands.json` |
| `REPOSITORY_GITLEAKS` | every file | gitleaks |

### Project types

| type | markers | commands |
|---|---|---|
| python-package | `pyproject.toml`, `uv.lock` | uv sync, ruff check, ruff format, pyright, pytest |
| verdog-project | `project.json` | verdog check and sync, ruff, pyright, pytest |
| typescript | `package.json` | npm ci, `npm run check`, `npm test` |
| cpp | `CMakeLists.txt` | cmake configure, build, ctest |

A repository may have several types, such as C++ with Python bindings. A
type runs after the types its `after` names (`cpp` after the Python types,
whose `uv sync` installs native dependencies from wheels). A `standards`
preset in `CMakePresets.json` or `CMakeUserPresets.json` replaces the plain
configure, build or test command of its kind; its binary directory stays
`build/standards` with `CMAKE_EXPORT_COMPILE_COMMANDS=ON`, or `run` fails.

### Defaults

The defaults' `rules/` hold only deviations from the tools' defaults that
make sense for any repository:

- `ruff.toml` (python-package, synced): `extend-select = ["I", "B"]`, import
  sorting, which `ruff format` leaves alone, and bugbear's likely bugs. No
  line length, docstring convention or complexity limit: those are an
  organization's choice.
- `verdog-project/ruff.toml` and `verdog-project/pyrightconfig.json`
  (synced): the same, plus what a Verdog project needs: imported workflows
  (`external/`) and generated declarations (`__init__.py`) are excluded,
  and pyright resolves imports from `src/`.
- `eslint.config.mjs` (typescript, synced): ESLint has no rules by default;
  this enables `@eslint/js`'s and typescript-eslint's recommended ones.
- `.vale.ini` (not synced): Vale needs a configuration to run; this one
  checks with Vale's own style, without its spelling (codespell checks that).

pyright, clang-tidy, doc8 and codespell run with their own defaults.

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

Both act only in a repository whose root has a `plumbcheck.yml`, so one
install serves the whole user; elsewhere they print nothing. A repository
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

`.github/workflows/release.yml` checks that the tag matches the version, runs
the checks, builds with `uv build`, publishes to PyPI by trusted publishing
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
