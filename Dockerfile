# sluice: multi-stage build. Template-owned skeleton; project-specific
# stages (extra binaries, assets) go between the builder and the runtime stage.

# ============================================================
# Stage 1: builder
# ============================================================
FROM python:3.13-slim AS builder

# The `build:` packages in contrib/system-packages.txt (project-owned): compilers
# and headers for dependencies that build from source. None listed: nothing runs
# hadolint ignore=DL3008,SC2046
RUN --mount=type=bind,source=contrib/system-packages.txt,target=/tmp/system-packages.txt \
    set -- $(sed -n 's/^build://p' /tmp/system-packages.txt); \
    if [ $# -gt 0 ]; then \
        apt-get update \
        && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "$@" \
        && rm -rf /var/lib/apt/lists/*; \
    fi

# Dependencies, then the package, into /opt/venv

COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /bin/uv

# From the lock file, compiled, without dev dependencies, with the base image's
# Python (never a downloaded one)
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /build

# Dependencies first, so a source change doesn't reinstall them
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project --extra fuse

COPY src/ ./src/
RUN uv sync --frozen --no-dev --no-editable --extra fuse

# ============================================================
# Stage 2: runtime
# ============================================================
FROM python:3.13-slim

# Set by CI (docker-build.yml) from the release; OCI labels for `docker inspect`
ARG VERSION=0.0.0
ARG REVISION=unknown
ARG CREATED=unknown
LABEL org.opencontainers.image.title="sluice" \
      org.opencontainers.image.description="Sluice — a Python FUSE filesystem with an Express-style router that gates, rewrites, and middleware-intercepts every filesystem call between virtual paths and their backing stores." \
      org.opencontainers.image.source="https://github.com/metalayerlab/sluice" \
      org.opencontainers.image.documentation="https://github.com/metalayerlab/sluice/wiki" \
      org.opencontainers.image.licenses="Unlicense" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.created="${CREATED}"

# The `runtime:` packages in contrib/system-packages.txt: libraries and tools the
# project needs at run time. None listed: nothing runs
# hadolint ignore=DL3008,SC2046
RUN --mount=type=bind,source=contrib/system-packages.txt,target=/tmp/system-packages.txt \
    set -- $(sed -n 's/^runtime://p' /tmp/system-packages.txt); \
    if [ $# -gt 0 ]; then \
        apt-get update \
        && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "$@" \
        && rm -rf /var/lib/apt/lists/*; \
    fi

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

RUN useradd --system --create-home --shell /usr/sbin/nologin --uid 1000 sluice

USER 1000:1000
ENTRYPOINT ["sluice"]
CMD ["--help"]
