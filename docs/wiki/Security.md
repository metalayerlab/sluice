# Security

## Threat model

sluice is a lab project, and phase 1 ships the hook points for policy, not a policy: `auth()` allows everything unless you inject a `verify()`. Read this before you point it at data that matters.

**What it exposes.** One FUSE mount, and nothing on the network: no ports, no HTTP. By default only the user who mounted it can enter the mount (the kernel enforces that). With `mount.allow_other`, every local user can.

**What it trusts.**

- **The app.** `app.module` is imported and runs with sluice's privileges. Whoever can change the module, or `app.dir`, controls what the mount does and can read every secret.
- **The kernel**, for the caller's uid, gid and pid (`ctx.caller`). That is the only identity anyone vouches for. A `{user}` in a path is just a string the caller typed.
- **The backends.** Symlinks inside a backing directory that point outside it are followed by the caller's kernel, as on any filesystem. A backend is trusted not to contain hostile links.

**Confused deputy, by design.** sluice reads and writes the backing store with its own uid for every caller. Files a caller creates are owned by sluice's user. With `allow_other`, your middleware is the only thing between other local users and everything sluice can reach. The kernel still checks the mode bits sluice reports (`default_permissions`), but those are the backing files' own.

**Tokens in paths.** A credential in a path (`/fuse/alice/T0k/...`) is visible to anyone who can see the caller's command line, shell history, `/proc/<pid>/cwd` or open files. sluice masks it only in its own output: mark the param `secret=True`, and `ctx.display_path`, the access log, error logs and `sluice resolve` show `********`. Synthetic directories never list param values. libfuse's tracing (`mount.debug`) prints raw paths and is the one exception.

**What an attacker gains**, if they control the app or the sluice process: everything its uid can read and write in every backend, and every secret in `secrets.dir`. If they can only call the mount: whatever your middleware lets through.

**Out of scope in phase 1:** real authentication, rate limiting, auditing beyond the access log, and protection against a malicious backend.

## Secrets

- Secrets are read from files only (`SLUICE_<KEY>_FILE`), never from the config file. See [In-Depth Configuration](In-Depth-Configuration#secrets).
- Values are never logged. Logs and `sluice config` show where a value came from, never the value itself.
- CI scans every commit with gitleaks, and secrets in history fail the build.

sluice's own settings hold no secrets. Your app's secrets, if it has any, are files in `secrets.dir`:

- Each file is one `ctx.config.secret[<name>]`, a `Secret` that prints as `********`. Only the names are logged, once at startup. They are never shown by `sluice config`, and are refused in backend templates (a rendered real path is logged).
- What a secret grants is up to your app: a database password used by a config template, a key `verify()` checks tokens against.
- **Rotating:** secrets are read once at mount time. Replace the file, then restart the mount (in Kubernetes, roll the pod). Open files on the old mount break at the restart.

## Hardening

- **Container:** runs as uid 1000, but a FUSE mount needs `/dev/fuse` and, in most runtimes, `CAP_SYS_ADMIN` (or a FUSE device plugin) and an unconfined AppArmor profile. Grant only those: keep the root filesystem read-only and drop every other capability. See [Deployment](Deployment).
- **Supply chain:** dependencies are pinned in the lock file and updated weekly by Dependabot. GitHub Actions are pinned to commit SHAs.

- **Paths:** the router matches whole segments only and never interprets them. A rendered real path is normalised and must stay under its backend template's literal prefix: `..` in a captured value can't escape (`EACCES`). Paths no route covers don't exist.
- **Routes are validated before mounting:** params in templates must be captured, `{env.*}` must be allowlisted, `{secret.*}` and secret params are refused in templates and in routes that inherit a backend (their segments become the real path, which is logged), and every hook must be `async`.
- **Environment:** only variables named in `env.expose` reach the app, and `SLUICE_*` names are refused.
- **Authorisation** is middleware: `auth(verify)` with an injected `async verify(user, token, real_path, op)`, or your own `pre` hooks. `rename` is authorised against both its source and target paths: every `pre` hook on the target's chain runs with the target's context, including a middleware the source chain also has. With `mount.cache_ms` above 0, cached lookups and attributes skip middleware.
- **Failure is closed:** an exception in a `post` hook returns `EIO`, never the unmodified result. Any unexpected exception is `EIO`, never a crash of the mount.
- **Not provided:** rate limiting, or protection against a caller who floods the mount. One event loop serves everyone, so a slow middleware slows every caller.

## Reporting a vulnerability

Report privately through [GitHub security advisories](https://github.com/metalayerlab/sluice/security/advisories/new), never in a public issue.
