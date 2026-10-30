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
| 0 | Success. For `mount`: unmounted cleanly after SIGINT, SIGTERM or `fusermount3 -u`. |
| 1 | An unexpected error: a bug in sluice or the app. The traceback is logged. |
| 2 | An expected error, printed as one `error: ...` line: bad usage, invalid configuration, the app can't be imported, the routes don't validate, or the filesystem can't be mounted. |

A single failed filesystem call never ends the mount: the caller gets an errno, and sluice carries on.

## Examples

Check an app's routes, and see what each route inherits and which middleware runs beneath it:

```console
$ SLUICE_APP_MODULE=myroutes:app sluice routes
/home/{user}/{token}  [backend /srv/data/{user}]
  middleware: access_log(), auth(verify)
  /home/{user}/{token}/status  [virtual]
    middleware: access_log(), auth(verify)
```

Find out where a path goes, and what would run for a `read`, without mounting or running any middleware:

```console
$ sluice resolve /home/alice/T0k/notes.txt --op read
path:       /home/alice/********/notes.txt
route:      /home/{user}/{token}
kind:       backed
params:     user=alice, token=********
real path:  /srv/data/alice/notes.txt
param():    (none)
middleware: access_log(), auth(verify)  (for read)
```

A path that no route covers prints `no route: ENOENT`, and one that would leave its backend prints `refused: EACCES: path escapes its backend`. Both exit 0: the dry run worked.

Mount in the foreground with a config file, and unmount a mount whose process died:

```sh
sluice mount -c /etc/sluice/config.yaml
fusermount3 -u /mnt/sluice
```
