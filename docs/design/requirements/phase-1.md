# FUSE Filesystem with Routed Middleware — Phase 1 Requirements & Decision Record

**Status:** Phase 1 (structure + stubs only — no production logic)
**Audience:** AI architect / implementing agent
**Purpose:** Lab project. Build a custom-authorisation FUSE filesystem in Python with an Express-style routing and middleware layer, where routes map virtual paths to real backend filesystem paths (or to purely virtual/in-memory content), gated and augmented by chainable middleware.

This document is a decision record, not a spec to re-derive. Where a decision has been made, implement it as stated. Where a question is still open, it is marked explicitly — do not assume an answer.

**Illustrative examples — what this architecture enables.** These are direct compositions of primitives defined in this document (route params, backend resolution, `virtual=true`, pre/post middleware, `ctx.config`) — no new mechanism required for any of them:

1. **Multi-tenant isolation from a single mount.** One mount (`/myfuse`) serves every user. A route like `/fuse/{user}/{token}/{rest}` with `backend="/ext4/{user}/{rest}"` and an auth middleware checking `{token}` means each user is confined to their own subtree before any real syscall runs — swapping `{user}` changes the resolved real path with no per-user route duplication.
2. **Live virtual status node.** A route such as `/fuse/{user}/status` with `virtual=true` has no backing file at all — its content is generated per-call by a handler (e.g. live load/mount stats), so reading it never touches disk.
3. **On-the-fly content redaction.** A post-call middleware on a backed route can rewrite the bytes returned from a real file (e.g. stripping sensitive fields from a log) before they reach the caller — the file on disk is untouched; only what's served through the mount is altered.
4. **Templated secret injection.** A virtual route (e.g. `/fuse/{project}/config.yaml`) can assemble its content per-request from merged route params and mount-time secrets (`ctx.config.secret.*`) — producing a config file that is rendered on the fly and never persisted anywhere.

---

## 1. Feasibility & Library Choice

**Decision: use `pyfuse3`.**

- `pyfuse3` is asyncio-native (wraps libfuse3), maps each FUSE request to an asyncio task, and gives non-blocking/concurrent request handling as the native execution model — not something bolted on.
- Rejected: `fusepy`/`refuse` (synchronous by default; `multithreaded=True` gives OS-thread concurrency, GIL-bound for CPU-touching work, fights the "non-blocking" requirement). `llfuse` (predecessor to pyfuse3, superseded).
- Feasibility conclusion: the overall system (multithreaded, non-blocking, middleware-interceptable, IO-optimised) is feasible. The hard part is not the FUSE binding — it's correctness under concurrency and the cost of per-call authorisation, since FUSE calls are frequent, per-inode, and interleaved (e.g. a single large `read()` from userspace becomes many small FUSE-level calls).

---

## 2. Core Architecture Overview

```
Incoming FUSE call (pyfuse3)
        │
        ▼
Core FUSE Operations class (thin wrappers only — no logic in callbacks)
        │
        ▼
Router: match virtual path against registered routes
        │  → extract route params
        │  → merge params across ancestor chain
        │  → run param(name) callbacks for each present param (§6a)
        │  → resolve backend (real path) or mark as virtual
        │  → accumulate middleware chain (ancestors + this route + global `use()`)
        │  → assemble Context object
        ▼
Middleware chain execution (pre-call phase)
        │  → auth, validation, mutation, short-circuit
        ▼
Real operation:
  - backed route  → syscall against resolved real_path
  - virtual route → synthesized by virtual handler / middleware
        ▼
Middleware chain execution (post-call phase)
        │  → logging, result mutation, inspection
        ▼
Response to FUSE caller
```

Every intercepted FUSE operation (`lookup`, `getattr`, `open`, `read`, `write`, `setattr`, `readdir`, `unlink`, `setxattr`, etc.) follows this same shape. No FUSE callback contains filesystem logic directly — it only drives the pipeline.

---

## 3. Core FUSE Layer

**Requirements:**

- Base `Operations`-equivalent class (pyfuse3) implementing a working passthrough filesystem first, to validate the harness before anything is layered on it.
- Every FUSE callback is a thin wrapper whose only job is to invoke the event/middleware pipeline.
- A defined subclassing contract: what a derived/layered filesystem must override vs. inherits for free. (**Open question**, see §9 — inheritance vs. stacking semantics beyond the router itself.)

**Inode ↔ path mapping (required subsystem, not optional):**

FUSE operates on inodes/file handles, not paths — `read()`/`write()` act on handles obtained from an earlier `open()`/`lookup()`, not on a path string. For path-based routing to work across *every* operation, the core layer must maintain:

- **inode ↔ virtual path** mapping, kept consistent across renames/unlinks/creates.
- **virtual path → real path** resolution per call, via the router (see §5).

This is a required subsystem in phase 1 (even if unoptimised), not a stretch goal — routing cannot function without it.

---

## 4. Event Model

Every intercepted call is represented as a structured event/context object, not raw args. Minimum required fields:

| Field | Description |
|---|---|
| `op` | Operation name (`getattr`, `read`, `write`, `open`, `readdir`, etc.) |
| `args` | Original call arguments |
| `params` | Dict of extracted route params, merged across the ancestor chain (see §6) |
| `real_path` | Resolved backend path, or `None` for virtual routes |
| `virtual` | Bool — whether this route has no real backing at all |
| `config` | Mount-scoped static config (env vars / secrets — see §8) |
| `inode` / `path` context | Virtual-side identity for this call |
| `timestamp` | — |
| `correlation_id` | Request ID, for tracing a call through pre/post middleware and logs |

**Pre-call vs. post-call middleware are distinct phases:**
- **Pre-call**: can inspect params/real_path, mutate args, short-circuit (deny/error) before the real operation executes. This is where auth lives.
- **Post-call**: can inspect/mutate the result, log outcomes, cannot block the already-executed operation.

Both are expressed as "events" but serve structurally different purposes and must be distinguishable in the middleware contract.

---

## 5. Routing Layer

**API shape (Express-inspired):**

```python
route(pattern, backend=None, virtual=False, middlewares=[...])
use([mw1, mw2, ...])  # global, unscoped, runs on every call
```

`use(prefix, [mw])` (prefix-mounted, Express-style) is a possible second form — **open question**, see §9.

**Decision: backend is a route *property*, not a middleware (Option A, confirmed).**

Rejected alternative: rewrite-as-middleware (a `rewrite_mw()` that mutates `ctx.real_path` as the first item in the middleware list). Rejected because it makes real-path resolution depend on middleware list ordering — a silent contract ("auth must run after rewrite") that a reordered list can violate with no structural guard.

Chosen approach: `real_path` resolution is a property of the route, resolved by the router *before* any middleware executes. Every middleware in the chain can therefore rely on `ctx.real_path` already being correct (or explicitly `None` for virtual routes) — no ordering ambiguity. Middleware may still choose to ignore `real_path` entirely if its logic doesn't need it (e.g. a logging middleware that only cares about `op` and `params`).

**Route matching / resolution requirements (must be decided before implementation, not left implicit):**

- **Specificity/overlap resolution**: what happens when two routes match the same incoming path. (**Open question**, see §9.)
- **Match depth**: routes must support matching a full subtree (`/root/{project}` matching everything beneath it, e.g. `/root/{project}/subdir/file.txt`), not just exact-depth matches — required given FUSE must handle arbitrarily nested paths under a mount.
- **Param typing/constraints**: whether `{param}` segments support constraints (e.g. "no `/` in this segment") or are naive string captures in phase 1. Naive capture is sufficient for phase 1; constraint syntax can be deferred.

**Nested routes without their own backend — three distinct cases (not two):**

A route's backend resolution has three possible states, and they must be represented distinctly — not inferred from presence/absence of a single field:

1. **Has its own backend** — `backend="/ext4/{user}/{rest}"` declared directly on the route.
2. **No backend, inherits from ancestor** — route exists purely to attach additional middleware (e.g. extra gating/logging) on a path beneath a parent route that already established a real-path mapping. Example: parent `/fuse/{user}/{token}` → `backend="/ext4/{user}"`; child `private/{rest}` → `backend=None`, adds `admin_only_mw` on top, without re-declaring the `/ext4/{user}` mapping.
3. **No backend, explicitly virtual** — `virtual=True`. A purely synthetic node with no real filesystem backing at all (e.g. `/fuse/{user}/status` returning live-generated content), analogous to procfs-style in-memory virtual files. This must **not** inherit a parent's backend even if one exists in the ancestor chain — `virtual=True` is a hard stop on the inheritance walk.

```python
@dataclass
class Route:
    pattern: str
    backend: Optional[str] = None  # case 1 if set
    virtual: bool = False  # case 3 — explicit opt-out of inheritance
    middlewares: list = field(default_factory=list)
```

Resolution walk (root → matched node) must:
- accumulate middleware from every ancestor plus the matched route plus global `use()` entries,
- carry the nearest ancestor's `backend` down to a child with `backend=None` and `virtual=False` (case 2),
- stop inheritance immediately at any node with `virtual=True` (case 3), leaving `real_path = None`,
- merge route params across the ancestor chain (see §6).

**Open question (see §9, flagged but not yet resolved):** for virtual routes, is content synthesized by a dedicated handler on the route (`route.virtual_handler(op, ctx) -> result`), or does the middleware chain itself produce the response (last middleware = the implementation)? This is the same "wrap-with-`next()` vs. replace-per-operation" question as the general middleware contract (§6), sharpened specifically for the no-backend case.

---

## 6. Middleware Contract

**Required decision before implementation (currently open — see §9):** does middleware *wrap* the underlying FUSE operation (pre/post around a `next()` call, chaining through multiple middlewares to an eventual real op), or does a middleware *replace* the implementation for specific operations on its route (e.g. a middleware object declaring `onRead`, `onGetattr` handlers, with unhandled ops passing through to default behaviour)?

This determines whether middleware is:
- (a) a single generic function with `(ctx, next) -> result` signature (Express-style), or
- (b) an object with named per-operation hooks, some of which may be absent (pass-through).

The user's phrasing ("augment the various fuse functions") leans toward (b), but this must be confirmed, not assumed, before base classes are written — it changes method signatures throughout the core layer.

**Constraint regardless of which model is chosen:** middleware must be async-compatible by contract. A blocking middleware (sync I/O, slow computation) breaks the non-blocking guarantee for the entire pipeline, not just its own call. This must be a stated, enforced constraint — e.g. a lint/runtime check that flags sync blocking calls inside middleware — not an informal expectation.

**Param merging across the ancestor chain:**

When a backend template or middleware references `{param}`, the param dict must be built by **walking the full match chain from root to the matched node**, not just the matched route's own capture group. Example: parent `/fuse/{user}/{token}` captures `user`, `token`; child `private/{rest}` captures `rest`; the child's effective param dict (available to its own backend template and middleware) must contain all three. Child captures override parent captures on key collision (default; not yet stress-tested against edge cases).

**Static validation requirement (phase 1, not deferred):** at route *registration* time, validate that every `{param}` referenced in a `backend` template is satisfiable by the route's own pattern captures plus whatever its ancestor chain guarantees. A `{param}` referencing an unresolvable name must fail at registration/mount time, not surface as a runtime `KeyError` on first matching request.

### 6a. Param Callbacks (`param(name, func)`)

**Decision: adopt Express's `app.param(name, callback)` pattern.**

A `param()` callback is bound to a **param name**, not a route. It fires whenever any matched route (at any level of the ancestor chain) contains a param with that name — regardless of which specific route matched — and runs **once per request**, after param extraction/merge and **before** the route's own middleware chain executes.

```python
param(name: str, func: Callable)   # func(ctx, raw_value) -> transformed_value
```

Purpose: centralises per-param preprocessing that would otherwise be duplicated across every route referencing that param. Example: rather than every route's auth middleware independently decoding `{token}`, a single `param('token', decode_and_attach)` runs once per request, transforms the raw captured string (e.g. into decoded claims or a loaded record), and every subsequent middleware in that route's chain sees the enriched value in `ctx.params.token`.

**Capabilities:**
- May transform the param value in place (raw string → richer object) before middleware sees it.
- May short-circuit (deny / not-found-equivalent) — e.g. a `param('user', ...)` callback that looks up a user ID and finds nothing, rather than letting every route's middleware independently rediscover that failure.

**Execution order:** `param()` callbacks run in registration order, after the full param set for the matched node is known (i.e. after ancestor-chain param merge), before any route/global middleware.

**Open question (added to §9):** if a param name is captured once but referenced by multiple nodes in the same ancestor chain (e.g. `{user}` captured at a parent, implicitly relevant to a child via inheritance), does its `param()` callback fire once per request (Express's behaviour — once per distinct value per request) or once per node in the chain that references it? Default assumption carried over from Express: **once per request per distinct value.** Must be confirmed, not re-derived, during implementation.

---

## 7. Authorisation Mechanism

- Phase 1: pluggable **hook point only** — a default-allow pre-call middleware stub, not a real policy engine.
- Expressed as middleware (not a separately-registered mechanism distinct from the router) — i.e. `use()`/`route()`-registered like any other middleware, consistent with the routing model rather than a bespoke parallel system.
- Required per-call inputs at the decision point: identity (`{user}`), credential (`{token}`), resolved `real_path`, requested operation (`op`) → allow/deny decision.
- Credential verification (e.g. token validity) must be an **injected, swappable dependency** — `verify(user, token, real_path, op) -> bool` — not hardcoded into the auth middleware. Phase 1 stub always returns `True`; the signature is the phase 1 deliverable, not the logic behind it.
- **Open question** (noted, not blocking phase 1 stub): is auth a plain middleware entry like any other, or does it deserve a first-class distinguished registration form (e.g. `authorize(pattern, policyFn)`) separate from generic `route()`/`use()` middleware? Deferred — phase 1 can proceed with auth-as-middleware.

---

## 8. Mount-Time Configuration: Environment Variables & Secrets

**Requirement:** backend templates and middleware must be able to reference environment variables and secrets (Docker/Kubernetes-sourced), in addition to route params — not strictly limited to mount-time resolution, but that is the phase 1 default.

**Decision: static resolution at mount time for phase 1.**

- Env vars/secrets are read once when the FUSE process starts, frozen into an immutable config object, and injected into every call's context thereafter (`ctx.config`).
- Rejected for phase 1: per-call dynamic resolution (re-reading/re-fetching on every request). This would put I/O (or at minimum a lookup) on the hot path of every FUSE call, directly conflicting with the stated non-blocking/IO-throughput requirement. If dynamic secret rotation is needed later, it must be layered in as a cached-with-periodic-refresh mechanism (e.g. re-read every N seconds), not a per-call fetch — and the call-site signature middleware/backend templates see should not need to change when this is added.

**Namespace separation (required, not optional):**

Config values must **not** share a flat namespace with route params — a route param `{user}` colliding with an env var `USER` is a real risk otherwise. Decision: explicit prefix in templates, e.g.:

```python
backend = "/ext4/{user}/{env.STORAGE_PREFIX}/{rest}"
```

**Open question:** whether plain env vars and secrets share one namespace (`env.*` for both) or are deliberately separated (`env.*` vs `secret.*`). Leaning toward separation, since secrets warrant different handling downstream (see below) — not yet finalized.

**Context placement:** a third field on the event/context object, alongside `params` and `real_path` — `ctx.config` — static, mount-scoped, same dict/object reference shared across all calls in phase 1.

**Security requirement (phase 1, not deferred):** logging/inspection middleware must have a redaction rule from the start — never log `ctx.config.secret.*` (or equivalent) — rather than retrofitting redaction after a secret has already reached a log file. This should be enforced structurally (e.g. the logging middleware stub redacts by default, not "redacts if someone remembers to configure it").

---

## 9. Open Questions (explicitly unresolved — do not assume answers)

These are flagged throughout the document and collected here for visibility. None should be resolved by the implementing agent without checking back — they materially affect base class shapes.

1. **Layering model beyond the router**: does "layered over existing FUSE class" mean inheritance-based specialization (subclasses overriding a base `Operations` class) or stacking independent FUSE filesystems (one's output feeding another's input, overlayfs-style)? Phase 1 discussion has proceeded on the inheritance assumption (Option A from early discussion) with the router as the mechanism for specialization *within* that — but this should be explicitly confirmed before the subclassing contract (§3) is finalized.
2. **Middleware contract shape**: wrap-with-`next()` (generic function chain) vs. replace-per-operation (named-hook object, e.g. `onRead`/`onGetattr`). Affects base class method signatures directly — needs resolving before any middleware base class is written.
3. **Route specificity/overlap resolution**: when two registered routes match the same incoming path, what determines precedence — registration order, specificity (longer/more literal match wins), or is overlap a registration-time error?
4. **`use(prefix, [mw])` form**: does phase 1 need both global `use()` and prefix-mounted `use()`, or just global `use()` plus `route()`?
5. **Virtual route content synthesis**: dedicated `virtual_handler` on the route vs. middleware-chain-as-implementation. Same shape question as #2, specific to the no-backend case.
6. **Auth as plain middleware vs. first-class `authorize()` registration**: deferred, not blocking, but should be revisited once the middleware contract (#2) is settled, since it affects whether auth gets special registration syntax or is just another `use()`/`route()` entry.
7. **Env/secret namespace split**: `env.*` shared vs. `env.*`/`secret.*` separated.
8. **Child param override semantics**: confirmed default is "child overrides parent on key collision" but not yet stress-tested against real route trees — worth revisiting once nested routing is implemented and tested.
9. **`param()` callback firing granularity**: once per request per distinct value (Express default, carried over as assumption) vs. once per ancestor-chain node referencing the name — needs confirming during implementation, not re-derived.

---

## 10. Explicitly Out of Scope for Phase 1

- Real authorisation policy logic (beyond the default-allow stub + swappable `verify()` signature).
- Real token/credential verification implementation.
- Performance tuning / benchmarking.
- Actual on-the-fly content modification logic (the hook/pipeline existing is sufficient; the transformation logic itself is not phase 1).
- Multiple simultaneous mounts / multi-mount orchestration (unless open question #1's resolution requires it).
- Dynamic (per-call) env/secret refresh — static mount-time only, per §8.
- Route pattern constraint syntax (typed/constrained `{param}` segments) — naive string capture only.
