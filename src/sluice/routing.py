"""The route tree: patterns, matching, backend templates and the resolution walk.

Registration builds mutable `Route`s; `compile_table()` validates them against the mount-time
config and freezes them into a `RouteTable`, which resolves a virtual path to a `Resolution`.
"""

from __future__ import annotations

import errno
import posixpath
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import IntEnum
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal

from sluice.context import MASK, MountConfig
from sluice.errors import FsError, RouteError
from sluice.middleware import Middleware, as_middleware, check_coroutine
from sluice.virtual import VirtualHandler

if TYPE_CHECKING:
    from sluice.context import Context

ParamFunc = Callable[["Context", Any], Awaitable[Any]]

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")


class Kind(IntEnum):
    """Segment kinds, most specific first: the order sibling matching compares them in."""

    LITERAL = 0
    PARAM = 1
    REST = 2


_END = 3  # Ranks after every segment kind: a longer pattern beats its own prefix.


@dataclass(frozen=True)
class Segment:
    kind: Kind
    text: str  # The literal, or the param name

    def __str__(self) -> str:
        if self.kind is Kind.LITERAL:
            return self.text
        return f"{{{self.text}{'*' if self.kind is Kind.REST else ''}}}"


def parse_pattern(pattern: str) -> tuple[Segment, ...]:
    segments: list[Segment] = []
    parts = [p for p in pattern.split("/") if p]
    for i, part in enumerate(parts):
        if part.startswith("{") and part.endswith("}"):
            name = part[1:-1]
            kind = Kind.PARAM
            if name.endswith("*"):
                name, kind = name[:-1], Kind.REST
                if i != len(parts) - 1:
                    raise RouteError(f"{pattern}: {{{name}*}} must be the last segment")
            if not _NAME.fullmatch(name):
                raise RouteError(f"{pattern}: {part!r} is not a valid param")
            segments.append(Segment(kind, name))
        elif "{" in part or "}" in part:
            raise RouteError(f"{pattern}: a param must be a whole segment, got {part!r}")
        elif part in {".", ".."}:
            raise RouteError(f"{pattern}: {part!r} is not a valid segment")
        else:
            segments.append(Segment(Kind.LITERAL, part))
    return tuple(segments)


class Route:
    """A registered route. `route()` adds a child whose pattern is relative to this one."""

    def __init__(
        self,
        pattern: str,
        *,
        parent: Route | None = None,
        backend: str | None = None,
        virtual: bool = False,
        handler: VirtualHandler | None = None,
        middlewares: Sequence[object] = (),
        direct_io: bool = False,
    ) -> None:
        self.pattern = pattern
        self.segments = parse_pattern(pattern)
        self.parent = parent
        self.backend = backend
        self.virtual = virtual
        self.handler = handler
        self.middlewares: list[object] = list(middlewares)
        self.direct_io = direct_io
        self.children: list[Route] = []

    def route(
        self,
        pattern: str,
        backend: str | None = None,
        virtual: bool = False,
        handler: VirtualHandler | None = None,
        middlewares: Sequence[object] = (),
        direct_io: bool = False,
    ) -> Route:
        child = Route(
            pattern,
            parent=self,
            backend=backend,
            virtual=virtual,
            handler=handler,
            middlewares=middlewares,
            direct_io=direct_io,
        )
        self.children.append(child)
        return child

    @property
    def full_pattern(self) -> str:
        parts: list[str] = []
        node: Route | None = self
        while node is not None:
            parts[:0] = [str(s) for s in node.segments]
            node = node.parent
        return "/" + "/".join(parts)

    def __repr__(self) -> str:
        return f"Route({self.full_pattern!r})"


@dataclass(frozen=True)
class ParamSpec:
    name: str
    func: ParamFunc | None
    secret: bool


@dataclass(frozen=True)
class Template:
    """A backend template, with `{env.*}` already substituted."""

    source: str
    parts: tuple[str | Segment, ...]  # Literal text, or a PARAM segment naming the value
    prefix: PurePosixPath  # The literal directory the rendered path must stay under

    @property
    def params(self) -> frozenset[str]:
        return frozenset(p.text for p in self.parts if isinstance(p, Segment))

    def render(self, params: Mapping[str, str]) -> PurePosixPath:
        text = "".join(p if isinstance(p, str) else params[p.text] for p in self.parts)
        return PurePosixPath(text)


def parse_template(source: str, where: str, config: MountConfig) -> Template:
    if not source.startswith("/"):
        raise RouteError(f"{where}: backend {source!r} must be an absolute path")
    parts: list[str | Segment] = []
    literal = ""
    end = 0
    for m in _PLACEHOLDER.finditer(source):
        literal += source[end : m.start()]
        end = m.end()
        name = m.group(1)
        if name.startswith("secret."):
            # A rendered real_path is logged and shown by `sluice resolve`.
            raise RouteError(f"{where}: backend {source!r} can't use {{{name}}}")
        if name.startswith("env."):
            var = name.removeprefix("env.")
            if var not in config.env:
                raise RouteError(
                    f"{where}: backend uses {{{name}}}, but {var} is not in env.expose"
                )
            literal += config.env[var]
            continue
        if not _NAME.fullmatch(name):
            raise RouteError(f"{where}: backend {source!r} has an invalid placeholder {{{name}}}")
        parts.append(literal)
        literal = ""
        parts.append(Segment(Kind.PARAM, name))
    literal += source[end:]
    if "{" in literal or "}" in literal:
        raise RouteError(f"{where}: backend {source!r} has an unbalanced brace")
    parts.append(literal)
    head = parts[0] if isinstance(parts[0], str) else ""
    prefix = PurePosixPath(head if len(parts) == 1 else posixpath.dirname(head) or "/")
    return Template(
        source, tuple(p for p in parts if p != ""), PurePosixPath(posixpath.normpath(prefix))
    )


@dataclass(frozen=True)
class Node:
    """A validated, frozen route."""

    route: Route
    segments: tuple[Segment, ...]
    children: tuple[Node, ...]
    middlewares: tuple[Middleware, ...]
    template: Template | None
    virtual: bool
    handler: VirtualHandler | None
    direct_io: bool

    @property
    def rank(self) -> tuple[int, ...]:
        return (*(int(s.kind) for s in self.segments), _END)


ResolutionKind = Literal["backed", "virtual", "synthetic"]


@dataclass(frozen=True)
class Resolution:
    """Where a virtual path goes, and what runs on the way. Built before any middleware."""

    path: PurePosixPath
    kind: ResolutionKind
    chain: tuple[Node, ...]  # Root first, the matched route last
    params: Mapping[str, str]
    middlewares: tuple[Middleware, ...]
    param_specs: tuple[ParamSpec, ...]  # The `param()` callbacks to run, in registration order
    tail: tuple[str, ...]  # Path segments below the matched route
    children: tuple[str, ...]  # Literal names of child routes listed in this directory
    display_path: str
    real_path: Path | None = None
    backend_root: Path | None = None  # The rendered backend, before the tail
    # The template's literal prefix: the only part of real_path the daemon follows symlinks in.
    backend_prefix: Path | None = None
    owner: Route | None = None  # The route whose backend this is
    handler: VirtualHandler | None = None
    direct_io: bool = False

    @property
    def route(self) -> Route:
        return self.chain[-1].route

    @property
    def virtual(self) -> bool:
        return self.kind == "virtual"


@dataclass(frozen=True)
class WalkedRoute:
    depth: int
    node: Node
    state: str  # The backend state: own, inherited from, virtual or grouping
    middlewares: tuple[Middleware, ...]  # The full chain, global first


_Match = tuple[int, dict[str, str], list[tuple[str, int, int]]]


def _match(segments: Sequence[Segment], path: Sequence[str], pos: int) -> _Match | bool:
    """A full match (consumed, captures, spans), True if `path` ends inside it, False if not."""
    captures: dict[str, str] = {}
    spans: list[tuple[str, int, int]] = []
    i = pos
    for seg in segments:
        if seg.kind is Kind.REST:
            captures[seg.text] = "/".join(path[i:])
            spans.append((seg.text, i, len(path)))
            i = len(path)
            break
        if i == len(path):
            return True
        if seg.kind is Kind.LITERAL:
            if path[i] != seg.text:
                return False
        else:
            captures[seg.text] = path[i]
            spans.append((seg.text, i, i + 1))
        i += 1
    return i - pos, captures, spans


class RouteTable:
    """The frozen route tree."""

    def __init__(
        self,
        root: Node,
        params: Sequence[ParamSpec],
        config: MountConfig,
    ) -> None:
        self.root = root
        self.params = tuple(params)
        self.config = config
        self.secret_params = frozenset(p.name for p in params if p.secret)

    def resolve(self, path: PurePosixPath | str) -> Resolution | None:
        """Resolve a virtual path. None if no route covers it."""
        # Not normalised: the kernel never sends "." or "..", and a ".." that does arrive (from
        # `sluice resolve`) must meet the escape check below rather than be folded away.
        path = PurePosixPath("/" + str(path).lstrip("/"))
        segs = path.parts[1:]
        node, pos = self.root, 0
        chain = [node]
        params: dict[str, str] = {}
        spans: list[tuple[str, int, int]] = []
        owner: tuple[Route, Template, int] | None = None
        while True:
            best: tuple[Node, _Match] | None = None
            for child in node.children:
                m = _match(child.segments, segs, pos)
                if isinstance(m, tuple) and (best is None or child.rank < best[0].rank):
                    best = (child, m)
            if best is None:
                break
            node, (consumed, captures, found) = best
            pos += consumed
            chain.append(node)
            params.update(captures)
            spans += found
            if node.virtual:
                owner = None
            elif node.template is not None:
                owner = (node.route, node.template, pos)

        tail = segs[pos:]
        listed: list[str] = []
        partial = False
        for child in node.children:
            if len(child.segments) > len(tail) and _match(child.segments, tail, 0) is True:
                partial = True
                nxt = child.segments[len(tail)]
                if nxt.kind is Kind.LITERAL and nxt.text not in listed:
                    listed.append(nxt.text)

        masked = list(segs)
        for name, start, end in spans:
            if name in self.secret_params:
                masked[start:end] = [MASK] * (end - start)
        base = Resolution(
            path=path,
            kind="synthetic",
            chain=tuple(chain),
            params=MappingProxyType(params),
            middlewares=tuple(mw for n in chain for mw in n.middlewares),
            param_specs=tuple(p for p in self.params if p.func and p.name in params),
            tail=tuple(tail),
            children=tuple(listed),
            display_path="/" + "/".join(masked),
            direct_io=any(n.direct_io for n in chain),
        )
        if node.virtual:
            return replace(base, kind="virtual", handler=node.handler)
        if owner is not None:
            route, template, end = owner
            root = template.render(params)
            real = PurePosixPath(posixpath.normpath(PurePosixPath(root, *segs[end:])))
            if not real.is_relative_to(template.prefix):
                raise FsError(errno.EACCES, "path escapes its backend")
            return replace(
                base,
                kind="backed",
                real_path=Path(real),
                backend_root=Path(root),
                backend_prefix=Path(template.prefix),
                owner=route,
            )
        if not tail or partial:
            return base
        return None

    def walk(self) -> list[WalkedRoute]:
        """Every route, depth first, for `sluice routes`."""
        out: list[WalkedRoute] = []

        def visit(
            node: Node, depth: int, inherited: str | None, chain: tuple[Middleware, ...]
        ) -> None:
            chain = (*chain, *node.middlewares)
            if node.virtual:
                state, inherited = "virtual", None
            elif node.template is not None:
                state = f"backend {node.template.source}"
                inherited = f"inherits {node.template.source} from {node.route.full_pattern}"
            elif inherited is not None:
                state = inherited
            else:
                state = "grouping"
            out.append(WalkedRoute(depth, node, state, chain))
            for child in node.children:
                visit(child, depth + 1, inherited, chain)

        for child in self.root.children:
            visit(child, 0, None, self.root.middlewares)
        return out


def compile_table(
    root: Route,
    params: Sequence[ParamSpec],
    config: MountConfig,
) -> RouteTable:
    """Validate the tree under `root` and freeze it. Raises `RouteError`."""
    secret = {p.name for p in params if p.secret}
    for spec in params:
        if spec.func is not None:
            check_coroutine(spec.func, f"param({spec.name!r})")

    def build(route: Route, captured: frozenset[str], backed: bool) -> Node:
        where = route.full_pattern
        own = [s.text for s in route.segments if s.kind is not Kind.LITERAL]
        for name in own:
            if name in captured or own.count(name) > 1:
                raise RouteError(f"{where}: param {{{name}}} is captured more than once")
        captured = captured | set(own)
        if route.virtual and route.handler is None:
            raise RouteError(f"{where}: virtual=True needs a handler")
        if route.virtual and route.backend is not None:
            raise RouteError(f"{where}: a virtual route can't have a backend")
        if route.handler is not None and not route.virtual:
            raise RouteError(f"{where}: a handler needs virtual=True")
        if route.handler is not None and not isinstance(route.handler, VirtualHandler):
            raise RouteError(f"{where}: handler must be a VirtualHandler")
        template = None
        if route.backend is not None:
            template = parse_template(route.backend, where, config)
            for name in sorted(template.params):
                if name not in captured:
                    raise RouteError(f"{where}: backend uses {{{name}}}, which no route captures")
                if name in secret:
                    raise RouteError(f"{where}: backend can't use the secret param {{{name}}}")
        elif backed and not route.virtual:
            # Below an inherited backend, this route's segments are appended to the real path.
            for name in own:
                if name in secret:
                    raise RouteError(f"{where}: backend can't use the secret param {{{name}}}")
        backed = not route.virtual and (backed or template is not None)
        children = tuple(build(c, captured, backed) for c in route.children)
        shapes: dict[tuple[tuple[Kind, str], ...], Route] = {}
        for child in children:
            shape = tuple(
                (s.kind, s.text if s.kind is Kind.LITERAL else "") for s in child.segments
            )
            if shape in shapes:
                other = shapes[shape].full_pattern
                raise RouteError(f"{child.route.full_pattern}: same shape as {other}")
            shapes[shape] = child.route
        return Node(
            route=route,
            segments=route.segments,
            children=children,
            middlewares=tuple(as_middleware(m, where) for m in route.middlewares),
            template=template,
            virtual=route.virtual,
            handler=route.handler,
            direct_io=route.direct_io,
        )

    return RouteTable(build(root, frozenset(), False), params, config)
