# Stack: Python

Python >= 3.13 with uv: always `uv run`, never `pip install` or activate the venv. Done = code + tests + wiki.

- **Design records:** `docs/design/NNNN-*.md` from `0000-template.md`, a line in its `README.md` index (read only what it names). New feature, dependency or public interface (config keys, CLI, HTTP API, file formats): write one first.
- Never re-propose `TODO.md`'s "Decided against".

## Don't read

`uv.lock` (use `uv tree --depth 1`), `CHANGELOG.md` (use `git log`), `.venv/`, `htmlcov/`, `.coverage`, `.mypy_cache/`, `.ruff_cache/`, `.pytest_cache/`; generated wiki blocks (`<!-- generated:start/end -->`).

## Commands

| Task | Command |
|---|---|
| Install / add | `uv sync` / `uv add <pkg>` (dev: `--dev`) |
| Lint, format | `uv run ruff check --fix`, `uv run ruff format` |
| Types (strict) | `uv run mypy` |
| Tests (80% floor) / integration | `uv run pytest` / `RUN_INTEGRATION=1 uv run pytest -m integration --no-cov` |
| Wiki sections | `uv run scripts/gen-wiki.py` |
| Run | `uv run sluice --help` |

## Repository map

| Path | What |
|---|---|
| `src/sluice/` | The package: `config.py`, `cli.py` (Typer); routing, middleware, dispatch (no pyfuse3); `fs.py`, `mount.py` (the only pyfuse3 users) |
| `examples/` | Example middleware, copied beside an app (not packaged); tested in `tests/examples/` |
| `benchmarks/` | Manual benchmarks; not run by pytest or CI |
| `tests/unit/`, `tests/integration/` | pytest; integration is marked and opt-in |
| `docs/wiki/`, `docs/design/` | The wiki (`.standard.toml`, T); design records |
| `docs/ai/` | Prompts, review checklist (T) |
| `contrib/` | `system-packages.txt` (apt `build:`/`runtime:`) |
| `Dockerfile` | Image (T skeleton) |
| `CHANGELOG.md`, `pyproject.toml` version, `.release-please-manifest.json` | release-please only |

## Conventions

- **Types:** full annotations, `mypy --strict`. A `# type: ignore` or `noqa` states its reason.
- **Style:** ruff (line length 100). Comments say *why*. No `print`: `logging`; only the CLI writes stdout (`typer.echo`).
- **Config:** every setting through `config.SETTINGS`: `SLUICE_<KEY>_FILE` > `SLUICE_<KEY>` > file > default. Secrets: `secret=True`, from `_FILE`, never logged. A secret is a `Secret`: `.reveal()` where it's used; `str()` gives `********`.
- **Errors:** specific exceptions; the CLI prints one line and exits 2. No bare `except:`.
- **Dependencies:** only when the stdlib or an existing one can't; justify it, and check the name exists.
- **Tests:** each behaviour change gets a test that fails without it; no network in unit tests.
- **Never:** edit `CHANGELOG.md`, the version or generated wiki blocks; lower the coverage floor or skip a test; add `no-docs-needed` (reviewers only), telemetry or import-time network calls.

## Definition of done

`sh scripts/checks.sh` (wiki standard, version, lock), then `uv run ruff check && uv run ruff format --check`, `uv run mypy`, `uv run pytest`, `uv run scripts/gen-wiki.py --check`, `scripts/check-manifests.sh`; with Docker, `scripts/check-markdown.sh`. And your change's docs-map pages.

## Docs map

| You changed | Update |
|---|---|
| a setting, a command | In-Depth-Configuration, CLI-Reference (regenerate) |
| components; security; a failure mode; tests | Architecture-Internals (+ a record); Security; Troubleshooting; Testing-Guide |

Wiki links use page names (`[Quick Start](Quick-Start)`); link new pages from `_Sidebar.md`.
