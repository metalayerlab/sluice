# 0011: YAML projection pilot

<!-- Add a line for this record to the index in docs/design/README.md (CI checks). -->

Status: Implemented (v0.2.7 to v0.2.10; acceptance tests ran through the test harness, not a live FUSE mount)
Date: 2026-11-03
Authors: Stephen Cox; drafted by Claude
Approved: 2026-11-04 by the brain coordinator for Stephen.
Implemented: 2026-11-04, approved by the arbiter (question "Mark Sluice Record 0011 Implemented"). The first run on a real mount is to be noted here.

## Context

Stephen's build spec "Sluice YAML Projection" (vault idea "🌳 Sluice YAML Projection", 2026-11-02) asks
for one YAML document mounted as a tree: mappings are directories, every scalar is a file, `cat` and `ls`
read one property at a time, and every write is validated before it reaches the YAML on disk. The pilot
document is one organisation record from the Virtual Organisations model (the private repo
`andronics/virtual-organisations`, design 0001 "Template and seed schema", approved 2026-11-03).

That model fixes what this record builds on:

- **One document, mostly mappings.** An org record has exactly `kind`, `version`, `org`, `name`,
  `status`, a verbatim `template` copy, `seed` (the seed's values, one flat mapping keyed by
  snake_case key) and `results`. Nodes, inputs, outputs, seed declarations and seed values are
  mappings keyed by id or name. The only sequences the schema names are `depends_on` (node ids,
  written order kept) and `list`-typed values (lists of scalars, order matters: `trustees`). `run.with`
  and `mapping`-typed values are free-form, so the schema doesn't rule out a list of mappings there,
  but no keyed sequence is part of the model, so no list-identity rule is needed (see [Lists](#lists)).
- **Declared types.** `string`, `integer`, `boolean`, `date`, `datetime`, `list` (of scalars),
  `mapping`. The template's `seed` section declares each seed value's type; each node's `outputs`
  declares each result's type. There is no float type, but a float is a scalar to the model, so one
  can appear where nothing declares a type (`run.with`, a list item).
- **Fixed now:** the org record's top-level keys (no others allowed), the template copy, `seed` as
  the flat values, and `results.<node>` as `status` (`done` or `failed`) beside optional
  `outputs.<key>`. The engine's own design record may settle more.
- **`results` belong to the engine.** sluice never writes them. Stephen hand-writes templates and seeds.
- **The template copy is read-only.** It is a verbatim copy of the template the record was made from.
  `validate_org` checks only that the copy is a valid template, not that it still matches its source,
  so a write there would pass validation and silently fork the record from its template. The
  coordinator for Virtual Organisations confirmed (2026-11-04) that writes go to seed values only;
  the engine's record will rule on when a seed value may change after a run.
- **The loader and validator** are `virtual_organisations.model`, merged in that repo: no engine code
  and no I/O beyond reading the file it is given.
  - `load_template`, `load_seed` and `load_org(source)` take a file path (a `Path` or a `str`, never
    YAML text), return a plain `dict` and don't validate. They raise `LoadError`, whose `code` is
    `yaml` (not valid YAML), `duplicate-key` (a mapping repeats a key) or `not-a-mapping`, with the
    file, line and column, and never the file's text.
  - `dump(doc) -> str` writes block-style YAML with pyyaml's `yaml.dump(..., sort_keys=False)`, so key
    order is kept, and with no anchors or aliases (a repeated value is written twice). Comments are not
    kept. It loads back equal, not always byte-identical: quoting and line wrapping are pyyaml's.
  - `validate(template, seed=None, *, handlers=None, webhooks=None)` and `validate_org(org)` return
    `list[Finding]`, never raise, and an empty list means valid. A `Finding(code, path, message)` has a
    rule `code` (`schema`, `cycle`, `seed-type`, ...), a `path` that is a JSON Pointer into the
    document, and a one-sentence `message`. `validate` puts seed findings under `/values/<key>`;
    `validate_org` puts the template copy's findings under `/template/...`, seed findings under
    `/seed/<key>` and result findings under `/results/<node>/...`. A document that fails its JSON
    Schema gets only `schema` findings.
  - Also `order(template)`, `json_schema(kind, version=1)` and `VALUE_TYPES`.
- **Version 1 only.** A version 2 of the VO schema is coming, loaded beside version 1 by each
  document's `version`; the pilot pins version 1 and refuses to mount any other. Version 2 adds
  references to real identifiers (bank, tax, registration) held in `pass`, written as a `pass:` path;
  see [Secret references](#secret-references).
- **Public and private.** sluice is public and the model is private. The projector is a sluice example
  that imports the model only when it is installed; whether the model goes public is decided before
  anything in sluice's package needs it (VO 0001, decision 6).

### Where the spec differs from sluice and interpose today

Checked against sluice `main` (v0.2.5, on interpose v0.2.2) and interpose `main` (designs 0003–0008;
0009, settings extensions, is approved but unreleased and changes nothing here).

| Spec says | sluice and interpose today | This record |
|---|---|---|
| A projector *middleware* registered into the pipeline | interpose's `Pipeline` (sluice 0004) runs middleware *around* an operation; the operation on a path with no backing file is a `VirtualHandler` on a `virtual=True` route | The projector is a `VirtualHandler` on one `{rest*}` route. Middleware (the access layer) wraps it as usual |
| Resolvers registered per node type via `route`/`use` | Routes match path shapes (literal, `{param}`, `{param*}`), not schema types | One route; the handler dispatches by declared type |
| Reuse sluice's auth mechanism | No middleware ships in the package (sluice 0002): `auth()` is `examples/auth.py`, whose default `verify` allows everything | The pilot's access layer is `examples/auth.py` unchanged. With `skip_inherited` on (sluice 0010), a `pre`-only gate costs a dict and a set lookup |
| Validate before commit | A `post` hook runs after the handler has acted. It can fail the call (an `FsError` it raises gives that errno, anything else `EIO`: interpose `Policy`, `on_error="fail"`), but the change has already happened | Validation is inside the handler, before anything changes. It is not a hook |
| Reject the bad write at the write syscall | `write` gets chunks (`WriteArgs(offset, data)`); `release` bypasses middleware and its errors are ignored; `close(2)` sees `flush`'s error | Checked at `write` (from offset 0) and committed at `flush` (below) |
| (not in the spec) | `pipeline.timeout_ms` is interpose's per-*hook* timeout (sluice 0006); it doesn't limit the handler, and the handler runs on the one event loop | Blocking work (validation, dump, fsync) runs in a worker thread, so a write doesn't stall the mount |
| Sequences are directories, entries named by a keyed field | No keyed sequences exist in the v1 model | A sequence of scalars is one file, an item per line |
| Strict type coercion, as in interpose's settings | interpose coerces configuration text and accepts `1`/`yes`/`on` for true | Not reused: the projector's rule is stricter (below) |
| A write "breaking a declared dependency" is rejected at the write syscall | Dependencies (`depends_on`, inputs) live in the template copy, which stays read-only | Refused earlier, at `open` (`EACCES`); a type error in a seed value is rejected at `write` (accepted by the brain coordinator, 2026-11-04) |
| "Round-trips through the template loader" | — | Through `load_org`; equal, not byte-identical |
| Write path in the pilot | — | Kept in the pilot, as a later PR than the read path (build order) |

## Decision

### Shape

- **An example, not a package feature.** Two files in `examples/`, as with every policy sluice ships
  (0002):
  - `examples/yaml_projection.py`: the generic projector. A `DocumentStore` (one per document: parse,
    hold, lock, write back) and a `Projection(VirtualHandler)` for a `{rest*}` route. It takes a small
    `Model` protocol: `load(path) -> dict`, `dump(doc) -> str`, `validate(doc) -> list[(code,
    pointer, message)]`, `type_of(pointer, doc) -> Type | None`, `writable(pointer, doc) -> bool`
    and `is_reference(pointer, doc) -> bool` (see [Secret references](#secret-references)).
    It imports only sluice and the stdlib, and is tested in sluice's CI with a sluice-owned toy
    document and a toy pyyaml model.
  - `examples/vo_projection.py`: the Virtual Organisations adapter. It imports
    `virtual_organisations.model` only when it is installed, and fills `Model` from it: `load_org`,
    `dump`, `validate_org` (each `Finding` as its code, path and message), seed value types from the
    template copy's `seed` declarations, and the writable set below. It refuses a record whose
    `version` isn't 1.
- **No new dependency.** `pyproject.toml` doesn't change. mypy gets an override for
  `virtual_organisations.*` (`ignore_missing_imports`, with its reason), since CI can't install a
  private repo.
- **Mounted:**

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

  The document's path is the handler's own configuration: a virtual route has no backend.

### What is mounted

**One org record**, through `load_org` and `validate_org`, never a template and seed pair: that would be
two documents, which the spec puts out of scope, and the VO design expects sluice to mount the org record.
A template and a seed (which Stephen hand-writes, with comments) are never mounted, so the comments pyyaml
drops are the engine's, not his. (The pilot's record, put together before the engine exists, loses any
comments on its first write.)

```text
/orgs/example-trust/
  kind  version  org  name  status
  template/name  template/description
  template/seed/<key>/{type,description,default}
  template/nodes/<id>/{description,depends_on}
  template/nodes/<id>/inputs/<name>
  template/nodes/<id>/run/{handler|webhook|approval/question,with/...}
  template/nodes/<id>/outputs/<key>/{type,description}
  seed/<key>                            (a key left to its default is absent)
  results/<node>/status
  results/<node>/outputs/<key>
```

The document must validate when it is mounted: one with findings is refused at mount, so every later
write starts from a valid document. Until the engine exists, the pilot's document is an org record put
together from VO's `smallest-charity` template and seed: the template as the copy, the seed's `values`
as `seed`, empty `results`, `status: creating`, built at test time from a VO checkout
([Open questions](#open-questions), 1).

### Scalar serialization

A leaf's content is its value's text **followed by one newline**; a write strips **one** trailing
newline, if there is one, then casts by the declared type. So `cat` prints cleanly, `echo x > leaf`
writes `x`, and read-then-write is lossless (a string that ends in a newline reads with two).

| Type | Read gives | A write accepts |
|---|---|---|
| `string` | the text itself, unquoted, unescaped | any UTF-8 text (not valid UTF-8: `EINVAL`) |
| `integer` | decimal, `-` sign if negative | `-?[0-9]+`, no `+`, `_` or leading zero |
| `boolean` | `true` or `false` | `true` or `false` only (not `yes`, `on`, `1`) |
| `date` | `YYYY-MM-DD` | `YYYY-MM-DD` |
| `datetime` | ISO 8601 with `T`, and the offset if the value has one | ISO 8601 (`datetime.fromisoformat`) |
| float (no declared type) | Python's `repr` (`0.5`) | what `float()` accepts, finite only |
| null | an empty file (0 bytes) | 0 bytes, only where the type allows null (no declared type does in v1) |

- **The declared type decides, never the text.** A string `true` stays a string. A leaf with no
  declared type (the template copy, results, values inside a `mapping`) has the type of its current
  value, and keeps it.
- **"Exactly as in the source YAML" means the same value**, not the same characters: `>-` folding,
  quotes and a `Z` offset (`+00:00` after loading) are YAML spelling, which a scalar's file doesn't show.
- `getattr` reports the real size (the projector knows the content), mode `0644` for a writable leaf
  and `0444` otherwise. Files open with `direct_io`, as virtual files do today.

### Lists

- **A sequence of scalars is one regular file, one item per line**, each item followed by a newline and
  serialized by the scalar rule: `[a, b]` reads `a\nb\n`, `[]` reads as an empty file. Order is kept
  both ways. A write replaces the whole list; an item containing a newline can't be written (`EINVAL`).
- `list` declares no item type in v1, so written items are strings (the model accepts any scalar
  items, so a list that held numbers holds strings after a write); `depends_on` items are node ids.
- **A sequence of mappings or sequences** is never declared by the v1 schema, though a free-form value
  (`run.with`, a `mapping`) could hold one: the projector refuses to mount a document that has one. No
  list-identity rule is needed until the model declares such a list (then a new record).
- A `mapping`-typed value is a directory, like any mapping.

### Writes, in two phases

1. **Read-only first.** `open` for writing, `setattr`, and every shape change fail; nothing changes on
   disk. The read acceptance criteria (`cat`, `ls`) are met here.
2. **Then writes**, once validation-on-write works, to seed values only: `seed/<key>`, cast by the
   type the template copy declares for that key (and, inside a `mapping`-typed value, its leaves, each
   keeping its current type).

   Everything else stays read-only: `kind`, `version`, `org`, `name` and `status`; `results` (the
   engine's); and the whole template copy, which is a verbatim copy of its source template (see
   [Context](#context)). A write to a read-only leaf fails with `EACCES` at `open`. Creating, removing
   or renaming anything (a new key, a new node) fails with `EPERM`: the pilot changes values, not the
   document's shape. Until the engine's record rules on when a seed value may change after a run,
   the pilot allows seed writes whatever `status` and `results` say. A reference leaf is never
   writable, whatever the writable set says ([Secret references](#secret-references)).

**A write, step by step.** `open` gives a handle with a buffer: empty with `O_TRUNC`, else the current
content. `write` changes the buffer. When a write starts at offset 0 (what `echo`, `printf` and most
editors send, one write for a small value), the buffer is cast and validated as it stands, and a failure
fails that `write` with `EINVAL`, so the shell sees the error even though it ignores `close`'s. `flush`
(and `fsync`) commit: under the store's lock,

1. cast the buffer (failure: `EINVAL`),
2. set the leaf in a deep copy of the document,
3. validate the copy (`validate_org`; any finding: `EINVAL`, and each finding logged at warning with its code and
   JSON Pointer),
4. `dump` it to a temporary file beside the document, `load_org` that back and require it equal to the
   copy (otherwise `EIO`),
5. fsync and rename it over the document, then swap the copy in as the document in memory.

A failure anywhere leaves the document on disk and in memory unchanged. `truncate(2)` on a path (no
handle) commits the shortened content the same way. A handle that wrote nothing commits nothing.
`release` only drops the buffer. Steps 1 and 3–5 run in a worker thread.

### Secret references

The condition of this record's approval. VO's schema version 2 (pending) adds references for real
identifiers (bank, tax, registration): a field holds a `pass:` path naming where the value is kept, not
the value. The projector:

- **Shows a reference as written.** The leaf's content is the reference text, the `pass:` path, by the
  scalar rule; it is never resolved to the value it names.
- **Never resolves one.** It never calls `pass`, nor runs any subprocess, for any leaf.
- **Never writes one.** A reference leaf is always read-only: `open` for writing fails with `EACCES`,
  whatever `writable` says, and `setattr` on it fails the same way.

Generically, the `Model` protocol's `is_reference(pointer, doc) -> bool` marks such a leaf, and the
projector applies these rules to every leaf it marks, before it consults `writable`. A model with no
references returns `False` everywhere. Version 1 has no such fields and the pilot refuses version 2,
so today this binds the generic projector (built and tested now) and the VO adapter's version 2
support, which needs a record of its own.

### Lock

**One lock per document store**, an `asyncio.Lock`, held across cast → validate → mutate → dump → load
back → rename → swap. Reads don't take it: they see the document in memory, which a commit replaces in
one assignment, so a read sees the old document or the new one, never half of each. One document has no
use for finer locks, and per-leaf locks wouldn't help: every write validates the whole document.

### YAML library

**The model's `load_org` and `dump`, so pyyaml, which sluice already depends on.** Not ruamel.yaml.
`dump` keeps key order (`sort_keys=False`) and writes no anchors, so a write changes the one value and
leaves the record's order alone. The round-trip is equal, not byte-identical: comments go, and quoting
and line wrapping are pyyaml's, so the first write to a hand-formatted file can reformat it once;
after that, writes change only the value. That is acceptable for a record the engine writes. The
projector never parses or emits YAML itself, so its writes can't disagree with the model about what
the document means.

## Alternatives considered

- **A separate filesystem.** Duplicates routing, the auth hook and the access log. Rejected by the spec.
- **The projector as middleware** (a `pre` hook answering with `ctx.respond`). It would be the
  operation dressed as a wrapper, and validation in `post` comes after the change has happened.
- **Writes to the template copy** (inputs, output declarations). The copy is verbatim and the
  validator doesn't compare it with its source, so an edit would fork the record from its template
  unnoticed. The VO coordinator confirmed seed values only.
- **A package feature** (`sluice.yaml`). sluice ships mechanism, not policy (0002, TODO "Decided
  against"), and the package mustn't depend on a private repo.
- **Only the VO adapter, no generic projector.** Its tests could never run in sluice's CI (the model
  is private), leaving the serialization and write path untested there.
- **Mounting the template and seed.** Two documents, out of the pilot's scope, and they are Stephen's
  hand-written files, whose comments pyyaml would drop.
- **A list as a directory of index-named files.** Indices name a different item after an insert, and
  replacing a list becomes several syscalls with no single point to validate.
- **The YAML spelling as file content** (`'03-31'` with its quotes, `>-` blocks). Quotes become part
  of what a writer must type, and a write would have to be parsed as YAML.
- **No trailing newline on read.** `cat` output runs into the prompt, and `echo` adds one anyway.
- **Validating only at `flush`.** Correct, but bash ignores `close`'s error, so `echo bad > leaf`
  would look like it worked.
- **ruamel.yaml to keep comments.** A new dependency (and its own record) for comments the engine's
  record doesn't need; the model's `dump` would no longer be the one writer.
- **interpose's strict coercion for the cast.** It is for configuration text and accepts `yes`, `on`
  and `1`; a document leaf wants one spelling per value.

## Consequences

- The idea's build order becomes PRs (below); its deliverables' README section becomes the examples'
  docstrings and a Writing-an-App section, as for the other examples.
- An edit through the mount loses the record's comments, and the first one may reformat quoting and
  wrapping; key order is kept (the model's `dump`).
- Editors that save by writing a new file and renaming it over the old one (vim's default
  `backupcopy=auto` on some files) fail with `EPERM`; `backupcopy=yes`, `echo`, `printf` and `tee` work.
  Troubleshooting says so.
- Only seed values can be changed through the mount. The template copy can't, so the copy stays the
  template the record was made from; a different graph means a new template and a new record. A seed
  key left to its default isn't in the record, and adding it is a shape change (`EPERM`).
- The spec's "a write breaking a declared dependency is rejected at the write syscall" is met at
  `open` instead: dependencies are in the read-only copy (accepted by the brain coordinator,
  2026-11-04).
- The adapter's tests need the model installed, so they don't run in sluice's CI (below).
- Rollback: delete the example files; nothing in `src/` changes.
- Wiki: Writing-an-App (the projector), Troubleshooting (editors, `EINVAL` on a write), Security (a
  writable leaf, that the access layer is a pass-through, and that a secret reference shows its `pass:`
  path, is never resolved, and can't be written), Testing-Guide (the adapter's tests).

## Build order

One PR each. `virtual_organisations.model` is merged in that repo, so nothing waits on it.

1. **This record.**
2. **Generic projector, read-only.** `examples/yaml_projection.py`: store, serialization, lists,
   `getattr`/`readdir`/`open`/`read`, refusing a document with lists of mappings; a toy document and
   model in `tests/examples/`. The `cat` and `ls` criteria on the toy document. Required: a toy
   reference leaf reads as its reference text and is never resolved.
3. **VO adapter, read-only.** `examples/vo_projection.py`, the mypy override, the mount example; the
   adapter's tests on the pilot's org record, built at test time from a VO checkout named by an
   environment variable and marked `integration` (question 1). The `cat` and `ls` criteria on the pilot.
4. **Generic projector, writes.** Buffers, the cast, check-at-write and commit-at-flush, the lock,
   atomic write-back with the load-back check, `truncate`, the errnos above; toy-model tests for every
   step, a failed write leaving the file byte-identical, and a read → write round-trip for every type. Required: a write to a reference leaf
   refused at `open` with `EACCES`, the file unchanged, and no subprocess spawned.
5. **VO adapter, writes.** The writable set (seed values), types from the declarations,
   `validate_org`; the remaining acceptance criteria (write-then-read, a seed type error rejected at
   `write` with `EINVAL`, a write to a dependency refused at `open` with `EACCES`, the file unchanged
   in both, the result loading through `load_org`).
6. **Phase 2, role scoping.** A record of its own, once the pilot holds.

## Open questions

Answered by the brain coordinator for Stephen on 2026-11-04, each with the recommendation adopted.
Earlier, the coordinator for Virtual Organisations answered key order in `dump` (kept,
`sort_keys=False`) and writes to the template copy (none: seed values only), both folded into the
Decision; and VO 0001 (decision 6) settled that the projector is a sluice example that imports the
model only when it is installed.

1. **Where the pilot's org record and the adapter's tests live.** sluice is public, the model private.
   Options: (a) a hand-made org record in sluice's tests, (b) the record built at test time from a VO
   checkout named by an environment variable, with the tests marked `integration`, (c) the record in the
   VO repo's examples. **Answer:** (b) now, (c) once the engine produces one: nothing from the private
   repo lands in sluice, and "never skip a test" holds, as integration tests are opt-in by design.
2. **The document changes on disk while mounted** (the engine, or Stephen's editor). **Answer:** at
   each commit, under the lock, compare the file's inode, size and mtime with those at load; if they
   differ, reload, refuse the write with `EBUSY`, and let the writer retry. Reads reload at `opendir`
   on a change. No watcher thread for the pilot.
3. **How a writer learns why a write failed.** An errno carries no message. **Answer:** log each
   finding (code, JSON Pointer, message) at warning; later, if wanted, a read-only `user.sluice.error`
   xattr on the leaf.
4. **`setattr` on a writable leaf** beyond the size. **Answer:** accept and ignore times (editors set
   them), fail mode and owner changes with `EPERM`.
