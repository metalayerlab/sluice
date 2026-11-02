# 0003: Adopt interpose for settings and secrets

<!-- Add a line for this record to the index in docs/design/README.md (CI checks). -->

Status: Proposed
Date: 2026-11-02
Authors: Stephen Cox
Supersedes: the `env.expose` type (str, comma-separated) in [0001](0001-initial-design.md)'s settings table

## Context

sluice carries its own settings engine (`config.py`: lookup order, coercion, `_FILE`) and its own `Secret`
type and secrets-directory reader (`context.py`). mitm-proxy and the project template carry copies of the
engine, and the three copies coerce values differently (interpose's design 0001 has the table). The
shared mechanism now lives in **interpose** ([metalayerlab/interpose](https://github.com/metalayerlab/interpose)),
whose v0.1.0 is sluice's engine and `Secret` moved out, plus list settings and per-setting checks.

Constraints: every config key and environment variable stays as it is; `config.SETTINGS`,
`config.CONFIG_ENV` and `config.ConfigError` stay importable (the CLI, `scripts/gen-wiki.py` and
`sluice.errors` use them); `sluice.Secret` stays importable for apps; values are still read once, at mount
time, never per call (TODO.md, "Decided against").

## Decision

- **Dependency:** `interpose @ git+https://github.com/metalayerlab/interpose@v0.1.0`, a tag on GitHub,
  never PyPI, as the first entry of `dependencies` in PEP 508 form (not `[tool.uv.sources]`, see
  below). hatchling needs `[tool.hatch.metadata] allow-direct-references = true` for it. Both follow
  template-python's decision 0003 to the letter, so its template update merges cleanly. `pyyaml` stays
  listed, as the template keeps it.
  uv fetches a git dependency with `git`, so `contrib/system-packages.txt` adds it to `build:` (CI's
  test job and the image's builder stage; the runtime image doesn't get it).
- **`config.py`** becomes one `SETTINGS = Settings("SLUICE_", [Setting(...), ...],
  default_path=Path("/etc/sluice/config.yaml"))`, the only place `Settings` is built, with
  `CONFIG_ENV = SETTINGS.config_env`, and `ConfigError` re-exported from `interpose.settings`.
  `load_config` calls `SETTINGS.resolve()` and builds the same `Config` dataclass.
- **Single-value rules move to `check=`:** `log_level` → `one_of("DEBUG", "INFO", "WARNING", "ERROR")`,
  `mount.cache_ms` and `pipeline.slow_ms` → `non_negative`, `io.threads` → `positive`.
- **`env.expose` becomes a list setting** (`is_list=True`): a YAML list or a comma-separated string, as
  before. Its rules (no `SLUICE_*` names, valid variable names) are sluice policy and stay in sluice, as a
  check function in `config.py` (interpose 0001, question 3).
- **No rule spans settings today.** One would go in `load_config`, after `resolve()`.
- **`context.py`** uses interpose's `Secret` and `read_secret_dir`; `sluice.Secret` is that class.
  `MASK` comes from `interpose.secret`.
- **Tests:** the engine tests interpose now covers (precedence, coercion, `_FILE`, YAML errors, `Secret`)
  go; sluice keeps tests for its keys and variables, defaults, checks and `env.expose` rules.

Behaviour a user can notice (all from interpose's stricter, shared rules):

| | Before | After |
|---|---|---|
| `env.expose` in YAML | a string only; a list was an error | a list or a string |
| An int from the environment | `int()`: `"1_000"` was `1000` | ASCII digits with a sign only: `"1_000"` is an error |
| `_FILE` contents | both ends stripped | trailing whitespace stripped; leading spaces kept |
| A YAML syntax error | `cannot read <path>: <pyyaml's message>` | `cannot parse <path> at line L, column C: <problem>` (no quoted text) |
| A coercion error | `mount.cache_ms: expected int, got 0.5` | the same, plus `(from file)` / `(from SLUICE_...)` |
| `io.threads: 0` | `must be at least 1, got 0` | `must be greater than 0, got 0` |
| A bad `log_level` | names the upper-cased value | names the value as given |
| `secrets.dir` errors | `secrets.dir: cannot read ...` | `cannot read secrets directory <dir>: ...`, `cannot read secret '<name>' in <dir>: ...` |
| `Secret` | `==` compares values | constant-time `==`; truthy when not empty; masked in any format spec |

## Alternatives considered

- **Keep sluice's own engine:** the copies keep drifting, and sluice misses list settings and checks.
- **Pin interpose with `[tool.uv.sources]`** (`git` + `tag`) and a bare `interpose` dependency: only uv
  reads it, so a wheel or sdist built from sluice would ask PyPI for `interpose`, which isn't ours.
- **Move the `env.expose` name rules into interpose:** they are sluice policy; interpose ships
  mechanism (interpose 0001, question 3).
- **Re-wrap `read_secret_dir`'s errors with a `secrets.dir:` prefix:** extra code to keep a message
  format; the new messages already name the directory and file.

## Consequences

- An engine fix reaches sluice as an interpose version bump. Dependabot can't propose that bump for a
  tag-pinned git dependency (interpose 0001, question 4); a scheduled tag check will come from
  template-python, so this record adds none.
- The table above is the migration note. Rollback is reverting the change: the keys and variables are
  unchanged.
- Wiki pages: In-Depth-Configuration (regenerated settings table, `env.expose` as a list, `_FILE`
  stripping), Troubleshooting (the new messages), Architecture-Internals (where the engine lives),
  Security (`Secret` from interpose).

## Open questions

1. **Is pinning `v0.1.0` exactly right, rather than a range?** Recommend yes: a git dependency has no
   ranges, and a bump is a reviewed PR.
