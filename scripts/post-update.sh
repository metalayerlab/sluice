#!/bin/sh
# Template-owned: template-base. After `copier update` (template-update.yml runs it, or
# you do): base's own migrations, then every scripts/post-update.d/*.sh, in name order
# (a lock refresh, say). Templates number theirs 00-89; a project adds its own as
# 9N-project-<what>.sh, unmarked, so they run last (decision 0008). Every step must be
# idempotent and cheap when nothing changed, and never push, tag or call GitHub: the
# update PR carries what it changes. The first failing step stops the rest.
# A step may assume sh, python3, git and uv, and in CI the runner image's tools
# (unpinned, and maybe missing locally). A missing tool fails, naming it.
set -eu
cd "$(dirname "$0")/.."

# Decision 0006: .gitleaks.toml became the project's and extends .gitleaks.base.toml. A file
# from before it says `useDefault = true`: point it at base instead, and drop the lock-file
# entry base now has, if the allow-list keeps another entry. Nothing else changes; idempotent.
python3 - << 'EOF'
import re
import tomllib
from pathlib import Path

path, base = Path(".gitleaks.toml"), ".gitleaks.base.toml"
CHECKS = ("paths", "regexes", "stopwords", "commits")
LOCKS = re.compile(r"(?m)^paths = \[\n(  # lock files: integrity hashes look like secrets\n)?"
                   r"  '''\(\^\|/\)\(uv\\\.lock\|pnpm-lock\\\.yaml\|package-lock\\\.json\)\$''',\n\]\n")


def load(text):
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None


def allowlists(data):
    lists = data.get("allowlists", [])
    return lists + [data["allowlist"]] if "allowlist" in data else lists


text = path.read_text(encoding="utf-8") if path.is_file() else ""
data = load(text)
extend = (data or {}).get("extend", {})
if data is not None and extend.get("useDefault") is True and "path" not in extend:
    lines = text.splitlines(keepends=True)
    table = None
    for i, line in enumerate(lines):
        if re.match(r"\s*\[", line):
            table = line.strip()
        elif table == "[extend]" and re.match(r"\s*useDefault\s*=\s*true\s*(#.*)?$", line):
            lines[i] = f'path = "{base}"\n'
            break
    new = "".join(lines)
    if (load(new) or {}).get("extend", {}).get("path") == base:
        deduped = LOCKS.sub("", new, count=1)
        moved = load(deduped)
        if moved is not None and all(any(a.get(k) for k in CHECKS) for a in allowlists(moved)):
            new = deduped
        path.write_text(new, encoding="utf-8")
        print(f"post-update: {path} now extends {base} (template-base decision 0006)")
EOF

for step in scripts/post-update.d/*.sh; do
    [ -e "$step" ] || continue
    sh "$step"
done
echo "post-update done"
