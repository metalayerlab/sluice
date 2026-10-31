"""What a middleware sees: the per-call `Context`, its arguments and results, and `MountConfig`.

`MountConfig` is read once at mount time (environment allowlist and secret files) and shared by
every call; nothing here does I/O per call.
"""

from __future__ import annotations

import os
import stat
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from sluice.config import Config, ConfigError

if TYPE_CHECKING:
    from sluice.routing import Resolution, Route

MASK = "********"


class Secret:
    """A secret value. `str()` and `repr()` are masked; `reveal()` gives the value."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return f"Secret({MASK!r})"

    def __str__(self) -> str:
        return MASK

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Secret) and other._value == self._value

    def __hash__(self) -> int:
        return hash(self._value)


@dataclass(frozen=True)
class MountConfig:
    """Mount-time values, in two namespaces: `env` (plain strings) and `secret` (`Secret`s)."""

    env: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    secret: Mapping[str, Secret] = field(default_factory=lambda: MappingProxyType({}))


def load_mount_config(cfg: Config, environ: Mapping[str, str] | None = None) -> MountConfig:
    """Freeze `env.expose` and the files in `secrets.dir` into a `MountConfig`."""
    env = os.environ if environ is None else environ
    missing = [name for name in cfg.env_expose if name not in env]
    if missing:
        raise ConfigError(f"env.expose: not set in the environment: {', '.join(missing)}")
    exposed = {name: env[name] for name in cfg.env_expose}
    return MountConfig(env=MappingProxyType(exposed), secret=_read_secrets(cfg.secrets_dir))


def _read_secrets(directory: Path) -> Mapping[str, Secret]:
    try:
        names = sorted(os.listdir(directory))
    except FileNotFoundError:
        return MappingProxyType({})
    except OSError as e:
        raise ConfigError(f"secrets.dir: cannot read {directory}: {e.strerror}") from e
    secrets: dict[str, Secret] = {}
    for name in names:
        path = directory / name
        # Kubernetes secret volumes hold `..data` and timestamped directories next to the files.
        if name.startswith(".") or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            raise ConfigError(f"secrets.dir: cannot read secret {name!r}: {e}") from e
        secrets[name] = Secret(text.removesuffix("\n").removesuffix("\r"))
    return MappingProxyType(secrets)


@dataclass(frozen=True)
class Caller:
    """The process making the call, as the kernel reports it."""

    uid: int
    gid: int
    pid: int


# Per-op arguments: what `ctx.args` holds. A `pre` hook may replace them.


@dataclass(frozen=True)
class NoArgs:
    pass


@dataclass(frozen=True)
class SetattrArgs:
    mode: int | None = None
    uid: int | None = None
    gid: int | None = None
    size: int | None = None
    atime_ns: int | None = None
    mtime_ns: int | None = None


@dataclass(frozen=True)
class MkdirArgs:
    mode: int


@dataclass(frozen=True)
class SymlinkArgs:
    target: str


@dataclass(frozen=True)
class RenameArgs:
    target: PurePosixPath
    flags: int = 0
    target_real_path: Path | None = None


@dataclass(frozen=True)
class OpenArgs:
    flags: int


@dataclass(frozen=True)
class CreateArgs:
    mode: int
    flags: int


@dataclass(frozen=True)
class ReadArgs:
    offset: int
    size: int


@dataclass(frozen=True)
class WriteArgs:
    offset: int
    data: bytes


@dataclass(frozen=True)
class FsyncArgs:
    datasync: bool


@dataclass(frozen=True)
class AccessArgs:
    mode: int


@dataclass(frozen=True)
class XattrArgs:
    name: str
    value: bytes = b""


# Results: what `post` hooks receive and may replace.


@dataclass(frozen=True)
class Attr:
    """File attributes. sluice sets the inode number itself."""

    st_mode: int
    st_nlink: int = 1
    st_uid: int = 0
    st_gid: int = 0
    st_rdev: int = 0
    st_size: int = 0
    st_blksize: int = 4096
    st_blocks: int = 0
    st_atime_ns: int = 0
    st_mtime_ns: int = 0
    st_ctime_ns: int = 0

    @classmethod
    def from_stat(cls, st: os.stat_result) -> Attr:
        return cls(
            st_mode=st.st_mode,
            st_nlink=st.st_nlink,
            st_uid=st.st_uid,
            st_gid=st.st_gid,
            st_rdev=st.st_rdev,
            st_size=st.st_size,
            st_blksize=st.st_blksize,
            st_blocks=st.st_blocks,
            st_atime_ns=st.st_atime_ns,
            st_mtime_ns=st.st_mtime_ns,
            st_ctime_ns=st.st_ctime_ns,
        )

    @classmethod
    def directory(cls, mode: int = 0o555, mtime_ns: int = 0) -> Attr:
        return cls(
            st_mode=stat.S_IFDIR | mode,
            st_nlink=2,
            st_uid=os.getuid(),
            st_gid=os.getgid(),
            st_atime_ns=mtime_ns,
            st_mtime_ns=mtime_ns,
            st_ctime_ns=mtime_ns,
        )

    @classmethod
    def file(cls, mode: int = 0o444, size: int = 0, mtime_ns: int = 0) -> Attr:
        return cls(
            st_mode=stat.S_IFREG | mode,
            st_uid=os.getuid(),
            st_gid=os.getgid(),
            st_size=size,
            st_atime_ns=mtime_ns,
            st_mtime_ns=mtime_ns,
            st_ctime_ns=mtime_ns,
        )


@dataclass(frozen=True)
class Opened:
    """The result of `open`, `create` and `opendir`: a backend fd or a virtual snapshot."""

    fd: int | None = None
    data: bytes = b""
    direct_io: bool = False
    keep_cache: bool = False


@dataclass(frozen=True)
class DirEntry:
    name: str
    attr: Attr


@dataclass(frozen=True)
class StatFs:
    f_bsize: int = 4096
    f_frsize: int = 4096
    f_blocks: int = 0
    f_bfree: int = 0
    f_bavail: int = 0
    f_files: int = 0
    f_ffree: int = 0
    f_favail: int = 0
    f_namemax: int = 255


class Context:
    """One filesystem call, as middleware sees it.

    The routing fields are read-only; `args` may be replaced in `pre`, `params` holds the merged
    route params after `param()` callbacks, and `state` is free for middleware.
    """

    def __init__(
        self,
        op: str,
        resolution: Resolution,
        *,
        config: MountConfig,
        inode: int = 0,
        args: Any = None,
        caller: Caller | None = None,
        correlation_id: str = "0",
    ) -> None:
        self._op = op
        self._resolution = resolution
        self._config = config
        self._inode = inode
        self._caller = caller or Caller(os.getuid(), os.getgid(), os.getpid())
        self._correlation_id = correlation_id
        self._timestamp = time.time()
        self.args: Any = NoArgs() if args is None else args
        self.params: dict[str, Any] = dict(resolution.params)
        self.state: dict[str, Any] = {}
        self._responded = False
        self._response: Any = None

    @property
    def op(self) -> str:
        return self._op

    @property
    def resolution(self) -> Resolution:
        return self._resolution

    @property
    def path(self) -> PurePosixPath:
        return self._resolution.path

    @property
    def display_path(self) -> str:
        """The virtual path with segments captured by secret params masked."""
        return self._resolution.display_path

    @property
    def inode(self) -> int:
        return self._inode

    @property
    def real_path(self) -> Path | None:
        return self._resolution.real_path

    @property
    def virtual(self) -> bool:
        return self._resolution.virtual

    @property
    def route(self) -> Route:
        return self._resolution.route

    @property
    def caller(self) -> Caller:
        return self._caller

    @property
    def config(self) -> MountConfig:
        return self._config

    @property
    def timestamp(self) -> float:
        return self._timestamp

    @property
    def correlation_id(self) -> str:
        return self._correlation_id

    @property
    def responded(self) -> bool:
        return self._responded

    @property
    def response(self) -> Any:
        return self._response

    def respond(self, result: Any) -> None:
        """From `pre`: skip the rest of `pre` and the operation, and return `result`."""
        self._responded = True
        self._response = result

    def __repr__(self) -> str:
        return f"Context({self._op} {self.display_path} #{self._correlation_id})"
