# Introduction

## What it is

sluice is a FUSE filesystem whose behaviour is a Python program. You declare **routes** that map virtual paths (`/fuse/{user}/{token}`) to where the data really lives (`/ext4/{user}`), or to content a handler generates (`virtual=True`). You attach **middleware**, async hooks that run before and after every filesystem call on a route, to authorise, log, or rewrite it. The model is Express's: routes with params, `use()` for global middleware, and `param()` callbacks.

It is for experimenting with per-call policy on a filesystem: one mount that shows each user only their own subtree, a `status` file that is computed on every read, a log file whose secrets are redacted on the way out, or a config file assembled from mount-time secrets and never written to disk.

It is deliberately **not** a production access-control system. Phase 1 delivers the structure (router, middleware pipeline, passthrough, extension points) with stub policy. `auth()` allows everything unless you inject a `verify()`. Content rewriting works for a whole read, not across read boundaries. There is no caching or performance work.

## How it works

The kernel sends every filesystem call on the mount to the sluice process. For each one:

1. **Route.** The inode or file handle is turned back into a virtual path, and the router finds the most specific matching route. That gives the route's params (`user=alice`), the real path (`/ext4/alice/notes.txt`) or the virtual handler, and the middleware chain: global first, then each ancestor's, then the route's own.
2. **`param()` callbacks** transform params once per call (for example, decode a token).
3. **`pre` hooks** run in chain order. Any of them can deny the call (`EACCES`), change its arguments, or answer it directly.
4. **The operation** runs: a syscall on the real path in a worker thread, or the route's virtual handler.
5. **`post` hooks** run in reverse order and may replace the result, for example to redact bytes.

Paths that no route covers don't exist (`ENOENT`). The mount root and the literal parts of route patterns (`/fuse`) appear as read-only directories. Everything runs on one asyncio event loop, so middleware must be `async` and must not block. The details are in [Architecture Internals](Architecture-Internals). How to write routes and middleware is in [Writing an App](Writing-an-App).

## When not to use it

- **Performance matters.** Every call pays for routing and the whole middleware chain, and kernel caching is off by default so that no call skips middleware. Expect many times the cost of the underlying filesystem.
- **You need real security boundaries.** The daemon reads and writes the backing store with its own uid for every caller. With `mount.allow_other`, your middleware is the only thing between other users and that data. Tokens in paths are visible in other processes' command lines and `/proc`. See [Security](Security).
- **You need hard links, device files, or file locking.** `link` and `mknod` return `ENOTSUP`, and locks are not implemented.
- **Content rewriting that changes length, or that spans reads.** A `post` hook sees one read at a time. Rewriting is correct only when the whole file is read in one call.
- **Not Linux.** sluice uses libfuse3 through pyfuse3, and is developed and tested on Linux only.

For a plain bind or overlay, use `mount --bind`, overlayfs or bindfs. For access control, use filesystem permissions, ACLs or a real authorisation layer in front of the data.
