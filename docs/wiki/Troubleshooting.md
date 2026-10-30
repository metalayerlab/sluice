# Troubleshooting

## Diagnostics

- **Effective configuration:** `sluice config` prints every setting and where it came from (default, file, env var). Secrets are masked.
- **More logging:** `SLUICE_LOG_LEVEL=DEBUG`.
- **Container:** `docker logs sluice`, and `docker inspect --format '{{json .State.Health}}' sluice`.

## Common problems

<!-- guidance: one ### heading per symptom, in the user's words ("Exits with 'cannot read ..._FILE'"), then the cause and the fix. Add one whenever a bug report or a support question shows a new failure mode. -->

### `error: cannot read SLUICE_..._FILE`

The file named by a `_FILE` variable doesn't exist, or the process can't read it. Check the path inside the container or unit, and the file's owner and mode: the process runs as uid 1000 / the `sluice` user.
