# Human review checklist

For every PR, and above all for AI-generated ones. Generated code is fluent and plausible-looking, which is
exactly why it needs a careful reader. Approve only when you could have written the change yourself and
would defend it.

## Before reading the code

- [ ] The PR says what it does, why, and how much was AI-generated.
- [ ] It does one thing. If it does several, ask for a split.
- [ ] A non-trivial change has an approved `docs/design/` record, and the PR matches it.

## Correctness

- [ ] I read every changed line, not only the summary or the tests.
- [ ] It does what the description says, and nothing else. No unrequested refactors, "improvements" or
      deleted code.
- [ ] Edge cases are handled: empty input, missing config, timeouts, partial failure, retries, concurrency.
- [ ] Errors are handled where they can be handled, and surface clearly otherwise. Nothing is swallowed
      by a broad `except`.
- [ ] No invented APIs: every library function or option used exists in the version the lock file pins.

## Tests

- [ ] Each behaviour change has a test that would fail without it. Ask for proof when it isn't obvious.
- [ ] Tests assert outcomes. They don't merely run the code or mirror its implementation.
- [ ] Nothing was skipped, xfailed, loosened or deleted to make the suite pass.

## Security and supply chain

- [ ] No secrets, tokens, real hostnames, IP addresses or personal data, including in tests, fixtures,
      docs and logs.
- [ ] Input from users, files and the network is validated. No shell or SQL built from strings, and no
      `eval`, `pickle` or unsafe YAML loading of untrusted data.
- [ ] New dependencies are justified, exist on the index under exactly that name (typosquatting), are
      maintained, and have a compatible licence.
- [ ] Hardening is intact: Dockerfile user, k8s `securityContext`, systemd sandbox, NetworkPolicy.
- [ ] Code copied from elsewhere is attributed and licence-compatible.

## Docs and release

- [ ] The wiki pages in AGENTS.md's "Docs map" are updated, and they're accurate rather than merely present.
- [ ] Generated wiki sections were regenerated, not hand-edited.
- [ ] The PR title is a correct Conventional Commit. `feat` vs `fix`, and `!` for breaking, decide the
      version bump.
- [ ] A `no-docs-needed` label, if present, is justified: nothing user-visible changed.

## AI instruction files

- [ ] Changes to AGENTS.md are terse rules; the reasons went to the wiki. CI enforces the size budget, so a
      change that only fits by squeezing out other rules needs a second look.
- [ ] New large or generated paths (fixtures, snapshots, build output) are in AGENTS.md "Don't read" and
      denied in `.claude/settings.json`.
- [ ] New design records are listed in `docs/design/README.md`.

## Before approving

- [ ] CI is green, and nothing in `.github/workflows/` or `scripts/` was changed to get there.
- [ ] I'd be comfortable debugging this at 3am from the code and the wiki alone.
