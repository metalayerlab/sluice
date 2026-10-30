# Testing Guide

## Running the tests

```sh
uv run pytest                                   # unit tests, with an 80% coverage floor
uv run pytest tests/unit/test_config.py -k env  # a subset
RUN_INTEGRATION=1 uv run pytest -m integration  # integration tests (real external systems)
```

CI runs the unit tests on every supported Python version, together with ruff, mypy (strict), the wiki checks and the manifest checks. All of them block a merge.

## Test layout

| Path | What goes there |
|---|---|
| `tests/unit/` | Fast and offline: no network, no real external systems. Most tests. |
| `tests/integration/` | Real dependencies, marked `@pytest.mark.integration`; skipped unless `RUN_INTEGRATION=1` |
| `tests/conftest.py` | Shared fixtures: `clean_env` (no `SLUICE_*` leakage), `write_config` |

<!-- guidance: add fixtures, test data directories and fakes as they appear. -->

## Writing tests

- Every behaviour change needs a test that fails without it. A bug fix starts with the failing test.
- Assert outcomes (outputs, side effects, errors), not internal calls.
- Use placeholders for anything that looks real: `example.org`, `192.0.2.0/24`, fake tokens.
- Don't lower the coverage floor or skip a test to make CI pass.

<!-- guidance: project-specific patterns: how to fake the upstream API, time, the filesystem. -->
