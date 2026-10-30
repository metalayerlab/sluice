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

<!-- guidance: below the table, explain anything a one-line description can't: how settings interact, valid ranges, performance trade-offs. -->

## Secrets

Never put a secret in the config file or in a plain environment variable that `docker inspect` or `ps` would show. Use the `_FILE` variant and mount the value as a file:

- Docker: `SLUICE_<KEY>_FILE=/run/secrets/<name>` with a secrets file or bind mount
- Kubernetes: a Secret mounted as a volume (SOPS-encrypted in the GitOps repo)
- systemd: `LoadCredential=` and `%d/<name>`

<!-- guidance: list this project's secrets, or say there are none. -->

## Examples

A complete config file with every default is in [contrib/sluice.example.yaml](https://github.com/metalayerlab/sluice/blob/main/contrib/sluice.example.yaml).

<!-- guidance: two or three real-world configurations, each with a sentence on when you'd use it. -->
