# Release Process

<!-- Template-owned page: it changes in project-template and arrives with `copier update`.
     AI assistants: AGENTS.md already holds the rules you need (PR titles, never edit the version or
     CHANGELOG). Read further only to explain releases to someone, or to change release configuration. -->

Releases are automated by [release-please](https://github.com/googleapis/release-please). Nobody edits a version number or the changelog by hand, and nobody pushes tags.

## Versioning

- [Semantic Versioning](https://semver.org/), with tags `vX.Y.Z`. Before 1.0, a breaking change bumps the minor version and a feature bumps the patch.
- The version is derived from Conventional Commit messages on `main`. PRs are squash-merged, so it's the **PR title** that counts:

| PR title | Bump | CHANGELOG section |
|---|---|---|
| `feat: ...` | minor (patch before 1.0) | ✨ Features |
| `fix: ...` | patch | 🐛 Bug Fixes |
| `perf: ...` | patch | ⚡ Performance |
| `feat!: ...`, or `BREAKING CHANGE:` in the body | major (minor before 1.0) | ⚠ Breaking changes |
| `refactor:`, `docs:`, `revert:` | patch | their own sections |
| `build:`, `ci:`, `test:`, `chore:` | none on their own | hidden |

- The *PR title* check enforces the format. A wrong type means a wrong version, so reviewers check it.
- The version lives in `pyproject.toml` and `.release-please-manifest.json`; `sluice.__version__` reads the installed metadata. CI fails if the two files disagree.

## How a release happens

1. Merge PRs to `main` as usual.
2. On every push to `main`, release-please updates a single open **Release PR** (`chore: release X.Y.Z`). It bumps the version, writes the new `CHANGELOG.md` entry from the commit titles. A follow-up job refreshes `uv.lock` on the same PR.
3. Review the Release PR like any other: is the version right, and does the changelog read well? Fix a wording problem by editing the PR *description* under the version heading (release-please keeps that), or fix the original commit's PR title and rerun the *Release* workflow.
4. The Release PR runs the wiki check in **strict** mode: any leftover `<!-- guidance -->` skeleton text in `docs/wiki/` blocks the release.
5. Merge it. release-please tags `vX.Y.Z` and publishes the GitHub Release with the changelog entry, and the release image is built and pushed: `ghcr.io/metalayerlab/sluice:X.Y.Z`, `:X.Y`, `:X` (from 1.0), and `:latest`.

Other images: `:edge` and `:main-<sha>` are built from every push to `main`, and `:<branch>` and `:<branch>-<sha>` from `dev/**` and `feat/**` branches. Branch images are deleted after 7 days.

## Pre-releases and hotfixes

- **Pre-release:** run the *Release* workflow after adding `"prerelease": true` and `"prerelease-type": "rc"` for the package in `release-please-config.json`, in a PR. Remove them again for the final release.
- **First release** is `0.1.0`, and `feat:` stays a patch bump until 1.0. Leave 0.x with the force below.
- **Force a version:** a commit with `Release-As: 1.0.0` in its body (an empty `chore:` commit is fine) makes the next Release PR use that version.
- **Hotfix:** a `fix:` PR to `main` and a release as usual. There are no long-lived release branches.
- **Rebuild an old image:** run the *Docker image* workflow manually with `tag: vX.Y.Z`. It builds that tag with today's Docker files and never moves `:latest`, unless you tick it.

Setup note for maintainers: Release PRs opened with the default `GITHUB_TOKEN` don't trigger CI. The organisation's release GitHub App (`RELEASE_APP_CLIENT_ID` variable, `RELEASE_APP_PRIVATE_KEY` secret) fixes that; see project-template's Release-Process page.
