# 0002: Middleware lives in examples/; strmfs and ShadowFS on sluice

Status: Implemented
Date: 2026-10-31 (revised 2026-11-01)
Authors: Stephen Cox; drafted by Claude
Supersedes: the built-ins part of [0001](0001-initial-design.md) (`auth()`, `access_log()` in `middleware.py`)

## Context

Two earlier projects solve problems that sluice is meant to cover generically:

- **fuse-strmfs** (fusepy, one script): mounts a media tree and shows each video file `X` as `X.strm`,
  a small text file whose content is the source location. Other files are hidden; directories pass
  through. Kodi, Jellyfin and similar players read `.strm` files as "play this location".
- **ShadowFS** (fusepy): rules (show or hide files by pattern), transforms (rewrite content on read:
  Markdown to HTML, decompress, templates), virtual layers (`by-type/`, `by-date/YYYY/MM/DD/`,
  `by-tag/`, the same file listed in several places), a three-level cache, and a list of middleware
  ideas (audit, quota, versioning, encryption, and more).

Mapping both onto sluice raised a scoping question: which of these belong in the package? 0001 put two
policies (`auth()`, `access_log()`) in `middleware.py`; the first draft of this record added two more
(`hide()`, `transform()`). Each one widens the public API, and each is a policy a real app will want
to change.

**Proven so far.** strmfs as one `Middleware` on a backed route was run through the `Dispatcher`
(no FUSE): `ls` shows only `Heat.mkv.strm` (size 40) for a directory holding `Heat.mkv` and
`Heat.nfo`; reading it returns `http://media.example.org/Films/Heat.mkv`; `Heat.mkv`,
`Heat.nfo.strm` and a missing `Nope.mkv.strm` are `ENOENT`; opening for write is `EROFS`. Nothing in
sluice changed.

Constraints from 0001 that still hold: one event loop (no blocking in hooks); the backend is a route
property, not middleware (`TODO.md`, "Decided against"); `post` on `read` sees one call at a time, so
a rewrite that changes length can't be done there.

## Decision

### 1. The package ships mechanism; middleware ships as examples

`src/sluice/` keeps the `Middleware` base, the pipeline, `param()`, `ctx.respond()`, route properties
and the backend. **No concrete middleware lives in `src/`.** Every policy is an example under a new
top-level `examples/` directory, read and copied by app authors, not imported from the wheel:

| Example | From |
|---|---|
| `examples/auth.py` | `auth()`, `Auth`, `allow_all` moved out of `middleware.py` |
| `examples/access_log.py` | `access_log()`, `AccessLog` moved out of `middleware.py` |
| `examples/strm.py` | strmfs |
| `examples/hide.py` | ShadowFS rules |
| `examples/layers.py` | ShadowFS virtual layers (symlink views) |
| `examples/transform.py` | ShadowFS transforms |

- `sluice.__init__` stops exporting `auth` and `access_log` (breaking: the PR is `refactor!:`).
- Secret masking stays core: it is `ctx.display_path` and `secret=True`; `access_log` only consumes it.
- Core tests that used `auth()`/`access_log()` as convenient real middleware (two-path authorisation,
  masking, `sluice routes` output) switch to small middleware defined in the test, so core tests don't
  depend on `examples/`.
- Each example has a test in `tests/examples/` through the `Dispatcher`. ruff and mypy cover
  `examples/`; the coverage floor stays on `sluice` only.
- An app uses an example by copying it beside the app module (`app.directory` already puts that
  directory on `sys.path`).

### 2. Core changes the examples need

**Gap A: `ctx.backend`, a safe read-only backend view (first PR).** Middleware that does I/O today
calls `os.stat` and friends directly, which follow symlinks out of the backend; `Passthrough` reaches
files through `_beneath` (`O_NOFOLLOW` below the template's prefix). `ctx.backend` exposes `lstat(path)`,
`read_all(path, limit)` and `scandir(path)` on the same thread pool with the same rule, accepting only
paths inside the resolution's backend root, and raising `FsError` with the real errno. `None` on
virtual routes. Examples ship after it, so none teaches the unsafe pattern.

**Gap B: `OSError` in hooks maps to its errno.** `_as_fs_error` maps `OSError` only at the operation
stage, so `ENOENT` from a hook surfaces as `EIO`. Map an `OSError` with an errno at every stage; keep
`EIO` (and the logged traceback) for everything else. Same PR as gap A.

**Deferred: `names=` route property.** A route-level `to_real`/`to_virtual` name map would stop
`Strm` re-deriving names in five ops. It doesn't remove the middleware: `readdir` entries and
`getattr` would still report the `.mkv` size, not the `.strm` body's, so `Strm` still hooks them. It
also needs a round-trip rule (`to_real(to_virtual(x)) == x`), collision handling and a meaning for
`create`/`rename`. Revisit when a second example needs it.

**Deferred: `resolver=` route property** for layers whose entries are real files, only if symlink
views prove a problem.

### strmfs as an example

| strmfs behaviour | sluice hook |
|---|---|
| list `X.mkv` as `X.mkv.strm`, hide non-video files | `post` on `readdir`: rename or drop entries |
| `stat X.mkv.strm` | `pre` on `lookup`/`getattr`: `ctx.backend.lstat` the real `X.mkv`, `ctx.respond(Attr.file(size=len(body)))` |
| read the `.strm` | `pre` on `open`: `ctx.respond(Opened(data=body, direct_io=True))`; `Passthrough.read` already serves an fd-less `Opened` as a snapshot |
| read-only | `open` for write → `EROFS`; `Passthrough.write` refuses an fd-less handle anyway |

Non-video files are hidden from lookup as well as listing (strmfs hid them only from `readdir`): a
listing and a lookup should agree.

```python
app = Sluice()
app.route("/media", backend="/srv/media", middlewares=[Strm("http://media.example.org/{rel}")])
```

What sluice adds over strmfs: per-route auth and access logging (from the examples), several sources
under one mount (`/films`, `/tv`), `{env.*}` in the backend, and a URL template instead of a host path.

### ShadowFS ideas as examples

| ShadowFS | Example | Core change |
|---|---|---|
| Rules: include/exclude by glob, size, date | `hide.py`: `post` on `readdir` drops matches, `pre` on path ops raises `ENOENT` | none |
| Transforms: content rewrite on read | `transform.py`: on `open`, `ctx.backend.read_all`, run `fn`, respond with an fd-less `Opened(direct_io=True)` | gap A |
| Transform pipeline | the middleware chain: each transform on `open` passes `Opened.data` to the next through `post` | none |
| Virtual layers: `by-type/`, `by-date/`, `by-tag/` | `layers.py`: a `VirtualHandler` on `/by-type/{rest*}` over an index, entries are **relative** symlinks (`../../src/project.py`) into the backed route, so reads get its middleware and the app needn't know the mountpoint | none |
| Cache | kernel cache (`mount.cache_ms`) for attributes; the transform example keys its cache by `(real_path, st_mtime_ns, st_size)`, bounded by bytes | none |
| Audit, quota, rate limit | `access_log.py`; quota and rate limit are `pre` hooks on `write`/`create` | none |
| Hot reload, control API | out of scope: the app is Python; `sluice routes`/`resolve` cover inspection | none |
| Versioning, encryption, dedup, cloud sync | need a write-side counterpart of the open-time snapshot | future record |

The layer index (scan, classify by extension/mtime/xattr tags, rebuild) is example code, run off the
event loop (`asyncio.to_thread`) at mount and on a timer.

**Transform sizes.** A transformed file's `getattr` must report the transformed length, so a cold
`ls -l` runs the transform on every matching file. The example caches by `(real_path, st_mtime_ns,
st_size)` and refuses files above `max_bytes` (default 64 MiB) with `EFBIG`, so a reader never mistakes
raw bytes for transformed ones.

## Alternatives considered

- **Keep `auth()`/`access_log()` as the only built-ins** (security and observability basics). Rejected:
  one rule with no exceptions is easier to hold, and `auth()` is a default-allow stub whose policy is
  the injected `verify`, so there is little shared code whose fixes a copy would miss.
- **Ship examples as an installable subpackage (`sluice.contrib`).** Same API-surface problem under
  another name.
- **strmfs as a virtual route** (`/media/{rest*}`, `virtual=True`). Re-implements passthrough for
  directories and loses the backend's beneath-the-prefix safety.
- **Name mapping as middleware that rewrites `real_path`.** Rejected in 0001 (order-dependent).
- **Transforms in `post` on `read`.** Can't change length or span reads (0001).
- **Port ShadowFS wholesale.** Its YAML config model duplicates sluice's Python app model; its fusepy
  core is what sluice replaces.

## Consequences

- Breaking: `from sluice import auth, access_log` stops working; apps copy `examples/auth.py` and
  `examples/access_log.py`. Acceptable at 0.1.x.
- `ctx.backend` widens the public API (Writing-an-App, Architecture-Internals); its path rule goes in
  Security.
- strmfs becomes a ~60-line example; the fusepy script can be retired, with fstab use through
  `sluice mount`.
- AGENTS.md's repository map gains `examples/`; `pyproject.toml` adds it to mypy `files`.
- Wiki: Writing-an-App (examples instead of built-ins, `ctx.backend`), Quick-Start, Home, Introduction,
  Security, CLI-Reference (routes output), Testing-Guide (`tests/examples/`), Troubleshooting,
  Frequently-Asked-Questions ("where did `auth()` go?", "can sluice do strm files / views by type?").
- `TODO.md` "Decided against": built-in middleware in the package; `sluice.contrib`.

## Open questions

1. **Ship order.** Recommend, one PR each: (1) gaps A and B; (2) move `auth`/`access_log` to
   `examples/` with `tests/examples/` and the lint/type scope; (3) `strm.py`; (4) `hide.py`;
   (5) `layers.py`; (6) `transform.py`. Gaps first so no example ships the unsafe I/O pattern.
2. **Examples' import path.** Recommend copy-beside-the-app (no packaging, no `sys.path` tricks in
   tests beyond `tests/examples/conftest.py` adding `examples/`). The alternative, documenting
   `app.directory = "<repo>/examples"`, couples apps to a repo checkout.
3. **`ctx.backend.lstat` vs a following `stat`.** strmfs libraries often symlink videos. Recommend
   `stat(path)` that follows links only while they stay beneath the prefix, plus `lstat`; `Strm` uses
   `stat`.

## Implementation notes

Shipped in the recommended order: #39 (gaps A and B), #41 (`auth`/`access_log` to `examples/`), #42 (`strm.py`),
#43 (`hide.py`), #44 (`layers.py`), #45 (`transform.py`); plus #40, a fix found on the way (a backend whose literal
prefix is itself a symlink showed as a symlink at the route root). Where the code differs from the text above:

- Question 3: `ctx.backend.stat` follows a final symlink only while its target stays inside the **rendered backend
  root**, not the literal prefix, so `/ext4/{user}` hooks for alice can't stat bob's files through a link.
- Gap B: an `OSError` keeps its errno only if it is a kernel errno; `socket.gaierror`, `ssl.SSLError` and unknown
  numbers stay `EIO` and are logged.
- Layers: sluice has no mount-lifecycle hook, so the index is built on first use and rebuilt in the background once
  older than `refresh`, rather than at mount and on a timer. Names two files share in a view carry their source
  directory (`project@docs.py`).
- `Strm` answers `.strm` calls with `ctx.respond()`, so it must follow any authorisation middleware on its route.
