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
