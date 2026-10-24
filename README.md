<p align="center">
  <img src="https://raw.githubusercontent.com/drexlerd/peachycheck/main/logo/logo.png" alt="peachycheck" width="160">
</p>

# peachycheck

One `peachycheck.yml` per repository runs the native linters (ruff, pyright,
eslint, clang-tidy, clang-format, vale, doc8, codespell, j2lint, gitleaks)
and the hooks that apply them while Claude Code or Codex works. It only
checks: builds and tests stay in the repository's own CI. Locally, in the agents' hooks
and in CI it is the same command with the same configuration, so a change
that passes one passes all.

Why: every repository otherwise grows its own mix of pre-commit hooks, CI
steps and editor settings that drift apart. peachycheck keeps the shared part
in one file an organization publishes, and each repository `EXTENDS` it,
overriding only what it must.

- **No opinions.** peachycheck only knows how to run and read each linter;
  every linter runs with its own defaults until an
  organization or a repository configures it.
- **Native linters, native inheritance.** Each linter reads its own
  configuration file. `sync` writes an organization's base configurations
  into `.peachycheck/`; the repository's own `ruff.toml`,
  `pyrightconfig.json` or `eslint.config.mjs` extends them and adds only
  its deviations. A finding names the linter, its rule and the rule's
  documentation.
- **Per file.** Each file belongs to the nearest folder above it with a
  `peachycheck.yml`, and is judged by that repository's configuration; one
  edit or one `lint` may span repositories.
- **Finding-free.** Every finding fails; there is no baseline. On a pull
  request only the changed files count, on the default branch every file.

## Languages

- [x] Python (`.py`): ruff, ruff format, pyright
- [x] C++ (`.cpp`, `.cc`, `.cxx`, `.hpp`, `.hh`, `.h`, `.tpp`, `.ipp`): clang-tidy, clang-format
- [x] TypeScript (`.ts`, `.tsx`): ESLint
- [x] reStructuredText (`.rst`): doc8, vale, codespell
- [x] Jinja (`.j2`, `.jinja`, `.jinja2`): j2lint
- [x] Secrets (every file): gitleaks
- [ ] LaTeX (`.tex`)

## Install

```sh
uv tool install peachycheck     # or: pipx install peachycheck
peachycheck --version
peachycheck install             # download the pinned vale and gitleaks
peachycheck hooks install       # lint after each edit and before Claude/Codex stop
```

ruff, codespell, doc8, clang-tidy, clang-format, j2lint and pyright are
pinned dependencies (pyright uses Node from `PATH` or installs it); vale and gitleaks are downloaded into `~/.cache/peachycheck/bin`, checked against their
sha256 (Linux x64); eslint is the repository's own (`node_modules`).

## Set a repository up

```sh
peachycheck init [--extends URL]    # once per repository
peachycheck upgrade [--to 0.2.0]    # later: move it to another version
```

`init` prints what it did; run again, it changes nothing:

1. `peachycheck.yml`, extending `--extends` (your organization's file, if
   any) and pinning `PEACHYCHECK_VERSION`: the installed release, or the
   installed commit for a git install;
2. `.github/workflows/peachycheck.yml`, a job that runs the action below
   (passing `SUBMODULES_TOKEN` when the repository has a `.gitmodules`);
3. the base configurations in `.peachycheck/` (`sync`) and, where the
   repository has none, its own `ruff.toml`, `pyrightconfig.json` or
   `eslint.config.mjs` holding just the line that extends the base;
4. the pinned vale and gitleaks (`install`);
5. for a Python project with a `pyproject.toml`, `peachycheck==<version>` in
   the `dev` dependency group (`uv add --dev`), when installed from PyPI.

`upgrade` moves `PEACHYCHECK_VERSION`, the refs of the action and the
reusable workflow in `.github/workflows/` and an existing dev pin to the
installed version (or `--to VERSION`) and syncs the configurations again.

### CI

The action (`action.yml`) is the check as one step of a job:

```yaml
jobs:
  peachycheck:
    runs-on: ubuntu-latest
    steps:
      - uses: drexlerd/peachycheck@v0.1.0
        # with:
        #   version: 0.1.0                          # see below
        #   token: ${{ secrets.SUBMODULES_TOKEN }}  # private submodules
        #   prepare: npm ci                         # what the linters need
```

The reusable workflow does the same in three lines:

```yaml
jobs:
  peachycheck:
    uses: drexlerd/peachycheck/.github/workflows/check.yml@v0.1.0
    secrets: inherit
```

It checks the repository out and installs peachycheck: the `version` input,
else the repository's `PEACHYCHECK_VERSION`, else the action's own code (so
`@<commit>` installs that commit). A version such as `0.1.0` comes from
PyPI, anything else is a git ref of this repository. Then it fails when

1. a tool configuration differs from what the configuration shares
   (`sync --check`),
2. a linter finds anything in a changed file (`lint --base`), or anywhere on
   a push to the default branch (`lint --all`).

It checks out submodules (private ones need the token) and runs `prepare`,
if given, before the linters: what they need, such as `npm ci` for ESLint
or a CMake configure that writes `compile_commands.json` for clang-tidy. It
builds and tests nothing; that is the repository's own CI. An organization can
wrap the action in a reusable workflow of its own that pins one peachycheck
for all its repositories; a job that runs the action keeps its name, so a
required status check stays the same.

### Locally

```sh
peachycheck sync [--check] # write (or compare) .peachycheck/, the base configurations
peachycheck lint [--fix]   # findings in files changed since the merge base
peachycheck lint --all     # findings anywhere
peachycheck lint FILE...   # findings in these files
```

## Configuration

A repository's `peachycheck.yml` extends peachycheck's defaults
([src/peachycheck/defaults/peachycheck.yml](src/peachycheck/defaults/peachycheck.yml)),
then the files it `EXTENDS`, then its own keys, in MegaLinter's style:

```yaml
EXTENDS: https://raw.githubusercontent.com/my-org/standards/<commit>/peachycheck.yml
PEACHYCHECK_VERSION: 0.1.0
DISABLE_LINTERS: [SPELL_VALE]
FILTER_REGEX_EXCLUDE: ^vendor/
PYTHON_RUFF_ARGUMENTS: [--preview]
```

- **Merging.** Later files win, mappings merge key by key, and the keys
  `CONFIG_PROPERTIES_TO_APPEND` names append their lists. `EXTENDS` takes a
  path or URL, or a list of them; a relative one resolves against the file
  that names it.
- **Paths.** The sources of `FILES` are paths relative to the file that
  names them. A file extended by its GitHub raw URL
  (`https://raw.githubusercontent.com/<owner>/<repo>/<ref>/<path>`) is read
  from a snapshot of that repository at that ref, cached in
  `~/.cache/peachycheck/extends/`, so an organization's folders (rules, Vale
  styles) resolve too. Pin a commit or tag: a cached ref is never
  fetched again. Other URLs work for single files without relative paths.
- **Selecting linters.** `ENABLE_LINTERS` (only these), `DISABLE_LINTERS`,
  `FILTER_REGEX_EXCLUDE` (a path regex for every linter) and, per linter,
  `<LINTER>_FILTER_REGEX_INCLUDE`, `<LINTER>_FILTER_REGEX_EXCLUDE` and
  `<LINTER>_ARGUMENTS` (appended to its command).

| key | what |
|---|---|
| `EXTENDS` | the files this one extends, resolved first |
| `PEACHYCHECK_VERSION` | the peachycheck the CI action installs |
| `LINTERS` | one entry per linter: language, extensions, command, fix, config, output parser, ... |
| `VERSIONS` | the downloaded tools, by version, URL and sha256 |
| `FILES` | under `general` or a marker file, the base files `sync` writes into `.peachycheck/` |
| `GENERATED` | the generated files `lint` skips, as a JSON manifest of the repository lists them |
| `CPP_CLANG_TIDY_COMPILE_COMMANDS` | the repository's `compile_commands.json` (default: `build/peachycheck/`, then `build/`) |

### Base and overrides

`FILES` names the files `sync` writes into the repository's `.peachycheck/`
folder: those under `general` for every repository, those under a marker
file (such as `pyproject.toml` or `package.json`) for each repository that
has it; a later entry wins, and a folder, such as Vale's styles, brings
every file below it.

```yaml
FILES:
  general: {.vale.ini: rules/.vale.ini}
  pyproject.toml: {ruff.toml: rules/ruff.toml}
  CMakeLists.txt: {.clang-tidy: rules/.clang-tidy}
``` The folder is
generated and committed, so editors and CI read it offline; `sync --check`
fails when it differs. A base file is read from `.peachycheck/`, so its
paths start there: `"../**/tests/**"` for ruff's per-file ignores,
`"extraPaths": ["../src"]` for pyright.

A repository overrides a base setting in its own configuration, which
inherits the base natively:

| tool | the repository's own file |
|---|---|
| ruff | `ruff.toml` (or `[tool.ruff]`): `extend = ".peachycheck/ruff.toml"` |
| pyright | `pyrightconfig.json`: `"extends": ".peachycheck/pyrightconfig.json"` |
| ESLint | `eslint.config.mjs`: `import base from "./.peachycheck/eslint.config.mjs"; export default [...base, ...]` |
| Vale | `.vale.ini` with `StylesPath = .peachycheck/styles` and the base's styles |
| clang-tidy | `.clang-tidy` with deviations only: `lint` lays it over the base (`Checks` joined, `CheckOptions` merged) |

A linter gets the repository's own file, else the base, else none: then
it runs with its defaults, or, for Vale and ESLint, which cannot run
without a configuration, it is skipped. Coarse switches stay in
`peachycheck.yml`: `DISABLE_LINTERS`, `<LINTER>_FILTER_REGEX_EXCLUDE`,
`<LINTER>_ARGUMENTS`.

### Linters

| linter | files | tool |
|---|---|---|
| `PYTHON_RUFF`, `PYTHON_RUFF_FORMAT` | `.py` | ruff |
| `PYTHON_PYRIGHT` | `.py` | pyright in the repository's environment (`.venv`, or `PYTHON_PYRIGHT_PYTHON`) |
| `J2LINT` | `.j2`, `.jinja`, `.jinja2` | j2lint |
| `RST_DOC8`, `SPELL_VALE`, `SPELL_CODESPELL` | `.rst` | doc8, vale, codespell |
| `TYPESCRIPT_ESLINT` | `.ts`, `.tsx` | the repository's `node_modules/.bin/eslint` |
| `CPP_CLANG_TIDY` | C++ | clang-tidy on the repository's `compile_commands.json`; results are kept per file until it, a header it includes or a `.clang-tidy` above it changes (`~/.cache/peachycheck/results`, restored and saved by the CI action) |
| `CPP_CLANG_FORMAT` | C++ | clang-format with the nearest `.clang-format` (else LLVM's style) |
| `REPOSITORY_GITLEAKS` | every file | gitleaks |

### Generated files and compile commands

`GENERATED` names a JSON manifest whose entries list generated files, which
`lint` never checks (and `init` pins no dev dependency into a generated
`pyproject.toml`):

```yaml
GENERATED: {file: manifest.json, list: sources, path: path, where: {ownership: generated}}
```

clang-tidy needs the build's `compile_commands.json`: the first of
`build/peachycheck/compile_commands.json` and `build/compile_commands.json`,
or the path(s) `CPP_CLANG_TIDY_COMPILE_COMMANDS` names. Configure CMake with
`CMAKE_EXPORT_COMPILE_COMMANDS=ON` (in CI through the action's `prepare`);
without the file, each C++ file reports that as a problem.

## Hooks

`peachycheck hooks install` adds two hooks to `~/.claude/settings.json` and
`~/.codex/hooks.json` and prints the diff (trust Codex's once with `/hooks`):

- after each edit (`Edit|Write|MultiEdit`, Codex's `apply_patch`),
  `peachycheck hook post-edit` fixes and lints the edited files and feeds the
  findings back to the agent, with every linter but the slow ones
  (`stage: stop`, by default clang-tidy);
- before the agent stops, `peachycheck hook stop` lints the files changed
  since the merge base with all linters (no builds or tests); it blocks the stop on a failure, at most twice in a row, then
  lets the agent stop and shows what remains.

Each edited file counts for its own repository (the nearest folder above it
with a `peachycheck.yml`), so a Codex patch that spans repositories is
judged file by file. The Stop hook checks every repository with changes at
or below the session's folder (its own, and side-by-side worktrees under a
parent folder, three levels deep), each with its own lint.
Files of no such repository are left alone, so one install serves the
whole user. A repository
whose `.venv` has another version of peachycheck (its dev pin) answers with
that version. `PEACHYCHECK_HOOKS=off` turns them off;
`peachycheck hooks uninstall [--claude] [--codex]` removes exactly these
entries.

## Claude Code plugin

Instead of `peachycheck hooks install`, Claude Code can take the same two
hooks from this repository as a plugin (or add the marketplace
`drexlerd/peachycheck` under a cloud project's settings):

```sh
/plugin install peachycheck --marketplace drexlerd/peachycheck
```

The plugin runs `peachycheck hook post-edit` after each `Edit|Write|MultiEdit`
and `peachycheck hook stop` before Claude stops, with the `peachycheck` on
`PATH` (install it as above). Without one, each hook prints a one-line
notice and exits 0. Use either the plugin or `peachycheck hooks install`,
not both, or each hook runs twice.

## Findings

Each file's findings come with a link to the documentation of each rule:

```text
a.py has 1 findings; fix each one:
  a.py:17: PYTHON_RUFF:N802 Function name `mixedCase` should be lowercase → https://docs.astral.sh/ruff/rules/invalid-function-name
```

Linters run on as many files at once as the machine has cores;
`PEACHYCHECK_JOBS=8` runs at most eight, where memory is short
(clang-tidy on heavy C++ needs a few GB per file).

A tool such as an agent loop can use peachycheck as a library:
`linting.lint_errors(root, base)` returns these messages for the files
changed since `base`.

## Develop

```sh
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check . && uv run pyright
uv run peachycheck lint --all
```

The tests use the sample organization in `tests/fixtures/org`.

## Release

Set `version` in `pyproject.toml`, merge, then tag the merge commit:

```sh
git tag v0.1.0 && git push origin v0.1.0
```

Before tagging, point `.github/workflows/check.yml` at the action of the
new release (`uses: drexlerd/peachycheck@v<version>`).
`.github/workflows/release.yml` checks that the tag matches the version and
that `check.yml` uses its action, runs the checks, builds with `uv build`, publishes to PyPI by trusted publishing
(environment `pypi`) and creates the GitHub release with the distributions.

Once, before the first release, add a pending publisher on PyPI (Account
settings, Publishing, "Add a new pending publisher", GitHub): project
`peachycheck`, owner `drexlerd`, repository `peachycheck`, workflow
`release.yml`, environment `pypi`; and create the environment `pypi` in this
repository's settings (Environments).

## License

peachycheck is licensed under the GNU Affero General Public License v3.0 only
(`AGPL-3.0-only`, see `LICENSE`).
