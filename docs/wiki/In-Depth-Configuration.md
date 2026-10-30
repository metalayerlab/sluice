# In-Depth Configuration

## Where configuration comes from

Every setting can be given in four places, and the first one that has it wins:

1. `SLUICE_<KEY>_FILE`: the path of a file holding the value (Docker and Kubernetes secrets)
2. `SLUICE_<KEY>`: an environment variable
3. the YAML config file: `SLUICE_CONFIG` or `--config`, default `/etc/sluice/config.yaml`. A missing file is fine.
4. the built-in default

`<KEY>` is the setting's key in upper case, with the dots as underscores: `listen.port` is `SLUICE_LISTEN_PORT`.
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
| `env.expose` | `SLUICE_ENV_EXPOSE` | *(none)* | Comma-separated environment variable names copied into `ctx.config.env`. Empty: none. `SLUICE_*` names are refused. |
| `secrets.dir` | `SLUICE_SECRETS_DIR` | `/run/secrets` | Each regular file in it becomes `ctx.config.secret[<filename>]` (trailing newline stripped). A missing directory means no secrets. |
| `pipeline.slow_ms` | `SLUICE_PIPELINE_SLOW_MS` | `0` | Log a warning when a middleware or handler holds the event loop longer than this, in milliseconds. 0: off. |
<!-- generated:end settings -->

Notes on what the table can't say:

- **What `mount` needs.** `app.module` and `mount.point` have no useful default. `sluice routes` and `sluice resolve` need only `app.module`. The mount point must be an existing, empty directory.
- **`app.dir`** goes first on `sys.path`, so a module there shadows an installed one of the same name. In a container, mount the app (a ConfigMap, a bind mount) and point `app.dir` at it.
- **`mount.cache_ms`** is the trade between speed and per-call control. At 0 (the default) the kernel asks sluice about every lookup and attribute read, so middleware sees every call, at the cost of many more calls. Above 0, the kernel answers repeated lookups and `stat`s from its cache for that long, **without running middleware**: a denial or a changed route takes up to that long to apply. File contents are never cached (no `keep_cache`).
- **`mount.allow_other`** needs `user_allow_other` in `/etc/fuse.conf` unless sluice runs as root. Without it only the mounting user can enter the mount. With it, any local user can, and sluice still accesses the backing store with its own uid: your middleware is then the only gate. The kernel also checks the mode bits sluice reports (`default_permissions`).
- **`mount.debug`** makes libfuse print every request to stderr, with raw paths. Tokens in paths are *not* masked there. Use it on your own machine only.
- **`io.threads`** bounds how many backend syscalls run at once. Each one occupies a thread until the disk answers. Raise it for slow or network-backed stores with many concurrent readers.
- **`env.expose`** is an allowlist: only these variables reach `ctx.config.env` and `{env.NAME}` in backend templates. Every name must be set when sluice starts (`env.expose: not set in the environment: X` otherwise), because templates are rendered with it. `SLUICE_*` names are refused so that sluice's own settings never leak into routes.
- **`secrets.dir`** and **`env.expose`** are read **once, at mount time**. Changing a secret file or variable takes a remount.
- **`pipeline.slow_ms`** turns on asyncio's debug mode, which has its own overhead. Turn it on to find a blocking middleware, then off again.

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
  expose: REGION
secrets:
  dir: /run/secrets
```

**Finding a blocking middleware**, with debug logging on:

```sh
SLUICE_LOG_LEVEL=DEBUG SLUICE_PIPELINE_SLOW_MS=20 sluice mount
```
