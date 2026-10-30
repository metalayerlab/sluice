# 0001: Initial design

Status: Implemented
Date: 2026-10-30
Authors: Stephen Cox; drafted by Claude

## Context

Sluice is a lab project: a FUSE filesystem in Python where an Express-style router maps virtual paths
to real backing paths (or to purely virtual content), and chainable middleware gates, inspects and
rewrites every filesystem call. The phase 1 requirements
([requirements/phase-1.md](requirements/phase-1.md), the decision record this design implements) fix
several choices already: pyfuse3; backend as a route property resolved before
middleware runs; three distinct backend states (own, inherited, virtual); param merge across the
ancestor chain with registration-time validation; Express-style `param()` callbacks; auth as a
default-allow middleware with an injected `verify()`; env vars and secrets frozen at mount time in
`ctx.config`, in a namespace separate from route params; logging that redacts secrets by default.

Phase 1 delivers the structure: a working passthrough, the router, the inode table, the pipeline and
the extension points, with stub policy. Not real auth, not content rewriting, not performance work.

Constraints:

- **Non-blocking.** One event loop serves every FUSE request. Anything blocking in a middleware or a
  backend syscall stalls the whole mount, not just its own call.
- **Per-call cost.** A single userspace `read()` becomes many FUSE calls, each running routing and the
  whole middleware chain. The kernel's attribute, entry and page caches answer calls *without*
  reaching us, so caching and per-call authorisation pull in opposite directions.
- **FUSE is inode-based.** `read`, `write`, `getattr` and friends get an inode or file handle, not a
  path, so path routing needs an inode ↔ path table kept consistent across create, rename and unlink.
- **Facts found while drafting** (they change the requirements' premises; see the open questions):
  - pyfuse3 is **Trio-native**, not asyncio-native. asyncio is supported through `pyfuse3.asyncio`
    (an adapter shipped in the package), and `trio` is a hard dependency either way.
  - pyfuse3 (3.5.0, June 2026) ships **only an sdist**: installing it compiles a C extension against
    libfuse3, so it needs `libfuse3-dev`, `pkg-config` and a C compiler. The template's CI
    (`uv sync --locked` on a bare `ubuntu-latest`) and Docker builder have none of these, and both
    are template-owned.
  - pyfuse3 is typed (`py.typed`, `__init__.pyi`), so `mypy --strict` needs no local stubs.
  - Mounting needs `/dev/fuse` and, in a container, `CAP_SYS_ADMIN` (or a FUSE device plugin).

## Decision

### Components

All under `src/sluice/`. Everything except `fs.py` and `mount.py` is free of pyfuse3, so the router,
pipeline and inode table are unit-tested in CI without libfuse.

| Module | Responsibility |
|---|---|
| `app.py` | `Sluice`, the public registration API: `route()`, `use()`, `param()`; `freeze(mount_config)` validates the tree and returns an immutable `RouteTable`. |
| `routing.py` | Pattern parsing, the `Route` tree, matching, specificity ordering, backend template rendering, `Resolution` (matched chain, merged params, `real_path`, `virtual`, middleware chain). |
| `context.py` | `Context` (the per-call event), `Caller`, per-op argument dataclasses, `MountConfig` (`env`, `secret`), `Secret` (masked `repr`/`str`). |
| `middleware.py` | The `Middleware` base and the pipeline runner (pre phase → operation → post phase). Built-ins: `auth(verify)` (default-allow stub) and `access_log()` (redacts by default). |
| `virtual.py` | `VirtualHandler`, the base class that implements operations for `virtual=True` routes. |
| `inodes.py` | `InodeTable`: inode ↔ virtual path, lookup counts (`forget`), rename/unlink bookkeeping, file handle table. |
| `backend.py` | Passthrough syscalls against `real_path`, run in a bounded worker thread pool so they never block the loop. |
| `dispatch.py` | `Dispatcher`: one async method per FUSE op. Turns inode/handle into a path, routes, builds the `Context`, runs the pipeline, maps errors to errno. Returns sluice's own result types. |
| `fs.py` | `SluiceOperations(pyfuse3.Operations)`: thin wrappers only, converting pyfuse3 arguments and results to and from the dispatcher's. No logic. |
| `mount.py` | Mount lifecycle: import the app, load `MountConfig`, freeze routes, `pyfuse3.init`, run the loop, unmount cleanly on SIGINT/SIGTERM. |
| `errors.py` | Exception types (see [Error handling](#error-handling)). |
| `config.py`, `cli.py` | As in the template; new settings and commands below. |

Subclassing contract (§3 of the requirements): users never subclass `SluiceOperations`. They build a
`Sluice` app; all specialisation goes through routes, middleware and virtual handlers.
`SluiceOperations` is internal and may change.

### Registration API

Routes are defined in Python, in a module the user names with `app.module` (like `uvicorn module:app`):

```python
from sluice import Sluice, access_log, auth

app = Sluice()
app.use(access_log())  # global, every call
app.param("token", decode_token, secret=True)  # once per request; value redacted in logs

user = app.route("/fuse/{user}/{token}", backend="/ext4/{user}", middlewares=[auth(verify)])
user.route("private", middlewares=[admin_only])  # case 2: inherits /ext4/{user}
user.route("status", virtual=True, handler=StatusHandler())  # case 3: no backing
app.route("/fuse/{project}/config.yaml", virtual=True, handler=ConfigTemplate())
```

- `Sluice.route(pattern, backend=None, virtual=False, handler=None, middlewares=(), direct_io=False)`
  returns the `Route`, whose own `.route(...)` adds a child with a pattern relative to it.
- `Sluice.use(*middlewares)`: global middleware, run before route middleware on every call.
- `Sluice.param(name, func=None, secret=False)`: `async func(ctx, raw) -> value`; may raise to
  short-circuit. `secret=True` makes `access_log()` and error messages mask the value.

**Patterns.** Segments are literals, `{name}` (exactly one segment, naive string capture) or a final
`{name*}` (zero or more segments, joined with `/`). A route matches its pattern **as a prefix**: the
unconsumed tail of the virtual path is appended to the rendered backend. So `/root/{project}` with
`backend="/data/{project}"` serves `/root/p/sub/file.txt` from `/data/p/sub/file.txt`. The tail is
measured from the route that **owns** the backend: for an inherited backend, the child's own segments
are part of it, so `private` under `/fuse/{user}/{token}` (backend `/ext4/{user}`) maps
`.../private/notes.txt` to `/ext4/{user}/private/notes.txt`.

**Backend templates** may reference `{param}` (from the route's chain) and `{env.NAME}`. `{secret.*}`
and any param registered with `secret=True` are rejected, because a rendered `real_path` is logged and
shown by `sluice resolve`.

**Matching.** A tree walk from the root. Among siblings, the most specific match wins: a literal
segment beats `{param}`, which beats `{param*}`, compared segment by segment. Two siblings with the
same shape (`/a/{x}` and `/a/{y}`) are a registration error.

**Resolution walk** (root → matched node): accumulate middleware (global `use()` first, then each
ancestor's, then the matched route's); merge params; carry the nearest ancestor's backend down to
`backend=None, virtual=False` children; stop at `virtual=True` (no inheritance, `real_path=None`). A
route with neither a backend of its own nor an ancestor's, and not virtual, is a **grouping node**:
its own path is a synthetic directory and its middleware applies to everything beneath it (this is
prefix-mounted middleware, so no `use(prefix, ...)` form is needed).

**Synthetic directories.** The mount root and every literal prefix of a route (`/`, `/fuse`, and
`/fuse/alice` once `alice` has been looked up) are read-only directories (`0555`). `readdir` lists
literal children only: param segments can't be enumerated, so `ls /fuse` shows nothing beneath it, and
a token in a path is never listed. A path no route covers is `ENOENT`. `readdir` of a **backed**
directory merges the real entries with the literal names of child routes (virtual or grouping), so
`ls /fuse/alice/T0k` shows `status`; a child route's name hides a real entry of the same name.

**Validation in `freeze()`**, before mounting (and by `sluice routes`), as `RouteError`:

- every `{param}` in a backend template is captured by the route's chain;
- every `{env.NAME}` is in `env.expose` (env names aren't known until mount-time config is loaded,
  which is why validation is in `freeze()` rather than in `route()`);
- a param name is captured at most once per chain (open question 8);
- `virtual=True` has a `handler`, and no `backend`; a `handler` without `virtual=True` is an error;
- every middleware hook and `param()` callback is a coroutine function (`async def`);
- `{name*}` appears only as the last segment, and sibling shapes don't collide.

### Middleware contract

Recommended answer to requirements open question 2 (see [Open questions](#open-questions)):
**phase-structured objects**, not `next()` wrapping and not per-op replacement.

```python
class Middleware:
    ops: frozenset[str] | None = None  # None = every op; else only these

    async def pre(self, ctx: Context) -> None: ...  # inspect, mutate ctx.args, raise to deny
    async def post(self, ctx: Context, result: R) -> R: ...  # inspect or replace the result
    async def failed(self, ctx: Context, error: FsError) -> None: ...  # observe a failure
```

- `pre` hooks run in chain order; any may raise `Deny` / `FsError`, or call `ctx.respond(result)` to
  short-circuit with a result (the operation is skipped). Short-circuiting skips the remaining `pre`
  hooks and the operation; the `post` hooks of the middlewares whose `pre` already ran still run, in
  reverse.
- When a call fails (a `pre` or `param()` denial, the operation's error, or a `post` that raised),
  `failed` runs, in reverse, on every middleware whose `pre` ran, including the one that raised. It
  can't change the outcome, and an exception in it is logged and ignored. So `access_log()` records
  successes in `post` and denials and errors in `failed`: every outcome.
- The operation runs once: the backend passthrough, or the route's `VirtualHandler`.
- `post` hooks run in reverse chain order (onion order) and may return a replacement result. They
  can't prevent the operation: it has already happened.
- A plain `async def fn(ctx)` passed where middleware is expected is a `pre`-only middleware.
- The pipeline is the same for every op; per-op behaviour comes from `ops` and `ctx.op`. The
  exceptions are the cleanup ops `forget`, `release` and `releasedir`: they bypass middleware and
  always do their bookkeeping (close the backend fd, drop the handle, decrement lookup counts),
  because the kernel can't act on their failure and gating them only leaks descriptors and inodes.
- **Two-path ops** are authorised against both paths. `rename` runs the source route's chain; before
  the operation, the `pre` hooks of the target route's chain that aren't already in it also run, with
  a `Context` for the target path and the same `op`; either may deny. So renaming a file into or out
  of `private` passes `admin_only`. `symlink` has one virtual path (the link); its target is an
  uninterpreted string, and following it goes through `lookup`, which is gated.

**Order of one call:** route resolution → `param()` callbacks (registration order, once per distinct
param per call) → `pre` hooks → operation → `post` hooks.

**Non-blocking is enforced** at two levels: `freeze()` rejects synchronous hooks and callbacks; and
`pipeline.slow_ms` (off by default, as asyncio debug mode has overhead) turns on asyncio's
slow-callback detection, which logs any step that holds the loop longer than that without yielding.

### Virtual routes

Recommended answer to requirements open question 5: a **dedicated handler** on the route.
`VirtualHandler` has one async method per op (`getattr`, `open`, `read`, `readdir`, `write`, ...).
The defaults return a read-only regular file of the content from `async render(ctx) -> bytes` (empty
unless overridden), `EROFS` for write ops, and `ENOSYS` for anything else. `render` runs once at
`open` and the bytes are kept in the file handle, so reads at different offsets see one consistent
snapshot. Virtual files are always opened with `direct_io`, so the kernel never caches them and
`st_size` need not match.

### Context

`Context` is a plain (mutable) dataclass. The routing fields (`op`, `path`, `inode`, `real_path`,
`virtual`, `caller`, `config`, `route`) are read-only properties over a frozen `Resolution`; `args`
may be replaced in `pre`; `params` starts as the raw merged captures, and each `param()` callback
replaces its own key in turn; `state` is free for middleware.

| Field | Type | Notes |
|---|---|---|
| `op` | `str` | `"getattr"`, `"read"`, ... |
| `args` | per-op dataclass | `ReadArgs(offset, size)`, `WriteArgs(offset, data)`, ... |
| `params` | `Mapping[str, object]` | Merged chain params, after `param()` callbacks |
| `real_path` | `Path \| None` | `None` for virtual routes and synthetic directories |
| `virtual` | `bool` | |
| `config` | `MountConfig` | `config.env: Mapping[str, str]`, `config.secret: Mapping[str, Secret]`; one shared instance |
| `path`, `inode` | `PurePosixPath`, `int` | Virtual-side identity; `display_path` masks secret segments |
| `caller` | `Caller(uid, gid, pid)` | From the FUSE request; the only identity the kernel vouches for |
| `timestamp` | `float` | `time.time()` at dispatch |
| `correlation_id` | `str` | Mount-unique counter, hex; in every log line for the call |
| `route` | `Route` | The matched route |
| `state` | `dict[str, object]` | Scratch space between middlewares (Express's `res.locals`) |

### Data flow

`open` + `read` on `/fuse/alice/T0k/private/notes.txt`:

1. `lookup` calls walk the path one component at a time; each resolves the child path through the
   router (so a denial in `pre` stops the walk), and `InodeTable` assigns inodes per virtual path.
2. `open(inode)`: `InodeTable` gives the path; the router resolves it (`user=alice`, `token=T0k`,
   backend `/ext4/alice` inherited, tail `private/notes.txt` → `real_path=/ext4/alice/private/notes.txt`;
   chain `access_log, auth, admin_only`); `param("token")` runs; `pre` hooks run; `backend.open` opens
   the real file in a worker thread; `post` hooks run. The handle stores the resolution.
3. `read(fh, offset, size)`: reuses the handle's resolution (no re-routing, matching POSIX: an open
   file survives a rename), builds a fresh `Context` and runs `param()` callbacks and the whole chain
   again; `backend.read` is `os.pread` in a worker thread; `post` may replace the bytes.
4. Any `FsError` / `OSError` becomes `pyfuse3.FUSEError(errno)` in `fs.py`.

Path ops (`getattr`, `setattr`, `unlink`, `rename`, `readdir`, xattrs, ...) route on every call.
`rename` between paths with different resolved backends (or into or out of a virtual route) is
`EXDEV`, so tools fall back to copy and delete through the pipeline.

**Kernel caches.** `entry_timeout` and `attr_timeout` default to 0 (`mount.cache_ms`) and backed
files open without `keep_cache`, so every call reaches the pipeline. Turning caching on is a stated
trade: cached calls skip middleware. Routes whose `post` hooks rewrite read data should set
`direct_io=True`, since the page cache would otherwise serve unrewritten or stale bytes and the kernel
would trust `st_size`.

**Operations in phase 1:** `lookup`, `forget`, `getattr`, `setattr`, `readlink`, `mkdir`, `rmdir`,
`unlink`, `symlink`, `rename`, `open`, `create`, `read`, `write`, `flush`, `release`, `fsync`,
`opendir`, `readdir`, `releasedir`, `statfs`, `access`, `getxattr`, `setxattr`, `listxattr`,
`removexattr`. `link` (hard links) and `mknod` return `ENOTSUP`. Each virtual path gets its own inode,
so two virtual paths to one real file are two inodes.

### Configuration settings

New entries in `config.SETTINGS` (so `SLUICE_<KEY>_FILE` > `SLUICE_<KEY>` > file > default).

| Key (env var) | Type | Default | Secret | Meaning |
|---|---|---|---|---|
| `log_level` (`SLUICE_LOG_LEVEL`) | str | `INFO` | no | Existing. |
| `app.module` (`SLUICE_APP_MODULE`) | str | `""` | no | The app to serve, `module:attribute` (e.g. `myroutes:app`). Required by `mount`, `routes`, `resolve`. |
| `app.dir` (`SLUICE_APP_DIR`) | str | `""` | no | Directory put first on `sys.path` before importing `app.module` (e.g. a mounted ConfigMap). Empty = unchanged. |
| `mount.point` (`SLUICE_MOUNT_POINT`) | str | `""` | no | Where to mount. Required by `mount`. Must be an existing empty directory. |
| `mount.fsname` (`SLUICE_MOUNT_FSNAME`) | str | `sluice` | no | Filesystem name shown by `mount` and `df`. |
| `mount.allow_other` (`SLUICE_MOUNT_ALLOW_OTHER`) | bool | `false` | no | Let users other than the mounting one in. Needs `user_allow_other` in `/etc/fuse.conf`. With it on, middleware is the only gate. |
| `mount.cache_ms` (`SLUICE_MOUNT_CACHE_MS`) | int | `0` | no | Kernel entry and attribute cache lifetime in milliseconds. Above 0, cached calls skip middleware. |
| `mount.debug` (`SLUICE_MOUNT_DEBUG`) | bool | `false` | no | libfuse request tracing. Logs raw paths, including any token in them: lab use only. |
| `io.threads` (`SLUICE_IO_THREADS`) | int | `8` | no | Worker threads for backend syscalls. |
| `env.expose` (`SLUICE_ENV_EXPOSE`) | str | `""` | no | Comma-separated environment variable names copied into `ctx.config.env`. Empty = none. `SLUICE_*` names are refused. |
| `secrets.dir` (`SLUICE_SECRETS_DIR`) | str | `/run/secrets` | no | Each regular file in it becomes `ctx.config.secret.<filename>` (trailing newline stripped). A missing directory means no secrets. |
| `pipeline.slow_ms` (`SLUICE_PIPELINE_SLOW_MS`) | int | `0` | no | Log a warning when a middleware or handler holds the event loop longer than this. 0 = off. |

None of the settings is itself a secret: mount-time secrets arrive as files in `secrets.dir`, are
wrapped in `Secret` (masked `repr`/`str`; `.reveal()` gets the value), are never `Setting`s, and are
never printed by `sluice config`. Only their names are logged, at startup. The environment is an
allowlist (`env.expose`), so templates and middleware can't read arbitrary process environment.

`contrib/sluice.example.yaml` gains every key with its default.

### CLI commands

| Command | What it does |
|---|---|
| `sluice version` | Existing. |
| `sluice config [-c FILE]` | Existing; shows the new settings. |
| `sluice mount [-c FILE]` | Import the app, load `MountConfig`, freeze and validate the routes, mount at `mount.point`, serve in the foreground until SIGINT/SIGTERM, then unmount cleanly (exit 0). |
| `sluice routes [-c FILE]` | Import, freeze and validate without mounting; print the route tree with each node's backend state (own / inherited from / virtual / grouping), rendered template and full middleware chain. Exit 2 on a `RouteError`. |
| `sluice resolve [-c FILE] PATH [--op OP]` | Dry run of the router for one virtual path: matched chain, params (secret ones masked), `real_path`, middleware that would run for `OP` (default `getattr`). Runs no middleware or `param()` callbacks. |

Unmounting a crashed mount is `fusermount3 -u <mountpoint>` (documented, not wrapped).

**Exit codes:** 0 success; 1 an unexpected error (a bug: traceback logged); 2 an expected error
(configuration, app import, route validation, mount failure), printed as one `error: ...` line.

### HTTP endpoints

None in phase 1. Status and introspection are what virtual routes are for (a `status` node is a file
under the mount), and `sluice routes` / `resolve` cover debugging. A listener would add a network
surface and an HTTP dependency for nothing phase 1 needs (open question 11).

### Error handling

| Exception (`errors.py`) | Raised | Result |
|---|---|---|
| `ConfigError` (existing, `config.py`) | Invalid setting, unreadable config or secrets dir | CLI: one line, exit 2 |
| `AppLoadError` | `app.module` empty, malformed, not importable, or not a `Sluice` | CLI: one line, exit 2 |
| `RouteError` | Any `freeze()` validation failure; the message names the route pattern | CLI: one line, exit 2 |
| `MountError` | Mountpoint missing or not empty, no `/dev/fuse`, permission denied, pyfuse3 not installed | CLI: one line, exit 2 |
| `FsError(errno, msg)` | By middleware, `param()` callbacks or handlers to fail a call with an errno | That errno to the caller |
| `Deny(msg)` | `FsError` with `EACCES`; what `auth()` raises | `EACCES` |

At runtime the mount never dies because of a single call:

- `OSError` from a backend syscall → its own errno.
- An unmatched path → `ENOENT`; a write op on a virtual route the handler doesn't implement →
  `EROFS`; any other unimplemented op → `ENOSYS`.
- Any other exception in a `pre` hook, `param()` callback, operation or `post` hook → logged at ERROR
  with the correlation id and traceback, `EIO` to the caller.
- **`post` hooks fail closed:** an exception there returns `EIO`, not the unmodified result. A failed
  redaction must not leak the unredacted bytes. The operation's side effects (a write, a created
  file) stand. If the operation opened something (`open`, `create`, `opendir`), the dispatcher closes
  it and drops the handle before returning `EIO`, since the kernel never got a handle to `release`.
- Error messages and logs show params marked `secret=True` and all `Secret` values as `********`,
  and log virtual paths with those segments masked (`/fuse/alice/********/notes.txt`, via
  `ctx.display_path`). `mount.debug` (libfuse's own tracing) is the one exception.

### Dependencies

- `pyfuse3` (runtime, PyPI name checked: `pyfuse3`, by the libfuse project). Pulls in `trio`.
  Compiles against libfuse3, so the build host needs `libfuse3-dev`, `pkg-config`, `gcc`; the runtime
  needs `libfuse3` and `fuse3` (for `fusermount3`). How CI and the Docker image get these is open
  question 10.
- No other new dependency: matching, templates and the thread pool are stdlib.

### Testing

- Unit (`tests/unit/`): pattern parsing, specificity, the resolution walk (all three backend states,
  grouping nodes), param merge and validation errors, `param()` order and short-circuit, pipeline
  order including short-circuit and fail-closed `post`, inode table across rename and unlink, secret
  masking in logs, settings. All through `Dispatcher` with a fake backend: no FUSE, no pyfuse3.
- Integration (`tests/integration/`, `RUN_INTEGRATION=1`): a real mount in a temp directory with the
  requirements' four example apps (multi-tenant, status node, redaction hook, templated secret);
  skipped when `/dev/fuse` is absent.

## Alternatives considered

- **fusepy / refuse, llfuse**: synchronous or superseded (requirements §1).
- **Rewrite-as-middleware** for `real_path`: makes resolution depend on list order (requirements §5).
- **Per-call env/secret resolution**: I/O on the hot path (requirements §8).
- **`next()`-wrapping middleware** (Express, `async def mw(ctx, next)`): the most flexible, but pre
  and post are then one function and not structurally distinct (requirements §4); a middleware that
  forgets `await next()` silently swallows the operation; and "post can't block" is unenforceable.
  The phase hooks express everything the requirements ask for.
- **Per-op replacement hooks** (`on_read`, `on_getattr`): two middlewares that both implement
  `on_read` have no defined composition. Kept only for virtual handlers, where there is exactly one
  implementation.
- **Routes in YAML**: middleware and handlers are code anyway, so YAML would only name Python objects;
  a second, weaker way of saying the same thing.
- **Inodes from the backing file's `st_ino`**: collides across backends and doesn't exist for virtual
  or synthetic nodes.
- **An HTTP control/metrics API**: see [HTTP endpoints](#http-endpoints).

## Consequences

- Easier: routing and middleware are plain async Python, testable without a kernel. One way to add
  behaviour (routes + middleware + handlers) instead of subclassing.
- Harder: every call pays routing plus the whole chain, and `param()` callbacks run per call (a token
  decode on every `read`); caching their results is phase 2 performance work. Content rewriting that
  changes length needs `direct_io` and has to cope with reads at arbitrary offsets (the hook exists;
  making it correct is out of scope). The daemon accesses backends with its own uid for every caller
  (a confused deputy by design): with `allow_other`, middleware is the only gate.
- Tokens in paths are visible to anyone who can see the caller's command lines, shell history or
  `/proc/<pid>/cwd`; sluice masks them in its own output only. Symlinks inside a backend that point
  outside it are followed (a backend is trusted); the rendered `real_path` is checked lexically to
  stay under the template's literal prefix, so `..` in a captured value can't escape.
- Rollout: new project, nothing to migrate. Rollback is unmounting.
- Wiki pages that change: Introduction, Quick-Start, In-Depth-Configuration (prose; table generated),
  CLI-Reference (generated), Architecture-Internals (this design), Security (threat model above,
  secrets, `allow_other`, token-in-path), Testing-Guide (unit vs mount tests, `/dev/fuse`),
  Troubleshooting (stale mount, `fuse: device not found`, `allow_other` refused), FAQ (why my cached
  call skipped middleware), and a Deployment section for the FUSE device and capabilities.

## Open questions

All resolved: approved on 2026-10-30 with every recommendation below accepted as written. The
requirements' nine questions (§9) first, then ones that came up while drafting.

1. **Layering: inheritance or stacking?** Recommend inheritance only, as drafted: one internal
   `SluiceOperations` class, specialised through the router; stacking filesystems out of scope. A
   backend can already be another mount (sluice on sluice, or on overlayfs) without sluice knowing.
2. **Middleware shape.** Recommend phase-structured objects (`pre` / `post` with an `ops` filter),
   as drafted. They make pre and post structurally distinct, as §4 requires, and give composition a
   defined order, which neither alternative does (see [Alternatives](#alternatives-considered)).
3. **Overlapping routes.** Recommend specificity among siblings (literal > `{p}` > `{p*}`), and an
   error for same-shape siblings. Registration order makes one route silently shadow another;
   specificity is deterministic and doesn't depend on import order.
4. **`use(prefix, ...)`.** Recommend no: a grouping route (no backend, not virtual) carrying
   middleware is exactly that, with no second mechanism.
5. **Virtual content.** Recommend a dedicated `VirtualHandler` on the route, as drafted: middleware
   then never needs to know whether it is last in the chain.
6. **`authorize()` registration.** Recommend plain middleware (`auth(verify)`) for phase 1; nothing
   needs special syntax yet, and it can be added on top later without breaking anything.
7. **`env.*` and `secret.*`.** Recommend separate namespaces, as drafted: secrets get the `Secret`
   type, masking, and are banned from backend templates; env values are plain strings.
8. **Duplicate param names in a chain.** Recommend making it a `RouteError` in phase 1, not "child
   overrides". Relaxing a rule later breaks nobody; tightening it later breaks existing route trees.
9. **`param()` granularity.** With 8 as recommended, each name occurs once per chain, so "once per
   call" is the only meaning and the question goes away. If you choose "child overrides" instead:
   once per call per distinct value (Express).
10. **Building pyfuse3 in CI and Docker.** It's sdist-only and needs libfuse3 headers; CI and the
    Dockerfile are template-owned. Recommend: open an issue on `metalayerlab/project-template` for a
    system-packages hook (an `apt` list read by CI and the Docker builder); until it lands, make
    `pyfuse3` an optional extra (`sluice[fuse]`) so `uv sync --locked` keeps working in CI, with
    `fs.py` and `mount.py` importing it lazily (`MountError` if absent), and a minimal, issue-linked
    builder-stage addition in the Dockerfile. Coverage still holds because only those two thin modules
    touch pyfuse3. The alternative, a hard dependency, breaks CI on the first commit that adds it.
11. **HTTP endpoints.** Recommend none in phase 1 (see [HTTP endpoints](#http-endpoints)). If you
    want health checks for Kubernetes, a probe that `stat`s a virtual `/.sluice/health` file through
    the mount checks more than an HTTP port would: that the FUSE loop is answering.
12. **asyncio or Trio.** The requirements assume asyncio-native; pyfuse3 is Trio-native, with
    asyncio through the shipped `pyfuse3.asyncio` adapter. Recommend asyncio, as drafted: middleware
    authors know it and its ecosystem, and it has the slow-callback detection that `pipeline.slow_ms`
    uses. Cost: the adapter is the less-exercised path, so integration tests matter more. Trio would be
    the native, best-tested path but asks every middleware author to use Trio.
13. **Kernel caching defaults.** Recommend `mount.cache_ms=0` and no `keep_cache`, as drafted:
    correct per-call auth first, speed in phase 2. The cost is many more `getattr`/`lookup` calls.
14. **Where the requirements live.** `requirements.md` at the repo root wasn't in the repository map.
    Done: moved it to `docs/design/requirements/phase-1.md` (outside the `NNNN-*.md` pattern the
    index check reads) and linking it from this record's Context, so the two are reviewed together.
