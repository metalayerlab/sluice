# Writing an App

A sluice app is a Python module with a `Sluice` object in it. `app.module` (`module:attribute`) names it, and `app.dir` adds its directory to `sys.path`. Everything sluice does is configured through that object: routes, middleware, `param()` callbacks and virtual handlers. You never subclass the filesystem itself.

```python
from sluice import Deny, Middleware, Sluice, VirtualHandler, access_log, auth

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

**Order of one call:** routing → `param()` callbacks → `pre` hooks in chain order (global `use()` first, then each ancestor's, then the route's) → the operation → `post` hooks in reverse order.

- **Deny** by raising `Deny("why")` (`EACCES`) or `FsError(errno.X)` in `pre`.
- **Answer directly** with `ctx.respond(result)` in `pre`: the remaining `pre` hooks and the operation are skipped. The `post` hooks of the middleware whose `pre` already ran still run.
- **`failed`** runs, in reverse, on every middleware whose `pre` ran (including the one that raised) when the call fails: a denial, the operation's error, or a `post` hook that raised. When a `param()` callback fails, no `pre` has run yet, so `failed` runs on the whole chain. It can't change the outcome, and an exception in it is logged and ignored.
- **`post` fails closed.** An exception in a `post` hook returns `EIO`, never the unmodified result, so a failed redaction can't leak the original bytes. The operation's side effects stand.
- `forget`, `release` and `releasedir` bypass middleware: they are cleanup the kernel can't act on the failure of.
- **`rename`** is authorised against both paths: the source route's chain runs, and before the operation the target route's whole chain runs its `pre` hooks too, with a context for the target path. A middleware on both chains (any `app.use()` one, or every one when both paths are on the same route) runs once per path, and each hook that ran `pre` gets `post` or `failed` with the same context, so `access_log()` logs a rename once for each path. A rename between different backends, or into or out of a virtual route, is `EXDEV`, and tools fall back to copy and delete.

Middleware runs on the event loop that serves the whole mount. **Never block in a hook:** no `time.sleep`, no synchronous network or disk I/O. Use `await` and `asyncio.to_thread` instead. `pipeline.slow_ms` logs any step that holds the loop too long.

### Built-in middleware

| | |
|---|---|
| `auth(verify, user_param="user", token_param="token")` | Denies the call unless `await verify(user, token, real_path, op)` returns True. The default `verify` allows everything: phase 1 provides the hook, not the policy. |
| `access_log(logger=None, level=logging.INFO)` | Logs one line per call to `sluice.access`: op, `display_path` (secret segments masked), caller uid and pid, outcome (`ok` or the errno name), time, correlation id. |

### The context

| Field | Type | Notes |
|---|---|---|
| `op` | `str` | `"getattr"`, `"read"`, ... |
| `args` | per-op dataclass | `ReadArgs(offset, size)`, `WriteArgs(offset, data)`, `OpenArgs(flags)`, ...; may be replaced in `pre` |
| `params` | `dict` | Merged params of the whole chain, after `param()` callbacks |
| `real_path` | `Path \| None` | `None` for virtual routes and route-made directories |
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
