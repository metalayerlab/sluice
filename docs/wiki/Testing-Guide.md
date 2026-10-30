# Testing Guide

## Running the tests

```sh
uv run pytest                                   # unit tests, with an 80% coverage floor
uv run pytest tests/unit/test_config.py -k env  # a subset
RUN_INTEGRATION=1 uv run pytest -m integration  # a real FUSE mount (needs uv sync --extra fuse)
```

CI runs the unit tests on every supported Python version, together with ruff, mypy (strict), the wiki checks and the manifest checks. All of them block a merge.

## Test layout

| Path | What goes there |
|---|---|
| `tests/unit/` | Fast and offline: no network, no real external systems. Most tests. |
| `tests/integration/` | A real mount, marked `@pytest.mark.integration`: skipped unless `RUN_INTEGRATION=1`, and skipped without `/dev/fuse`, `fusermount3` or pyfuse3 |
| `tests/conftest.py` | Shared fixtures: `clean_env` (no `SLUICE_*` leakage), `write_config` |
| `tests/unit/test_routing.py` | Patterns, specificity, the three backend states, synthetic directories, templates, every `freeze()` validation error |
| `tests/unit/test_middleware.py` | Pipeline order, short-circuit, `failed`, fail-closed `post`, `param()` callbacks, two-path `rename`, `auth()`, `access_log()` masking |
| `tests/unit/test_dispatch.py` | The `Dispatcher` end to end against a temporary directory: the requirements' four example apps, readdir merging, rename, cleanup of failed opens |
| `tests/unit/test_inodes.py`, `test_app.py`, `test_mount.py`, `test_cli.py` | The inode table; app loading, `MountConfig` and secrets, virtual handler defaults; mount-point checks and signal handling; the commands |
| `tests/integration/test_mount.py` | `sluice mount` in a subprocess, used through the kernel; fails if the daemon logs a traceback or a token |

CI has no libfuse, so it runs only the unit tests, and `fs.py` (the pyfuse3 adapter) shows as uncovered there. Run the integration tests before a PR that touches `fs.py`, `mount.py` or the dispatcher.

## Writing tests

- Every behaviour change needs a test that fails without it. A bug fix starts with the failing test.
- Assert outcomes (outputs, side effects, errors), not internal calls.
- Use placeholders for anything that looks real: `example.org`, `192.0.2.0/24`, fake tokens.
- Don't lower the coverage floor or skip a test to make CI pass.

- **Test through the `Dispatcher`, not FUSE.** Build an app, `freeze()` it, and wrap it in `Dispatcher(table, Passthrough(2))`. Walk paths with `lookup` from `ROOT_INODE`, and call `open`/`read`/`readdir` as the kernel would. See `walk()` and `cat()` in `test_dispatch.py`. The backend is a real temporary directory (`tmp_path`): local disk is fine in unit tests, the kernel is not. Call `shutdown()` on the `Passthrough` at the end.
- **Async without plugins:** the tests drive coroutines with `asyncio.run(...)`. There is no pytest-asyncio.
- **The pipeline alone:** `run_pipeline(Context(op, table.resolve(path), config=MountConfig()), operation)` with a stub `async def operation(ctx)`. A `Recorder` middleware that appends to a list makes order assertions easy.
- **Apps for the CLI:** write a module into `tmp_path`, set `SLUICE_APP_MODULE` and `SLUICE_APP_DIR`, and remove it from `sys.modules` afterwards (see the `app_env` fixture in `test_cli.py`).
- **Errors:** assert the errno (`pytest.raises(FsError)` and `e.value.errno`), not the message.
- **A real mount:** copy the `mounted` fixture in `tests/integration/test_mount.py`: it starts `python -m sluice mount`, waits for `os.path.ismount`, and on exit sends SIGTERM and checks for exit 0, an unmounted directory and a clean log.
