"""Virtual Organisations adapter: one org record mounted read-only (design 0011, step 3).

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

Read-only for now: `writable` is `False` everywhere, and version 1 has no reference leaves.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, get_args

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
        parts = [_unescape(p) for p in pointer.split("/")[1:]]
        if len(parts) != 2 or parts[0] != "seed":
            return None
        declared = doc["template"]["seed"].get(parts[1], {}).get("type")
        # validate_org has checked it is a v1 type; anything else falls back to the value's own
        return declared if declared in TYPES else None

    def writable(self, pointer: str, doc: dict[str, Any]) -> bool:
        return False  # Writes to seed values come in step 5

    def is_reference(self, pointer: str, doc: dict[str, Any]) -> bool:
        return False  # Version 1 has no secret references


def org_projection(path: str | Path, *, param: str = "rest") -> Projection:
    """The read-only projection of the org record at `path`, for a `{rest*}` route.

    Raises `ModelNotInstalled` without the package, the model's `LoadError` for a file that
    doesn't load, and `DocumentError` for one that isn't a valid version 1 org record.
    """
    return Projection(DocumentStore(path, OrgModel()), param=param)
