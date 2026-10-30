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
