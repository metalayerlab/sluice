# Writing an App

A sluice app is a Python module with a `Sluice` object in it. `app.module` (`module:attribute`) names it, and `app.dir` adds its directory to `sys.path`. Everything sluice does is configured through that object: routes, middleware, `param()` callbacks and virtual handlers. You never subclass the filesystem itself.

```python
from access_log import access_log  # examples/access_log.py, copied beside this module
from auth import auth  # examples/auth.py
from sluice import Deny, Middleware, Sluice, VirtualHandler

app = Sluice()
app.use(access_log())  # global: every call
app.param("token", decode_token, secret=True)  # once per call; masked in logs

user = app.route("/fuse/{user}/{token}", backend="/ext4/{user}", middlewares=[auth(verify)])
user.route("private", middlewares=[AdminOnly()])  # inherits /ext4/{user}
user.route("status", virtual=True, handler=Status())  # no backing file
app.route("/fuse/{project}/config.yaml", virtual=True, handler=ConfigTemplate())
```

Check an app without mounting it: `sluice routes` prints the tree, and `sluice resolve <path>` shows where one path goes. See [CLI Reference](CLI-Reference).

## Routes

`app.route(pattern, backend=None, virtual=False, handler=None, middlewares=(), direct_io=False)` returns a `Route`. Its own `.route(...)` adds a child, with a pattern relative to the parent's.

**Patterns.** Segments separated by `/`, each one of:

| Segment | Matches | Captures |
|---|---|---|
| `name` | exactly that name | nothing |
| `{name}` | any one segment | the segment, as a string |
| `{name*}` | zero or more segments; last segment only | the segments joined with `/` |

A param must be a whole segment (`file-{id}` is an error).

**Prefix matching.** A route matches its pattern as a *prefix*. What is left of the path (the tail) is appended to the backend: with `backend="/data/{project}"`, the route `/root/{project}` serves `/root/p/sub/file.txt` from `/data/p/sub/file.txt`.

**Most specific wins.** Among sibling routes, sluice compares patterns segment by segment: a literal beats `{param}`, which beats `{param*}`, and a longer pattern beats its own prefix. So `/fuse/{project}/config.yaml` takes `/fuse/p/config.yaml` from `/fuse/{user}/{token}`. Two siblings of the same shape (`/a/{x}` and `/a/{y}`) are an error, so the outcome never depends on registration order.

### Three kinds of backend

| Route | Backend | Real path |
|---|---|---|
| `backend="/ext4/{user}"` | its own | the rendered template plus the tail |
| no `backend`, not virtual, under a backed route | inherited from the nearest ancestor | the ancestor's template plus the tail, measured from that ancestor: `.../private/notes.txt` is `/ext4/{user}/private/notes.txt` |
| `virtual=True, handler=...` | none: inheritance stops here | `None`; the handler implements every operation |

A route with no backend of its own, none to inherit and not virtual is a **grouping** node. Its path is a read-only directory, and its middleware applies to everything beneath it. That is how you attach middleware to a prefix.

**Backend templates** can use the route chain's params (`{user}`) and allowlisted environment variables (`{env.REGION}`, see `env.expose` in [In-Depth Configuration](In-Depth-Configuration)). They can't use `{secret.*}` or a param registered with `secret=True`, because the rendered real path is logged and shown by `sluice resolve`. For the same reason, a route that inherits a backend can't capture a secret param, since its segments become part of the real path; capture it on the backed route itself (`/fuse/{user}/{token}` with `backend="/ext4/{user}"`) or above it. A rendered path that would leave the template's literal prefix (through `..` in a captured value) is refused with `EACCES`.

### Directories that routes create

The mount root, and each literal segment on the way to a route (`/fuse`), are read-only directories (mode `0555`). Params can't be listed, so `ls /fuse` shows nothing, and a token in a path is never listed. Any name below `/fuse` exists as a directory, though, because it could be a `{user}`. Listing a backed directory shows the real entries plus the literal names of child routes: `ls /fuse/alice/T0k` shows `private` and `status`. A child route's name hides a real entry with the same name. A path no route covers does not exist (`ENOENT`).

### What freeze() checks

The routes are validated before mounting (and by `sluice routes`). Each problem is a `RouteError` naming the route:

- every `{param}` in a backend template is captured by the route or an ancestor;
- every `{env.NAME}` is in `env.expose`;
- no backend template uses a secret param, and no route that inherits a backend captures one;
- a param name is captured at most once along a chain;
- `virtual=True` has a `handler` (a `VirtualHandler`) and no `backend`, and a handler needs `virtual=True`;
- every middleware hook and `param()` callback is `async def`, and every name in a middleware's `ops` is a real operation;
- `{name*}` is last, and siblings don't have the same shape.

## param() callbacks

`app.param(name, func=None, secret=False)`. `async func(ctx, raw)` runs once per call, for every call whose route chain captures `name`, after routing and before any middleware. Its return value replaces `ctx.params[name]` (for example a decoded token instead of the raw string). Raising `Deny` or `FsError` fails the call. Callbacks run in registration order.

`secret=True` masks the param: `ctx.display_path`, the access log and `sluice resolve` show `********` for its segment, and backend templates (and routes that inherit a backend) can't use it. `func` can be left out to mark a param secret without transforming it.

## Middleware

A middleware is a `Middleware` subclass that overrides any of three async hooks:

```python
class Redact(Middleware):
    ops = frozenset({"read"})  # None (the default): every operation

    async def pre(self, ctx):  # before: inspect, replace ctx.args, raise to deny
        ...

    async def post(self, ctx, result):  # after: return result, or a replacement
        return result.replace(b"SECRET", b"******")

    async def failed(self, ctx, error):  # the call failed: observe only
        ...
```

A plain `async def fn(ctx)` passed as middleware is a `pre` hook.

A hook you don't override is never called (design 0010). Which hooks a middleware overrides is settled when the route tree is frozen: a hook assigned on an instance or class later (`mw.pre = gate` after `freeze()` or the mount) is never called, so assign it before.

**Order of one call:** routing → `param()` callbacks → `pre` hooks in chain order (global `use()` first, then each ancestor's, then the route's) → the operation → `post` hooks in reverse order.

- **Deny** by raising `Deny("why")` (`EACCES`) or `FsError(errno.X)` in `pre`.
- **Answer directly** with `ctx.respond(result)` in `pre`: the remaining `pre` hooks and the operation are skipped. The `post` hooks of the middleware whose `pre` already ran still run.
- **`failed`** runs, in reverse, on every middleware whose `pre` ran (including the one that raised) when the call fails: a denial, the operation's error, or a `post` hook that raised. When a `param()` callback fails, no `pre` has run yet, so `failed` runs on the whole chain. It can't change the outcome, and an exception in it is logged and ignored.
- **`post` fails closed.** An exception in a `post` hook returns `EIO`, never the unmodified result, so a failed redaction can't leak the original bytes. The operation's side effects stand.
- `forget`, `release` and `releasedir` bypass middleware: they are cleanup the kernel can't act on the failure of.
- **`rename`** is authorised against both paths: the source route's chain runs, and before the operation the target route's whole chain runs its `pre` hooks too, with a context for the target path. A middleware on both chains (any `app.use()` one, or every one when both paths are on the same route) runs once per path, and each hook that ran `pre` gets `post` or `failed` with the same context, so `access_log()` logs a rename once for each path. If the target's `param()` callbacks fail, the target's whole chain gets `failed` too, as the source's does for its own. A `ctx.respond()` in a target-side `pre` hook skips the rename, as on the source side. A rename between different backends, or into or out of a virtual route, is `EXDEV`, and tools fall back to copy and delete.

Middleware runs on the event loop that serves the whole mount. **Never block in a hook:** no `time.sleep`, no synchronous network or disk I/O. Read backend files through `ctx.backend` (below), and `await` async libraries or `asyncio.to_thread` for anything else. `pipeline.slow_ms` logs any step that holds the loop too long.

### Timeouts and cancellation

With `pipeline.timeout_ms` set, a hook that runs longer is **cancelled at the `await` it is suspended in**, and the call fails with `EIO` (design 0006). Write every hook so that can happen:

- **Be cancel-safe.** Leave shared state consistent at every `await`: take and release locks with `async with`, undo partial work in `finally`.
- **Never swallow `CancelledError`.** Don't catch `BaseException` or `asyncio.CancelledError` without re-raising it. A hook that swallows it runs on, holding the call, until it returns; its result is then discarded and the call fails with `EIO` anyway (the log says `(it swallowed the cancellation)`). A hook that blocks the loop past its limit is treated the same way (`(it blocked the event loop past the limit)`).
- **Blocking work goes through `ctx.backend`**, which runs it in the I/O pool. A hook that blocks the loop can't be cancelled at all, and stalls every call.
- **A timed-out backend call keeps running** in its thread: Python can't stop a syscall. Its result is dropped, and a write may still happen. `backend.max_abandoned` bounds how many such threads a hung backend can hold: past it, backend calls fail fast with `EIO` until they finish (see [In-Depth Configuration](In-Depth-Configuration#settings)). Work you start with `asyncio.to_thread` isn't counted there: prefer `ctx.backend` for file I/O.

An `OSError` raised in a hook fails the call with its own errno (a hook's `FileNotFoundError` is `ENOENT`). Any other unexpected exception is `EIO`, with its traceback logged, and so are OSErrors whose number isn't a kernel errno (`socket.gaierror`, `ssl.SSLError`).

### Reading the backend from a hook

On a backed route, `ctx.backend` is a read-only view of the route's backend; it is `None` on virtual routes and route-made directories. Use it instead of `os.stat` and `open`: its calls run in the I/O thread pool (no blocking the loop), and they reach files the way the passthrough does, never following a symlink below the template's literal prefix (see [Security](Security#hardening)).

| | |
|---|---|
| `root` | The rendered backend (`/srv/media` for `backend="/srv/media"`) |
| `await lstat(path)` | `Attr` of `path` itself, a symlink included |
| `await stat(path)` | `Attr` of what `path` names: a final symlink is followed while its target stays inside `root` (`EACCES` if it leaves, `ELOOP` after 40 hops) |
| `await read_all(path, limit)` | A regular file's content; `EFBIG` above `limit` bytes, `EISDIR` or `EINVAL` for anything but a regular file, `ELOOP` for a symlink |
| `await scandir(path)` | A directory's `DirEntry` list, with `lstat` attributes |

`path` is a real path inside `root`, absolute (`ctx.real_path.with_suffix("")`) or relative to `root`. A path outside `root`, or with a `..` component, is `EACCES`. Failures raise `FsError` with the real errno, so a hook can let them propagate.

### Example middleware

sluice ships no concrete middleware: the package is the mechanism, and every policy is yours. The repository's [`examples/`](https://github.com/metalayerlab/sluice/tree/main/examples) directory holds middleware to copy and adapt. Copy a file beside your app module (`app.dir` is on `sys.path`) and import it by name, as in `from auth import auth`. The examples aren't installed with the package. Each one is tested in `tests/examples/`.

| `examples/` | |
|---|---|
| `auth.py`: `auth(verify, user_param="user", token_param="token")` | Denies the call unless `await verify(user, token, real_path, op)` returns True. The default `verify` allows everything: phase 1 provides the hook, not the policy. |
| `access_log.py`: `access_log(logger=None, level=logging.INFO)` | Logs one line per call to `sluice.access`: op, `display_path` (secret segments masked), caller uid and pid, outcome (`ok` or the errno name), time, correlation id. |
| `strm.py`: `Strm(template, video=VIDEO)` | strmfs as middleware: shows each video `X.mkv` as `X.mkv.strm`, a read-only file whose content is `template` filled in (`{rel}`, the URL-quoted path below the backend; `{name}`, the file name) plus a newline. Kodi and Jellyfin play a `.strm` as "play this location". Directories pass through, other files are hidden from listings and lookups, and the mount is read-only (`EROFS`). A symlinked video counts while its target stays in the backend. All its I/O is `ctx.backend`. Put it after any authorisation middleware: it answers `.strm` lookups and opens itself, which skips the `pre` hooks after it |
| `hide.py`: `Hide(*rules)`, with `glob(pattern)`, `larger_than(size)`, `older_than(seconds)` | ShadowFS rules as middleware: an entry any rule matches is dropped from listings and is `ENOENT` to every path op, and creating or renaming onto a hidden name is `EACCES`. A rule is any `(rel, attr) -> bool` over the path below the backend and its `lstat` (`None` for a new name). `glob` matches the name, or the path when the pattern has a `/` (`build/**`, `src/**/*.c`). A hidden directory hides its subtree. Child routes are never hidden. Hidden entries still exist, so `rmdir` of a directory that lists as empty is `ENOTEMPTY` |
| `layers.py`: `Layer(source, target, classify, refresh=300)`, with `by_type`, `by_date`, `by_tag` | ShadowFS virtual layers: a `VirtualHandler` for a `{rest*}` route (`/by-type/{rest*}`) that lists views of `source` (`py/`, `2026/10/31/`, one directory per tag in the `user.tags` xattr). Each file in a view is a **relative** symlink into the backed route at `target` (`../../src/project.py`), so reads go through that route and its middleware, and the app needn't know the mountpoint. The index is scanned off the event loop on first use and rebuilt in the background after `refresh` seconds. The scan never follows a symlink. Names two files share in one view are marked with their source directory (`project@docs.py`), so a name never comes to mean a different file. A view lists names that the source route's middleware might hide, so give the view route the same middleware if names are sensitive |
| `transform.py`: `Transform(fn, match="*", max_bytes=64 MiB, cache_bytes=64 MiB)` | ShadowFS transforms: a file whose name matches is read whole through `ctx.backend`, passed through `fn` (in a worker thread), and served as a read-only snapshot. `getattr` and listings report the transformed size, so a cold `ls -l` runs `fn` on every matching file. Outputs are cached by path, mtime and size, bounded by `cache_bytes`. A file above `max_bytes` keeps its raw size and fails to open with `EFBIG`, so nobody reads raw bytes as transformed ones. Opening a matching file for writing, truncating it or changing its attributes is `EROFS`. Several on one route form a pipeline: the last in the chain runs first |
| `yaml_projection.py`: `Projection(DocumentStore(path, model))`, with the `Model` protocol | One YAML document as a tree ([design 0011](https://github.com/metalayerlab/sluice/blob/main/docs/design/0011-yaml-projection-pilot.md)), a `VirtualHandler` for a `{rest*}` route: mappings are directories, scalars are files, a list of scalars is one file with one item per line. A leaf's content is its value and one newline: null is empty, a float is its `repr`, dates and datetimes are ISO 8601, and the type the model declares decides, else the value's own. `getattr` reports the real size. The `Model` loads, validates and types the document; the projector never parses YAML itself. A document with findings, or with a list of mappings or of lists, is refused at mount (`DocumentError`), each finding logged at warning with its code and JSON Pointer. At each `opendir` the store reloads the file if its inode, size or mtime changed (a rename-replace counts); a replacement that doesn't validate is logged and the last good document stays. **Writable where the model says so** (step 4): a leaf the model marks `writable` is mode 0644 and takes `echo`, `printf` or an editor's write, cast, validated and written back atomically (below); every other leaf is 0444. Creating, removing or renaming anything is refused: the projection changes values, not the document's shape. A leaf the model marks `is_reference` (a `pass:` path) reads as its reference text, is never resolved (no `pass`, no subprocess) and fails every write with `EACCES` |
| `vo_projection.py`: `org_projection(path)` | The Virtual Organisations adapter for `yaml_projection.py` ([design 0011](https://github.com/metalayerlab/sluice/blob/main/docs/design/0011-yaml-projection-pilot.md), step 3): a `Projection` of one org record, its `Model` filled from the `virtual_organisations.model` package (`load_org`, `dump`, `validate_org`, each finding as its code, JSON Pointer and message). A seed value is typed by the template copy's `seed` declaration for its key; everything else by its current value. Only a version 1 record mounts: another version is refused (`DocumentError`), as is a record with findings. **Needs the private `virtual-organisations` package** installed beside the app: without it the module imports, and `org_projection` raises `ModelNotInstalled` (an `ImportError`) saying so. **Read-only for now:** nothing is writable, and version 1 has no secret references |

A projection, with a model of your own (`load`, `dump`, `validate`, `type_of`, `writable`, `is_reference`; `tests/examples/test_yaml_projection.py` has a toy pyyaml one):

```python
from yaml_projection import DocumentStore, Projection

store = DocumentStore("/srv/records/widget.yaml", WidgetModel())  # Raises on a bad document
app.route("/records/widget/{rest*}", virtual=True, handler=Projection(store))
```

`ls /mnt/records/widget` lists the top-level keys, and `cat /mnt/records/widget/address/city` prints one value.

`echo Leeds > /mnt/records/widget/address/city` changes one. A write is checked twice: the first `write` (at offset 0, what `echo` and `printf` send) casts and validates the content as it stands, so the shell sees a bad value; `close` (the `flush`) commits it. A commit, under the store's lock: cast the content by the declared type (else the value's own), set it in a copy of the document, run the model's `validate` on the copy, `dump` it to a temporary file beside the document, `load` that back and require it equal, fsync, rename it over the document, and only then show the new value. Any failure leaves the file byte-identical and the temporary file removed. The cast is strict: one trailing newline is stripped, an integer is `-?[0-9]+` (no `+`, `_` or leading zero), a boolean is `true` or `false`, a date `YYYY-MM-DD`; 0 bytes are null, which only a null leaf takes; a list is replaced whole, one string item per line. Comments in the file are lost on the first write (the model's `dump` writes it), and its key order is kept.

What a writer sees:

| errno | Why |
|---|---|
| `EINVAL` (`Invalid argument`) | The content doesn't cast to the leaf's type, isn't UTF-8, or the model's `validate` has a finding for the changed document |
| `EACCES` (`Permission denied`) | The leaf isn't writable, or is a secret reference (refused at `open` and `setattr`, before the model is asked) |
| `EBUSY` (`Device or resource busy`) | The file changed on disk since it was loaded (another writer): it is reloaded and left as that writer left it. Retry |
| `EIO` (`Input/output error`) | The model's `dump` doesn't load back equal, or fails: a bug in the model |
| `EPERM` (`Operation not permitted`) | A create, `mkdir`, `unlink`, `rmdir` or `symlink`; any `setattr` on a directory; `chmod` or `chown` on a leaf. Times (`touch`) are accepted and ignored. A rename is `EXDEV`, as on every virtual route |

An errno carries no message, so each refusal is logged at warning on the `yaml_projection` logger, with its code and JSON Pointer and never the value: `yaml_projection: /srv/records/widget.yaml: write refused: range at /staff: staff must be under 1000`. `cast` is a value that doesn't cast, `changed` an `EBUSY`, `load-back` and `dump` an `EIO`; any other code is the model's own finding. `truncate -s` on a leaf commits the shortened content the same way; while a write handle on that leaf is open, the truncate goes to that handle's buffer and is committed at its `close`.

An org record from Virtual Organisations, behind the access layer (`examples/auth.py`):

```python
from auth import auth
from vo_projection import org_projection

app.route(
    "/orgs/example-trust/{rest*}",
    virtual=True,
    handler=org_projection("/srv/orgs/example-trust.yaml"),
    middlewares=[auth()],
)
```

The record's path is the handler's own configuration: a virtual route has no backend. `ls /mnt/orgs/example-trust` lists `kind`, `version`, `org`, `name`, `status`, `template`, `seed` and `results`; `cat /mnt/orgs/example-trust/seed/<key>` prints one seed value by its declared type.

### The context

| Field | Type | Notes |
|---|---|---|
| `op` | `str` | `"getattr"`, `"read"`, ... |
| `args` | per-op dataclass | `ReadArgs(offset, size)`, `WriteArgs(offset, data)`, `OpenArgs(flags)`, ...; may be replaced in `pre` |
| `params` | `dict` | Merged params of the whole chain, after `param()` callbacks |
| `real_path` | `Path \| None` | `None` for virtual routes and route-made directories |
| `backend` | `BackendView \| None` | Read-only, symlink-safe access to the backend ([above](#reading-the-backend-from-a-hook)); `None` when `real_path` is |
| `virtual` | `bool` | |
| `config` | `MountConfig` | `config.env` (str values) and `config.secret` (`Secret` values); read once at mount time |
| `path`, `display_path`, `inode` | | The virtual path; `display_path` has secret segments masked; use it in logs |
| `caller` | `Caller(uid, gid, pid)` | From the kernel: the only identity it vouches for |
| `timestamp`, `correlation_id` | `float`, `str` | When the call arrived; a mount-unique id that appears in every log line for the call |
| `route`, `resolution` | | The matched route, and everything routing worked out |
| `state` | `dict` | Scratch space shared by the call's middlewares |

`Secret` values print as `********`. Call `.reveal()` to get the value.

## Virtual routes

A `virtual=True` route has a `VirtualHandler` that implements every operation. The default is a read-only regular file whose content is `await render(ctx)`:

```python
class Status(VirtualHandler):
    async def render(self, ctx):
        return f"user={ctx.params['user']}\n".encode()
```

`render` runs once per `open`, and the bytes are kept with the open file, so reads at different offsets see one snapshot. Virtual files are opened with `direct_io`, so the kernel never caches them. `getattr` reports size 0, and readers still read to the end. Write operations fail with `EROFS`. Override any method (`getattr`, `open`, `read`, `readdir`, `write`, ...) for other behaviour. The method set is in `sluice/virtual.py`.

## Caching and direct_io

Kernel caching is off by default (`mount.cache_ms=0`), so every call reaches the pipeline. Turning it on is a trade: calls the kernel answers from its cache skip middleware. Set `direct_io=True` on routes whose `post` hooks rewrite read data. Otherwise the page cache can serve unrewritten bytes, and the kernel trusts the real file's size. A `post` hook sees one `read` call at a time, so a rewrite that spans two reads, or changes the length, is not handled in phase 1.
