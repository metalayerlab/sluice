# CLI Reference

`sluice --help` lists the commands. `sluice <command> --help` shows a command's options.

## Commands

Generated from the code: don't edit, run `uv run scripts/gen-wiki.py`.

<!-- generated:start cli -->
### `sluice config`

Print the effective configuration and where each value came from. Secrets are masked.

| Option | Environment variable | Description |
|---|---|---|
| `--config`, `-c` | `SLUICE_CONFIG` | Config file (default /etc/sluice/config.yaml; missing is fine). |

### `sluice mount`

Mount the app at `mount.point` and serve in the foreground.

Imports `app.module`, loads the mount-time environment and secrets, validates the routes,
mounts, and serves until SIGINT or SIGTERM, then unmounts. Unmount a crashed mount with
`fusermount3 -u <mount.point>`.

| Option | Environment variable | Description |
|---|---|---|
| `--config`, `-c` | `SLUICE_CONFIG` | Config file (default /etc/sluice/config.yaml; missing is fine). |

### `sluice resolve`

Dry-run the router for one virtual path.

Prints the matched route, params (secret ones masked), the real path, and the middleware
that would run for `--op`. Runs no middleware and no `param()` callbacks.

| Argument | Description |
|---|---|
| `PATH` | A virtual path under the mount, e.g. /fuse/a/b. |

| Option | Environment variable | Description |
|---|---|---|
| `--op` |  | The operation whose middleware to list. |
| `--config`, `-c` | `SLUICE_CONFIG` | Config file (default /etc/sluice/config.yaml; missing is fine). |

### `sluice routes`

Validate the routes without mounting, and print the tree.

Each route shows its backend state (own backend, inherited from an ancestor, virtual, or
grouping) and the full middleware chain that runs beneath it, global middleware first.

| Option | Environment variable | Description |
|---|---|---|
| `--config`, `-c` | `SLUICE_CONFIG` | Config file (default /etc/sluice/config.yaml; missing is fine). |

### `sluice version`

Print the version.
<!-- generated:end cli -->

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Failure while running |
| 2 | Bad usage or invalid configuration |

<!-- guidance: add project-specific exit codes, and worked examples of common invocations. -->
