# In-Depth Configuration

## Where configuration comes from

Every setting can be given in four places, and the first one that has it wins:

1. `SLUICE_<KEY>_FILE`: the path of a file holding the value (Docker and Kubernetes secrets)
2. `SLUICE_<KEY>`: an environment variable
3. the YAML config file: `SLUICE_CONFIG` or `--config`, default `/etc/sluice/config.yaml`. A missing file is fine.
4. the built-in default

`<KEY>` is the setting's key in upper case, with the dots as underscores: `listen.port` is `SLUICE_LISTEN_PORT`.

Values are checked strictly against the setting's type, and a wrong one stops sluice with a one-line error rather than being converted: a whole-number setting takes `500` but not `0.5`, `true` or `1_000` from the environment; a text setting takes a single value, not a YAML list. A `_FILE` value has its trailing whitespace (the newline) removed; leading spaces are kept. sluice gets these rules from [interpose](https://github.com/metalayerlab/interpose), the settings library it shares with other metalayerlab tools.

Two things sluice does not do, though interpose can: an unknown key in the config file, or a `SLUICE_*` variable no setting reads, is ignored without a warning, so check the spelling against the table below; and `${VAR}` in the config file is not expanded, but read as written.

`sluice config` prints the effective value of every setting and where it came from, with secrets masked.

## Settings

Generated from the code: don't edit the table, run `uv run scripts/gen-wiki.py`.

<!-- generated:start settings -->
| Key | Environment variable | Default | Description |
|---|---|---|---|
| `log_level` | `SLUICE_LOG_LEVEL` | `INFO` | Log level: `DEBUG`, `INFO`, `WARNING` or `ERROR`. |
| `app.module` | `SLUICE_APP_MODULE` | *(none)* | The app to serve, `module:attribute` (e.g. `myroutes:app`). Required by `mount`, `routes` and `resolve`. |
| `app.dir` | `SLUICE_APP_DIR` | *(none)* | Directory put first on `sys.path` before importing `app.module` (e.g. a mounted ConfigMap). Empty: `sys.path` is unchanged. |
| `mount.point` | `SLUICE_MOUNT_POINT` | *(none)* | Where to mount: an existing, empty directory. Required by `mount`. |
| `mount.fsname` | `SLUICE_MOUNT_FSNAME` | `sluice` | Filesystem name shown by `mount` and `df`. |
| `mount.allow_other` | `SLUICE_MOUNT_ALLOW_OTHER` | `False` | Let users other than the mounting one in. Needs `user_allow_other` in `/etc/fuse.conf`. With it on, middleware is the only gate. |
| `mount.cache_ms` | `SLUICE_MOUNT_CACHE_MS` | `0` | Kernel entry and attribute cache lifetime, in milliseconds. Above 0, cached calls skip middleware. |
| `mount.debug` | `SLUICE_MOUNT_DEBUG` | `False` | libfuse request tracing. Logs raw paths, including any token in them: lab use only. |
| `io.threads` | `SLUICE_IO_THREADS` | `8` | Worker threads for backend syscalls (at least 1). |
| `env.expose` | `SLUICE_ENV_EXPOSE` | *(empty list)* | Environment variable names copied into `ctx.config.env`. Empty: none. `SLUICE_*` names are refused. A list: YAML, or comma-separated. |
| `secrets.dir` | `SLUICE_SECRETS_DIR` | `/run/secrets` | Each regular file in it becomes `ctx.config.secret[<filename>]` (trailing newline stripped). A missing directory means no secrets. |
| `pipeline.slow_ms` | `SLUICE_PIPELINE_SLOW_MS` | `0` | Log a warning when a middleware or handler holds the event loop longer than this, in milliseconds. 0: off. |
| `pipeline.timeout_ms` | `SLUICE_PIPELINE_TIMEOUT_MS` | `0` | Fail a call with `EIO` when one middleware hook runs longer than this, in milliseconds; the hook is cancelled at its current `await`. 0: off. |
| `backend.max_abandoned` | `SLUICE_BACKEND_MAX_ABANDONED` | `0` | How many backend calls cancelled by a timeout may still be running in I/O threads before new backend calls fail with `EIO` instead of queueing. At most `io.threads`; 0: half of `io.threads`, at least 1. |
| `metrics.enabled` | `SLUICE_METRICS_ENABLED` | `False` | Serve Prometheus metrics as the read-only file `/.sluice/metrics` in the mount, and reserve `/.sluice` for sluice. Off: no file, and nothing is counted. |
<!-- generated:end settings -->

Notes on what the table can't say:

- **What `mount` needs.** `app.module` and `mount.point` have no useful default. `sluice routes` and `sluice resolve` need only `app.module`. The mount point must be an existing, empty directory.
- **`app.dir`** goes first on `sys.path`, so a module there shadows an installed one of the same name. In a container, mount the app (a ConfigMap, a bind mount) and point `app.dir` at it.
- **`mount.cache_ms`** is the trade between speed and per-call control. At 0 (the default) the kernel asks sluice about every lookup and attribute read, so middleware sees every call, at the cost of many more calls. Above 0, the kernel answers repeated lookups and `stat`s from its cache for that long, **without running middleware**: a denial or a changed route takes up to that long to apply. File contents are never cached (no `keep_cache`).
- **`mount.allow_other`** needs `user_allow_other` in `/etc/fuse.conf` unless sluice runs as root. Without it only the mounting user can enter the mount. With it, any local user can, and sluice still accesses the backing store with its own uid: your middleware is then the only gate. The kernel also checks the mode bits sluice reports (`default_permissions`).
- **`mount.debug`** makes libfuse print every request to stderr, with raw paths. Tokens in paths are *not* masked there. Use it on your own machine only.
- **`io.threads`** bounds how many backend syscalls run at once. Each one occupies a thread until the disk answers. Raise it for slow or network-backed stores with many concurrent readers.
- **`env.expose`** is a list: a YAML list (`expose: [REGION, HOME]`) or a comma-separated string (`SLUICE_ENV_EXPOSE=REGION,HOME`). It is an allowlist: only these variables reach `ctx.config.env` and `{env.NAME}` in backend templates. Every name must be set when sluice starts (`env.expose: not set in the environment: X` otherwise), because templates are rendered with it. `SLUICE_*` names are refused so that sluice's own settings never leak into routes.
- **`secrets.dir`** and **`env.expose`** are read **once, at mount time**. Changing a secret file or variable takes a remount.
- **`pipeline.slow_ms`** turns on asyncio's debug mode, which has its own overhead. Turn it on to find a blocking middleware, then off again.
- **`pipeline.timeout_ms`** (off by default) bounds each middleware hook call (`pre`, `post` and `failed` separately). A hook that runs longer is **cancelled at its current `await`** and the call fails with `EIO`, as for any other hook failure (a timed-out `failed` hook is ignored). Hooks must then be cancel-safe: see [Writing an App](Writing-an-App#timeouts-and-cancellation). `param()` callbacks, the operation and virtual handlers aren't bounded. Choose a limit well above your slowest healthy hook: a `post` that times out fails a call whose operation already happened.
- **`backend.max_abandoned`** only matters with a timeout. A cancelled hook's backend call (`ctx.backend`) can't be stopped once its syscall runs: it keeps its I/O thread until it returns. While this many such calls are still running, every new backend call fails at once with `EIO` instead of queueing behind them, so a hung backend (a stale NFS mount) can't take the whole pool; calls work again as they finish. Closing a file is never refused. Keep it at most `io.threads`; the default, half the pool, leaves the other half to healthy backends.
- **`metrics.enabled`** adds the read-only file `/.sluice/metrics` (see [Metrics](#metrics)) and reserves `/.sluice`: an app route under it is then a route error.

## Metrics

With `metrics.enabled: true`, `cat <mount.point>/.sluice/metrics` prints Prometheus text, rendered when the file is opened. Counting is in memory; nothing is written anywhere. Global middleware (`app.use()`) runs on the file like on any other path, so your authorisation applies to it.

| Metric | Labels | What |
|---|---|---|
| `sluice_call_duration_seconds` (histogram) | `op` | Every filesystem call, failed ones included (not one the kernel cancelled); `_count` is the number of calls |
| `sluice_call_errors_total` | `op`, `errno` | Failed calls by the errno the caller got (`ENOENT`, `EACCES`, ...; `unknown` for a number outside the errno table) |
| `sluice_middleware_seconds_total` | `middleware` | Seconds spent in each middleware's hooks; only hooks it overrides are called and timed, so a middleware has no series until one of them has run |
| `sluice_middleware_failures_total` | `middleware`, `hook` | `pre` and `post` hooks that raised something other than `FsError`/`Deny` or timed out, and `failed` hooks that raised anything |
| `sluice_backend_abandoned_total` | | Backend calls left running in an I/O thread after a timeout cancelled their hook |
| `sluice_backend_abandoned` (gauge) | | Those still running now |
| `sluice_backend_refused_total` | | Backend calls failed with `EIO` because `backend.max_abandoned` were still running |

`op` is one of the operations middleware sees (`forget`, `release` and `releasedir` aren't counted), and `middleware` is the name `sluice routes` prints; when one call's chain has the same name more than once (a middleware registered globally and on a route, two instances of one class), the second is `name#2`, the third `name#3`, and so on, each with its own series. No label ever holds a path, a route param or a user: per-path detail is what an access-log middleware is for. A denial counts as an error of its call, not as a middleware failure.

## Secrets

sluice has two kinds of secret.

**Secrets for your app** (a database password for a config template, a key for `verify()`) come from `secrets.dir`. Every regular file in it becomes `ctx.config.secret[<filename>]`, with one trailing newline stripped. Names that start with `.` are skipped (Kubernetes keeps `..data` there), and a missing directory means no secrets. They are `Secret` objects: `str()` and `repr()` give `********`, and `.reveal()` gives the value. Only their names are logged, once at startup. They can't be used in backend templates. The default, `/run/secrets`, is where Docker and Kubernetes put them:

- Docker: Compose `secrets:` mounts each one at `/run/secrets/<name>`; with plain `docker run`, bind-mount a directory of files there read-only
- Kubernetes: a Secret mounted as a volume at `/run/secrets` (SOPS-encrypted in the GitOps repo)
- systemd: `LoadCredential=<name>:<path>` and `SLUICE_SECRETS_DIR=%d`

**Settings** follow the general rule: never put one that is sensitive in the config file or in a plain environment variable that `docker inspect` or `ps` would show. Use the `_FILE` variant and mount the value as a file:

- Docker: `SLUICE_<KEY>_FILE=/run/secrets/<name>` with a secrets file or bind mount
- Kubernetes: a Secret mounted as a volume (SOPS-encrypted in the GitOps repo)
- systemd: `LoadCredential=` and `%d/<name>`

None of sluice's own settings is a secret, so `sluice config` masks nothing today. The values that are sensitive are your app's, in `secrets.dir`, and tokens that appear in paths (mark their params `secret=True`, see [Writing an App](Writing-an-App#param-callbacks)).

## Examples

A complete config file with every default is in [contrib/sluice.example.yaml](https://github.com/metalayerlab/sluice/blob/main/contrib/sluice.example.yaml).

**On your own machine**, everything from the environment, mounted in your home directory:

```sh
SLUICE_APP_DIR=~/sluice-app SLUICE_APP_MODULE=routes:app \
SLUICE_MOUNT_POINT=~/mnt/sluice SLUICE_SECRETS_DIR=~/sluice-app/secrets \
sluice mount
```

**A container serving several users**, with the app from a ConfigMap, secrets from a Secret volume, and an allowlisted region used in backend templates (`backend="/data/{env.REGION}/{user}"`):

```yaml
# /etc/sluice/config.yaml
app:
  module: routes:app
  dir: /etc/sluice/app
mount:
  point: /mnt/sluice
  allow_other: true   # other containers' users reach the mount; middleware is the gate
env:
  expose: [REGION]
secrets:
  dir: /run/secrets
```

**Metrics for Prometheus**, through node_exporter's textfile collector (copy the file on a timer; `cp` reads it in one open):

```sh
SLUICE_METRICS_ENABLED=true sluice mount
# elsewhere, every 15 s:
cp /mnt/sluice/.sluice/metrics /var/lib/node_exporter/textfile/sluice.prom.tmp &&
  mv /var/lib/node_exporter/textfile/sluice.prom.tmp /var/lib/node_exporter/textfile/sluice.prom
```

**Finding a blocking middleware**, with debug logging on:

```sh
SLUICE_LOG_LEVEL=DEBUG SLUICE_PIPELINE_SLOW_MS=20 sluice mount
```
