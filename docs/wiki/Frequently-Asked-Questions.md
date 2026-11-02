# Frequently Asked Questions

## Why didn't my middleware run for a call?

Four possible reasons, most likely first:

- **Its `ops` filter** doesn't include the operation. `ops = frozenset({"read"})` sees reads only: `ls` is `lookup`, `getattr`, `opendir` and `readdir`.
- **The kernel answered from its cache.** With `mount.cache_ms` above 0, repeated lookups and `stat`s within that time never reach sluice. Keep it at 0 when every call must pass middleware. Page-cached file contents can also skip `read`: set `direct_io=True` on routes that rewrite reads. See [Writing an App](Writing-an-App#caching-and-direct_io).
- **An earlier middleware answered** with `ctx.respond()` or denied the call: the chain stops there.
- **It's a cleanup op.** `forget`, `release` and `releasedir` bypass middleware.

`sluice resolve <path> --op <op>` lists exactly which middleware runs for a path and operation.

## Why does `ls /fuse` show nothing, when `/fuse/alice/T0k` works?

`/fuse/{user}/{token}` has params, and params can't be listed: sluice doesn't know which users exist, and a token is never listed. Every name below `/fuse` is a directory you can `cd` into. Only literal route segments appear in listings. See [Writing an App](Writing-an-App#directories-that-routes-create).

## Where did `auth()` and `access_log()` go?

They moved out of the package into the repository's `examples/` directory (design 0002): sluice ships the mechanism, and every policy is code you own. Copy `examples/auth.py` and `examples/access_log.py` beside your app module, and change the import to `from auth import auth` and `from access_log import access_log`. Their behaviour and `repr` are unchanged. See [Writing an App](Writing-an-App#example-middleware).

## Can sluice serve `.strm` files for Kodi or Jellyfin, like fuse-strmfs?

Yes, with `examples/strm.py`. Copy it beside your app, then add `app.route("/media", backend="/srv/media", middlewares=[Strm("http://media.example.org/{rel}")])`. Each video shows up as `<name>.strm`, holding its URL. Unlike strmfs, you can have several sources under one mount, an `{env.*}` backend, and the other middleware on the same route (put `Strm` after any authorisation middleware: it answers `.strm` calls itself). See [Writing an App](Writing-an-App#example-middleware).

## Can sluice show the same files by type, date or tag, like ShadowFS?

Yes, with `examples/layers.py`. Add `app.route("/by-type/{rest*}", virtual=True, handler=Layer("/srv/project", "/src", by_type))` beside the backed route `/src`. Each view entry is a relative symlink back into `/src`, so reading it goes through `/src`'s middleware. For rules that hide files, see `examples/hide.py`. See [Writing an App](Writing-an-App#example-middleware).

## Is the `auth()` middleware secure?

Not by itself. The example `auth()` allows everything until you give it a `verify` function, and sluice reaches the backing store with its own uid for every caller. Write `verify` yourself, keep `mount.allow_other` off unless you need it, and read [Security](Security).

## Can a middleware block, or call a synchronous library?

Not on the event loop: one loop serves the whole mount, so a blocking hook stalls every caller. `await` async libraries, or wrap synchronous calls in `await asyncio.to_thread(...)`. `pipeline.slow_ms` logs any step that holds the loop longer than you allow, and `pipeline.timeout_ms` cancels a hook that runs too long (it must be cancel-safe: see [Writing an App](Writing-an-App#timeouts-and-cancellation)). Synchronous hooks (`def` instead of `async def`) are refused before mounting.

## Why does renaming between two users' directories fail with "Invalid cross-device link"?

A rename between paths with different resolved backends (or into or out of a virtual route) is `EXDEV`. `mv` then copies and deletes instead, and each of those calls goes through the middleware of its own path.

## How do I see what a path resolves to without mounting?

`sluice resolve /fuse/alice/T0k/private/notes.txt --op read` prints the matched route, params (secret ones masked), the real path and the middleware chain. `sluice routes` prints the whole tree. Neither runs any middleware. See [CLI Reference](CLI-Reference#examples).

## Why does a virtual file show size 0?

Its content is rendered when it is opened, not when it is `stat`ed, so its size isn't known yet. Virtual files are opened with `direct_io`, so `cat` and friends read to the end anyway. Override `getattr` in your `VirtualHandler` to report a size.
