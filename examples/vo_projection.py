"""Virtual Organisations adapter: one org record mounted, its seed values writable (design 0011).

An example, not part of the sluice package: copy it beside your app module, with
`yaml_projection.py` and `auth.py`. It fills the generic projector's `Model` from the
`virtual_organisations.model` package, which is private: install it into the app's
environment. Without it this module still imports, and `org_projection` raises
`ModelNotInstalled`.

    from auth import auth
    from vo_projection import org_projection

    app.route(
        "/orgs/example-trust/{rest*}",
        virtual=True,
        handler=org_projection("/srv/orgs/example-trust.yaml"),
        middlewares=[auth()],
    )

One org record is mounted, never a template and seed pair. It loads through `load_org`, must
pass `validate_org` (each `Finding` becomes its code, JSON Pointer and message), and must be
version 1: version 2 adds secret references (`pass-path`) and links to other orgs (`org`),
which need a design record of their own, so another version is refused at mount, and a
replacement of another version is refused at reload. A seed value is typed by the template
copy's `seed` declaration for its key; everything else (the template copy, results, the
leaves inside a `mapping` value) by its current value.

Writes go to seed values only (design 0011, "Writes, in two phases"): `seed/<key>` for a key the
record's `seed` holds, cast by the type the template copy declares for it, and the leaves inside
a `mapping`-typed value, each keeping its current type. A `list` value is replaced whole, each
item keeping the type of the item it replaces (design 0012); `: > seed/<key>` commits an empty
file, which empties a list and is refused on any other declared type. Every write is checked by
`validate_org` on the whole changed record. Everything else is read-only (`EACCES` at `open`):
`kind`, `version`, `org`, `name` and `status`; `results`, which are the engine's; and the
template copy, a verbatim copy of its source template, dependencies (`depends_on`) included. A
seed key left to its default is absent, and adding it changes the record's shape (`EPERM`). Seed
writes are allowed whatever `status` and `results` say, until the engine's own record rules on
when a seed value may change after a run. Version 1 has no reference leaves.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast, get_args

from yaml_projection import DocumentStore, Finding, Projection, Type

try:
    from virtual_organisations import model as vo
except ImportError:  # The private package isn't installed: org_projection says so
    vo = None

VERSION = 1  # The only version this adapter mounts (design 0011, "Version 1 only")
TYPES: frozenset[str] = frozenset(get_args(Type.__value__))


class ModelNotInstalled(ImportError):
    """`virtual_organisations.model` can't be imported."""

    def __init__(self) -> None:
        super().__init__(
            "vo_projection needs the virtual_organisations package: install the "
            "virtual-organisations project into this environment (it is private)"
        )


def _unescape(part: str) -> str:
    return part.replace("~1", "/").replace("~0", "~")


def _parts(pointer: str) -> list[str]:
    return [_unescape(p) for p in pointer.split("/")[1:]]


def _declared(key: str, doc: dict[str, Any]) -> Type | None:
    """The type the template copy's `seed` section declares for `key`, if a v1 one."""
    node: Any = doc
    for part in ("template", "seed", key, "type"):
        node = node.get(part) if isinstance(node, dict) else None
    # validate_org has checked it is a v1 type; anything else falls back to the value's own
    return cast(Type, node) if isinstance(node, str) and node in TYPES else None


def writable(pointer: str, doc: dict[str, Any]) -> bool:
    """Whether the leaf at `pointer` may be written: a seed value the record holds, or a leaf
    inside a `mapping`-typed one. Needs only the pointer and the record, not the model.

    Never true for a directory (a `mapping` value itself), for a seed key left to its default
    (adding it is a shape change), or for anything outside `seed`.
    """
    parts = _parts(pointer)
    seed = doc.get("seed")
    if len(parts) < 2 or parts[0] != "seed" or not isinstance(seed, dict) or parts[1] not in seed:
        return False
    if len(parts) > 2 and _declared(parts[1], doc) != "mapping":
        return False  # A list's items, say: a list is one leaf, replaced whole
    node: Any = seed
    for part in parts[1:]:
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return not isinstance(node, dict)


class OrgModel:
    """The projector's `Model` for a version 1 org record."""

    def __init__(self) -> None:
        if vo is None:
            raise ModelNotInstalled
        self._vo: Any = vo

    def load(self, path: Path) -> dict[str, Any]:
        doc: dict[str, Any] = self._vo.load_org(path)  # LoadError names file and line only
        return doc

    def dump(self, doc: dict[str, Any]) -> str:
        text: str = self._vo.dump(doc)
        return text

    def validate(self, doc: dict[str, Any]) -> list[Finding]:
        version = doc.get("version")
        if type(version) is not int or version != VERSION:  # `True == 1`, so the type too
            return [("version", "/version", f"only version {VERSION} org records can be mounted")]
        return [(f.code, f.path, f.message) for f in self._vo.validate_org(doc)]

    def type_of(self, pointer: str, doc: dict[str, Any]) -> Type | None:
        """A seed value's declared type; `None` (the current value's type) everywhere else."""
        parts = _parts(pointer)
        if len(parts) != 2 or parts[0] != "seed":
            return None
        return _declared(parts[1], doc)

    def writable(self, pointer: str, doc: dict[str, Any]) -> bool:
        return writable(pointer, doc)

    def is_reference(self, pointer: str, doc: dict[str, Any]) -> bool:
        return False  # Version 1 has no secret references


def org_projection(path: str | Path, *, param: str = "rest") -> Projection:
    """The projection of the org record at `path`, for a `{rest*}` route.

    Raises `ModelNotInstalled` without the package, the model's `LoadError` for a file that
    doesn't load, and `DocumentError` for one that isn't a valid version 1 org record.
    """
    return Projection(DocumentStore(path, OrgModel()), param=param)
