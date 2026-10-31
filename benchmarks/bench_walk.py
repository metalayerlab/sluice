"""Benchmark the O_NOFOLLOW backend walk (#24): backend-level and through a real mount.

Compares two source trees: the new one walks each directory below the route's literal
prefix with O_NOFOLLOW (`_beneath` in src/sluice/backend.py); the old one calls
`os.lstat`/`os.open`/`os.scandir` on the real path. It measures, at 1, 5 and 15
directory levels below the prefix:

- the backend alone (no FUSE): getattr, open+read and readdir, in µs per call;
- through a mount, with `mount.cache_ms` 0 and 1000: stat, open+read, listdir and an
  `os.walk` + `lstat` of a tree, in µs per op.

Results and how to read them: issue #28 and the wiki's Architecture-Internals page.

Run it on a tmpfs work dir, with FUSE available (`/dev/fuse`, `fusermount3`):

    uv sync --extra fuse
    git worktree add <old-checkout> 3327997^
    uv run benchmarks/bench_walk.py src <old-checkout>/src <workdir>
"""

import importlib.util
import os
import signal
import statistics
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

DEPTHS = (1, 5, 15)
FANOUT = 20  # files per directory level for the tree walk

USAGE = "usage: bench_walk.py <src-dir-of-new> <src-dir-of-old> <workdir>"


def timeit(fn: Callable[[], object], n: int, reps: int = 7) -> float:
    best = []
    for _ in range(reps):
        t = time.perf_counter_ns()
        for _ in range(n):
            fn()
        best.append((time.perf_counter_ns() - t) / n / 1000)
    return statistics.median(best)


def build(backing: Path) -> Path:
    prefix = backing / "u"
    for d in DEPTHS:
        p = prefix / f"d{d}"
        for _ in range(d - 1):
            p = p / "a"
        p.mkdir(parents=True, exist_ok=True)
        (p / "f").write_bytes(b"x" * 4096)
        # tree for find-like walk: a chain of d dirs, FANOUT files each
        q = prefix / f"t{d}"
        for _ in range(d):
            q.mkdir(parents=True, exist_ok=True)
            for j in range(FANOUT):
                (q / f"f{j}").write_bytes(b"")
            q = q / "a"
    return prefix


def target(root: Path, d: int) -> Path:
    p = root / f"d{d}"
    for _ in range(d - 1):
        p = p / "a"
    return p


def load_backend(src: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, src / "sluice" / "backend.py")
    if spec is None or spec.loader is None:
        raise SystemExit(f"no sluice/backend.py under {src}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def backend_level(new_src: Path, prefix: Path) -> list[tuple[int, str, float, float]]:
    sys.path.insert(0, str(new_src))
    new = load_backend(new_src, "newbackend")
    rows = []
    for d in DEPTHS:
        f = target(prefix, d) / "f"
        dirp = target(prefix, d)

        def old_stat(f: Path = f) -> None:
            os.lstat(f)

        def new_stat(f: Path = f) -> None:
            new._at(new._lstat)(prefix, f)

        def old_open(f: Path = f) -> None:
            fd = os.open(f, os.O_RDONLY)
            os.pread(fd, 4096, 0)
            os.close(fd)

        def new_open(f: Path = f) -> None:
            fd = new._at(new._open)(prefix, f, os.O_RDONLY)
            os.pread(fd, 4096, 0)
            os.close(fd)

        def old_ls(dirp: Path = dirp) -> None:
            with os.scandir(dirp) as it:
                for e in it:
                    e.stat(follow_symlinks=False)

        def new_ls(dirp: Path = dirp) -> None:
            new._at(new._scandir)(prefix, dirp)

        for op, o, n in (
            ("getattr", old_stat, new_stat),
            ("open+read", old_open, new_open),
            ("readdir", old_ls, new_ls),
        ):
            rows.append((d, op, timeit(o, 20000), timeit(n, 20000)))
    return rows


APP = """
from sluice import Sluice
app = Sluice()
app.route("/b/{user}", backend="BACKING/{user}")
"""


def mount(
    src: Path, backing: Path, work: Path, cache_ms: int
) -> tuple[subprocess.Popen[bytes], Path]:
    mnt = work / "mnt"
    mnt.mkdir(exist_ok=True)
    (work / "benchapp.py").write_text(APP.replace("BACKING", str(backing)))
    (work / "cfg.yaml").write_text(f"mount:\n  cache_ms: {cache_ms}\nlog_level: WARNING\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("SLUICE_")}
    env |= {
        "SLUICE_CONFIG": str(work / "cfg.yaml"),
        "SLUICE_APP_MODULE": "benchapp:app",
        "SLUICE_APP_DIR": str(work),
        "SLUICE_MOUNT_POINT": str(mnt),
        "PYTHONPATH": str(src),
    }
    with (work / "log").open("wb") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "sluice", "mount"],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    t = time.monotonic()
    while not os.path.ismount(mnt):
        if proc.poll() is not None or time.monotonic() - t > 20:
            raise SystemExit((work / "log").read_text())
        time.sleep(0.05)
    return proc, mnt


def mount_level(
    src: Path, backing: Path, work: Path, cache_ms: int
) -> list[tuple[int, str, float]]:
    proc, mnt = mount(src, backing, work, cache_ms)
    root = mnt / "b" / "u"
    rows = []
    try:
        for d in DEPTHS:
            f = target(root, d) / "f"
            dirp = target(root, d)
            tree = root / f"t{d}"

            def stat(f: Path = f) -> None:
                os.lstat(f)

            def rd(f: Path = f) -> None:
                with open(f, "rb") as fh:
                    fh.read()

            def ls(dirp: Path = dirp) -> None:
                os.listdir(dirp)

            def walk(tree: Path = tree) -> None:
                for top, _dirs, files in os.walk(tree):
                    for x in files:
                        os.lstat(os.path.join(top, x))

            ops: tuple[tuple[str, Callable[[], None], int], ...] = (
                ("getattr", stat, 2000),
                ("open+read", rd, 1000),
                ("readdir", ls, 2000),
                (f"walk({FANOUT}/lvl)", walk, 30),
            )
            for op, fn, n in ops:
                fn()
                rows.append((d, op, timeit(fn, n, reps=5)))
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=20)
    return rows


def main() -> None:
    if len(sys.argv) != 4:
        raise SystemExit(USAGE)
    new_src, old_src, work = (Path(a).resolve() for a in sys.argv[1:4])
    backing = work / "backing"
    prefix = build(backing)
    print("## backend level (no FUSE), us/op: before #24 | after | delta")
    for d, op, o, n in backend_level(new_src, prefix):
        print(f"depth {d:2} {op:10} {o:8.2f} {n:8.2f} {n - o:+8.2f}")
    for cache in (0, 1000):
        print(f"## through the mount, cache_ms={cache}, us/op: before | after | ratio")
        old = mount_level(old_src, backing, work, cache)
        new = mount_level(new_src, backing, work, cache)
        for (d, op, o), (_, _, n) in zip(old, new, strict=True):
            print(f"depth {d:2} {op:14} {o:10.1f} {n:10.1f} {n / o:6.2f}x")


if __name__ == "__main__":
    main()
