# Troubleshooting

## Diagnostics

- **Effective configuration:** `sluice config` prints every setting and where it came from (default, file, env var). Secrets are masked.
- **More logging:** `SLUICE_LOG_LEVEL=DEBUG`.
- **Container:** `docker logs sluice`, and `docker inspect --format '{{json .State.Health}}' sluice`.

## Common problems

### `Transport endpoint is not connected`

sluice stopped (crashed or was killed) without unmounting, and the kernel still has the mount. Every access fails, and `sluice mount` refuses the directory (`cannot read ...: a stale mount?`). Unmount it with `fusermount3 -u <mount.point>` (`umount -l` as root if it's busy), then start sluice again. Processes that had files open there need to reopen them.

### `error: /dev/fuse not found`

The FUSE kernel module isn't loaded, or (in a container) the device wasn't passed in. On the host: `sudo modprobe fuse`. In Docker: `--device /dev/fuse --cap-add SYS_ADMIN`. In Kubernetes: a FUSE device plugin, or a privileged pod. See [Deployment](Deployment).

### `error: cannot mount at ...: fuse_session_mount failed`

libfuse logged its reason just above. The common ones:

- `fusermount3: option allow_other only allowed if 'user_allow_other' is set in /etc/fuse.conf`: add that line to `/etc/fuse.conf`, or turn `mount.allow_other` off.
- `fusermount3: mount failed: Operation not permitted`: in a container, it lacks `CAP_SYS_ADMIN` or is confined by AppArmor or seccomp. See [Deployment](Deployment).
- `fusermount3: failed to access mountpoint`: the directory vanished, or its parent isn't searchable by sluice's user.

### `error: mount.point: ... is not empty`

FUSE can mount over a non-empty directory, but sluice refuses to so that files can't silently disappear under the mount. Use an empty directory.

### `error: pyfuse3 is not installed ...: install sluice[fuse]`

sluice was installed without the `fuse` extra. Reinstall with it (`uv tool install 'sluice[fuse] @ ...'`, or `uv sync --extra fuse` in a checkout). Building pyfuse3 needs libfuse3 headers, `pkg-config` and a C compiler: see [Quick Start](Quick-Start#prerequisites).

### `error: app.module: cannot import ...`

The module isn't on `sys.path`, or importing it raised the error shown. Set `app.dir` to the directory that holds it, and check that `python -c 'import <module>'` works from there. `app.module` is `module:attribute`, e.g. `routes:app`.

### `error: /fuse/{user}: backend uses {x}, which no route captures` (and other route errors)

The routes don't validate. The message names the route and the problem. Fix the app, and use `sluice routes` to check it without mounting. The full list of checks is in [Writing an App](Writing-an-App#what-freeze-checks).

### `error: env.expose: not set in the environment: NAME`

A variable in the `env.expose` allowlist isn't set in sluice's environment. Set it, or remove it from the list. Every exposed name must exist, because backend templates are rendered with it.

### Every access returns `Input/output error`

A middleware, `param()` callback or virtual handler raised something other than `FsError` or `Deny`. sluice logs it at ERROR, with a traceback and the call's correlation id (`[1f]`), and returns `EIO`. A `post` hook that raises always gives `EIO`: it fails closed.

### `Permission denied` on everything below a path

A `pre` hook or `param()` callback denied the call (`auth()` with a `verify` that returned False, or a `Deny`). The access log shows the op and path that got `EACCES`. `sluice resolve <path> --op <op>` lists the middleware that runs there.

### A file shows up in `ls` but `cat` says `No such file or directory`

Below a route-made directory (one that exists only because a route passes through it), `ls` shows the literal names of child routes. If the backend directory a route inherits doesn't exist (`/ext4/alice` for user `alice`), nothing beneath it can be reached. Create the backing directory, or check `real path` in `sluice resolve`.

### The mount is slow

Every call runs routing and the whole middleware chain, and with `mount.cache_ms=0` the kernel asks about every lookup. Check for a blocking middleware with `pipeline.slow_ms`, raise `io.threads` for slow backends, or set `mount.cache_ms` if you can accept cached calls skipping middleware. See [In-Depth Configuration](In-Depth-Configuration#settings).

### `error: cannot read SLUICE_..._FILE`

The file named by a `_FILE` variable doesn't exist, or the process can't read it. Check the path inside the container or unit, and the file's owner and mode: the process runs as uid 1000 / the `sluice` user.
