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

## Is the `auth()` middleware secure?

Not by itself. Phase 1's `auth()` allows everything until you give it a `verify` function, and sluice reaches the backing store with its own uid for every caller. Write `verify` yourself, keep `mount.allow_other` off unless you need it, and read [Security](Security).

## Can a middleware block, or call a synchronous library?

Not on the event loop: one loop serves the whole mount, so a blocking hook stalls every caller. `await` async libraries, or wrap synchronous calls in `await asyncio.to_thread(...)`. `pipeline.slow_ms` logs any step that holds the loop longer than you allow. Synchronous hooks (`def` instead of `async def`) are refused before mounting.

## Why does renaming between two users' directories fail with "Invalid cross-device link"?

A rename between paths with different resolved backends (or into or out of a virtual route) is `EXDEV`. `mv` then copies and deletes instead, and each of those calls goes through the middleware of its own path.

## How do I see what a path resolves to without mounting?

`sluice resolve /fuse/alice/T0k/private/notes.txt --op read` prints the matched route, params (secret ones masked), the real path and the middleware chain. `sluice routes` prints the whole tree. Neither runs any middleware. See [CLI Reference](CLI-Reference#examples).

## Why does a virtual file show size 0?

Its content is rendered when it is opened, not when it is `stat`ed, so its size isn't known yet. Virtual files are opened with `direct_io`, so `cat` and friends read to the end anyway. Override `getattr` in your `VirtualHandler` to report a size.
