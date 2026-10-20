# verdog-ai standards

The coding and documentation standards of every verdog-ai repository, and the
one check that enforces them. There is no repository-local specialization:
each repository uses the same rules, checkers and tool configurations.

## What is here

- `standards/`: the instance. `general/` applies to every file; each folder of
  `languages/` (C++, Python, reStructuredText, TypeScript, Verdog) holds its
  `language.toml`, `guidelines.toml` (one table per guideline, with good and
  bad examples) and `tools.toml` (its checkers). `metrics.toml` declares the
  code metrics, `literature.bib` what guidelines and metrics cite.
- `templates/<type>/`: the project types. `type.toml` names the marker files
  that recognise a type and the commands its check runs; `files/` holds the
  root tool configurations (`ruff.toml`, `pyrightconfig.json`,
  `eslint.config.mjs`, `.clang-tidy`) every repository
  of that type shares.
- `src/verdog_standards/`: the engine and the CLI `verdog-standards`.
- `.github/workflows/check.yml`: the reusable workflow every repository calls.

| type | markers | commands |
|---|---|---|
| python-package | `pyproject.toml`, `uv.lock` | uv sync, ruff check, ruff format, pyright, pytest |
| verdog-project | `project.json` | verdog check and sync, ruff, pyright, pytest |
| typescript | `package.json` | npm ci, `npm run check`, `npm test` |
| cpp | `CMakeLists.txt` | cmake configure, build, ctest |

A repository may have several types, such as C++ with Python bindings.

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

## Licenses

`standards/` quotes or pins third-party guides; see
`standards/languages/*/references/SOURCES.md`. The Google style guides are
CC BY 3.0 and Diátaxis CC BY-SA 4.0, both committed with attribution. The C++
Core Guidelines are not committed: `references/downloads.toml` pins them by
commit and sha256, and the engine fetches them into
`~/.cache/verdog-standards/` when a check needs them.
