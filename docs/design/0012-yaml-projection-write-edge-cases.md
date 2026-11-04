# 0012: YAML projection write edge cases

<!-- Add a line for this record to the index in docs/design/README.md (CI checks). -->

Status: Approved
Date: 2026-11-04
Authors: Stephen Cox; drafted by Claude
Approved: 2026-11-04 by the arbiter (question "Approve Sluice Record 0012 On YAML Write Edge Cases").
Supersedes: part of [0011](0011-yaml-projection-pilot.md) (its write rules: a truncating `open` with no `write`, the type of a written list's items, and the check at a write at offset 0)

This record supersedes those three points only; everything else in 0011 stands. 0011 is Implemented, so
it is superseded in part by a new record, not rewritten
([issue #85](https://github.com/metalayerlab/sluice/issues/85)).

## Context

0011's aims, which each case below falls short of: a shell tool sees a bad write as an error; a failure
leaves the document unchanged; and what a read gives, a write takes back unchanged ("read-then-write is
lossless", 0011 "Scalar serialization"). The code follows 0011 as written (`examples/yaml_projection.py`);
the review of that code found the three cases.

### Case 1: `: > leaf` does nothing

0011: "`open` gives a handle with a buffer: empty with `O_TRUNC`, else the current content" and "`truncate(2)`
on a path (no handle) commits the shortened content the same way. A handle that wrote nothing commits
nothing."

`Projection.open` gives an `O_TRUNC` handle an empty `_Buffer` whose `dirty` flag is false; only `write`
and a size change through `setattr` set it, and `flush` returns at once when it is false. libfuse 3 asks the
kernel for atomic `O_TRUNC` by default, so `: > leaf`, `cp /dev/null leaf` and an editor that truncates
and then has nothing to write arrive as one `open` with `O_TRUNC` and no `write`: nothing is committed and
every step succeeds. Without atomic `O_TRUNC` the kernel sends the `open` without the flag and then a
size-0 `setattr` on the handle, which `setattr` turns into `_Buffer.resize(0)`, so the same command does
commit. `truncate(2)` on a path commits too. The same request commits or not, depending on how it is
spelled and on the kernel's flags.

### Case 2: list items become strings

0011: "`list` declares no item type in v1, so written items are strings (the model accepts any scalar items,
so a list that held numbers holds strings after a write)."

`render` writes each item by its own type (`3` reads `3`, `true` reads `true`, a null item an empty line),
but `cast` splits a list's content into lines and keeps every line a string. So `cat tags > t; cat t > tags`
turns `[alpha, 3, null, 2.5]` into `[alpha, "3", "", "2.5"]`; the model's `validate` accepts it (any scalar
is a valid item), and the commit succeeds. This holds for every list, declared (`seed/<key>` of type
`list`) or not (a list inside a `mapping` value).

### Case 3: the offset-0 check sees one chunk

0011: "When a write starts at offset 0 (what `echo`, `printf` and most editors send, one write for a small
value), the buffer is cast and validated as it stands, and a failure fails that `write` with `EINVAL`."

`Projection.write` copies the chunk into the buffer, then calls `DocumentStore.check` (cast and validate)
whenever the chunk's offset is 0, before any later chunk has arrived. Leaves open with `direct_io`, so a
program's `write(2)` reaches the handler as it was made, split only where it exceeds the mount's maximum
write. Two things then fail a valid value:

- a value larger than one write: the first chunk alone may not cast or validate;
- a multi-byte UTF-8 character split at a chunk boundary (a program that writes in fixed-size blocks):
  `cast` decodes the buffer and fails on the incomplete trailing sequence (`not valid UTF-8`).

Either way the `write` fails with `EINVAL` though the `flush` commit of the whole value would succeed.

## Decision

### Case 1: a truncating open is a change

An `open` with `O_TRUNC` on a writable leaf marks its handle's buffer as changed (`dirty`), so `flush`
commits the empty buffer exactly as `truncate(2)` to size 0 on the path does: cast, validate, dump, load
back, write back, or change nothing. Any open that includes `O_TRUNC` counts, `O_RDONLY | O_TRUNC` too,
since Linux truncates on it and the projector already gives it a write handle. An `open` without `O_TRUNC`
that writes nothing still commits nothing.

What an empty buffer means follows from `cast` unchanged (0011: 0 bytes are null):

| Leaf | Empty buffer at `flush` |
|---|---|
| `list` (declared, or a current list) | `[]`: the list is emptied, if the model's `validate` allows an empty list |
| null (no declared type, current value null) | null: the value it already has |
| `string` | `EINVAL`: an empty file is null, which a string leaf can't hold. `echo > leaf` (one newline) writes the empty string |
| `integer`, `boolean`, `date`, `datetime`, float | `EINVAL` |
| `mapping` | not reached: a directory, so `open` fails with `EISDIR` |

Why: one request now has one result however it arrives, and it is the one `truncate(2)` already gives.
Where the empty value is refused, the document stays unchanged and the refusal is logged with its code
(`cast`) and JSON Pointer, as for any write. A shell still exits 0 for `: > leaf`, since it ignores
`close`'s error and the open is the only call it makes; a program that checks `close` (`cp`, Python) sees
`EINVAL`.

This relies on something the code already does and must keep: a `write` that fails its check still leaves
its bytes in the buffer, so a refused `echo bad > leaf` fails again at `flush` instead of committing the
empty buffer its `O_TRUNC` open left (question 3).

### Case 2: a written list keeps its items' types

The list being replaced types the items written to it, the way 0011 types a leaf with no declared type
("has the type of its current value, and keeps it"):

- **By position.** Written item *i* takes the type of the current item *i*, and is cast by that type's
  scalar rule, strictly (0011's table, per line): `3` is an integer where an integer was, a string where a
  string was. An item that doesn't cast fails the write with `EINVAL`.
- **Past the end.** Items beyond the current list's length take the type of its last non-null item; a list
  that is empty, or holds only nulls, takes strings, as in 0011.
- **Nulls.** At a position whose current item is null, an empty line stays null, and any other text is
  cast by the type the list's non-null items share, else as a string. At any other position an empty line
  is the empty string in a string slot, and fails the cast elsewhere.
- **Declared types win.** If a later schema declares a list's item type, that type wins over position,
  as a declared type wins over a leaf's current value in 0011.
- **Mixed lists keep their length.** A write that changes the length of a list whose non-null items have
  more than one type is refused with `EINVAL`, logged like 0011's other refusals (at warning, with the
  code `list-types` and the JSON Pointer, never the value); such a list can still be edited in place
  (question 2).

So every unchanged list reads and writes back equal, mixed or not (except an item holding a newline,
which 0011 already can't write), and the common edits to a list of one type (change, insert, delete,
append items) keep that type. The rule needs nothing from the model and parses no YAML.

`depends_on` is in the template copy, which stays read-only (`EACCES` at `open`), so this changes nothing
for it. Its items are node ids, which are mapping keys and so strings (a non-string key is refused at
mount), so a list of them would keep strings under this rule anyway.

### Case 3: check at write only a short, whole write

At a `write` at offset 0, the buffer is checked (cast and validate, as now) only when both hold:

1. **The chunk is shorter than 4096 bytes.** This assumes the default mount limits, which sluice doesn't
   change (question 4). With `direct_io` the kernel splits a `write(2)` only where it exceeds the maximum
   write or the pages one request may carry. The maximum write is never below 4096
   bytes, and sluice sets neither limit, so on its mounts a shorter chunk is the whole of one `write(2)`,
   not the first piece of a split one. A fixed limit is used because the handler can't see the limits
   libfuse and the kernel agree on. A chunk of 4096 bytes or more (a large value, or a program writing
   in full blocks) is checked only at `flush`.
2. **The buffer doesn't end in an incomplete UTF-8 sequence.** One to three trailing bytes that start a
   multi-byte character without all its continuation bytes mean "not checked yet": the write succeeds,
   and `flush` checks the whole buffer. Invalid UTF-8 anywhere else still fails the write.

`echo bad > leaf` and `printf bad > leaf` still fail at `write` with `EINVAL`: one short call at offset 0.

What the check can't catch, all still caught at `flush` (`EINVAL` from `close`, and the refusal logged),
with the document unchanged:

- a value of 4096 bytes or more;
- a value written in several `write(2)` calls whose first piece checks clean (`{ printf 1; printf x; } >
  leaf` on an integer);
- writes that don't start at offset 0, and a value ending in a split character.

And one case it can still get wrong: a value written in several short `write(2)` calls whose first piece
alone doesn't cast or validate (`{ printf 2026-; printf '11-04\n'; } > leaf` on a date) fails at its first
`write`. At that write it can't be told from `printf 2026- > leaf`, a bad value in one call, which the
check exists to catch: no rule that looks at one write can tell them apart. The writer gets a visible
`EINVAL`, never a silent change, and one `write(2)` of the whole value works.

## Alternatives considered

- **Case 1: keep 0011's rule and document `: > leaf` as a no-op.** The same request would still commit or
  not depending on the kernel's flags and on `truncate(2)` versus `open`, and a silent success is what 0011
  set out to avoid.
- **Case 1: check the empty buffer at `open`.** `echo x > leaf` opens with `O_TRUNC` too, so every write to
  a leaf that can't be empty would fail before its value arrived.
- **Case 2: resolve each item as YAML would (`3` an integer, `"3"` a string).** The projector would parse
  YAML text, which 0011 rules out, and the text would decide the type, which 0011 rules out too ("a string
  `true` stays a string"). pyyaml resolves YAML 1.1, so `No`, `on`, `007` and `2026-11-04` as names or ids
  would turn into booleans, numbers and dates: the same bug, run the other way.
- **Case 2: one type for the whole list** (the type its items share, else strings). Simpler, but a mixed
  list, like the toy document's `tags`, still loses its types on an unchanged write.
- **Case 3: check only a buffer that ends in a newline.** It never rejects a valid value for any type that
  can't hold a newline, but `printf bad > leaf` (no newline) would pass `write` and fail only at `close`,
  which the shell ignores.
- **Case 3: compare the chunk with the mount's own maximum write.** The handler can't see it, and the
  kernel can split an unaligned buffer up to a page early, so the limit would need a margin anyway.
- **Case 3: fail only what no later write could fix** (a prefix grammar per type). It needs a grammar for
  every type's prefixes, `datetime` and float included, and still can't apply the model's `validate` to a
  prefix.

## Consequences

- **What a user sees.** `: > leaf` empties a list and, on any other leaf that can't be empty, fails at
  `close` with `EINVAL` (logged) instead of succeeding silently; a shell's exit status is still 0. A list
  written back unchanged keeps its numbers, booleans, dates and nulls, and an item that doesn't cast to its
  position's type fails with `EINVAL` where it used to become a string. A value of 4096 bytes or more, or
  one ending in a split character, is accepted when valid and refused at `close` when not, where it used to
  fail at its first `write` either way.
- **Version.** A `fix(examples):` PR, so a patch release (0.2.x; before 1.0 the release rules also bump
  only the patch for a feature). The only change in `src/` is the 4096-byte constant, kept beside the
  mount options (`fuse_options` in `src/sluice/mount.py`) and imported by the example, with a test that
  fails if either limit it assumes (the maximum write, or the pages per request) is ever set there.
  Rollback is reverting that PR.
- **Tests**, in `tests/examples/test_yaml_projection.py` through the `Dispatcher` harness, as 0011's were:
  one per case. `: > leaf` (an `O_TRUNC` open, no write, flush) empties a list and fails with `EINVAL` on
  an integer, the file byte-identical; the toy's mixed `tags` written back unchanged loads equal, and an
  appended item takes the last non-null item's type; a value of 4096 bytes or more written in two chunks,
  and a multi-byte character split across two chunks, both commit at `flush`, while one short bad chunk
  still fails at `write`; a mixed list whose length changes fails with `EINVAL` (`list-types`); a write of
  an unchanged value leaves the file and its mtime alone; and the mount-limits test above.
  `test_a_handle_that_wrote_nothing_commits_nothing` keeps its `O_WRONLY` case and loses
  its `O_TRUNC` one, and the "items are strings" cases of the list test change with case 2.
- **Docs.** The module docstrings of `examples/yaml_projection.py` and `examples/vo_projection.py` ("a
  handle that wrote nothing commits nothing", "its items strings"). Wiki: Writing-an-App (the paragraph
  that tracks issue #85 becomes these rules) and Troubleshooting (the `EINVAL` entry: an empty file, list
  items, the `list-types` refusal, and which writes are checked at `close` only; and that with two
  write handles on one leaf, the last to close wins). The code PR adds this record's rejected
  alternatives to TODO.md's "Decided against".
- **Index.** 0011's row says part of it (its write rules on these three points) is superseded by 0012.

## Open questions

Answered by the arbiter on 2026-11-04 (question "Approve Sluice Record 0012 On YAML Write Edge Cases"),
each with the recommendation adopted and the changes it asked for folded in.

1. **A commit whose value equals the current one still rewrites the file.** `flush` dumps and renames even
   when the cast value equals the leaf's current value: case 1 on a null leaf, and case 2's unchanged list,
   so a no-op write can still reformat the file and drop its comments (0011 "YAML library"). Recommend: the
   code PR skips the dump and write-back when the cast value equals the current one (the document validated
   when it was loaded), so a no-op write leaves the file and its mtime alone. **Answer:** as recommended.
   The skip runs only after 0011's on-disk change check (inode, size and mtime; on a change, reload and
   `EBUSY`), and compares with the value in the document as reloaded, never a stale copy.
2. **A mixed list whose length changes.** By position, deleting `alpha` from `[alpha, beta, 3]` shifts `3`
   into `beta`'s string slot, where it casts silently as `"3"`. Recommend: refuse with `EINVAL` (logged as
   code `list-types`) a write that changes the length of a list whose non-null items have more than one type;
   such a list can still be edited in place. v1's writable lists are names (`trustees`), so this binds only
   free-form values and the toy document. **Answer:** as recommended, logged like 0011's other refusals
   (warning, code `list-types`, JSON Pointer); now in the Decision (case 2).
3. **A failed write's bytes stay in the buffer.** POSIX says a failed `write` transfers nothing; here the
   bytes stay, and `flush` fails on them again. Recommend: keep it, and say so in the docstring: dropping
   them would let case 1's `flush` commit the empty buffer the `O_TRUNC` open left, emptying a list after a
   refused write. **Answer:** as recommended.
4. **The 4096-byte limit.** Recommend: a named constant, not a setting, with the chunk sizes confirmed on
   the first real mount (0011's tests ran through the harness, not FUSE); revisit only if sluice ever
   learns the negotiated limits. **Answer:** as recommended. The rule assumes the default mount limits,
   which sluice doesn't change; the constant is kept beside the mount options, with a test that fails if
   either limit is set (Consequences).
5. **An empty file as the empty string on a `string` leaf**, so `: > leaf` empties it. Recommend: no. 0011
   makes 0 bytes null and one newline the empty string, which keeps the two apart on a read; changing that
   is a change to the serialization, not to these edge cases. **Answer:** as recommended: an empty file
   stays null.
6. **Two write handles on one leaf.** Each has its own buffer and the last `flush` wins, and an `O_TRUNC`
   open doesn't empty another handle's buffer, unlike a regular file. Recommend: leave it for now (editors
   and shell tools use one handle) and say so in Troubleshooting; a record of its own if it matters.
   **Answer:** as recommended: the last to close wins, documented in Troubleshooting.
