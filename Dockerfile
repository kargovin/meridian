# syntax=docker/dockerfile:1

# One workspace, two deployables (A1). The `app` and `platform` targets are the images the
# cluster runs; `dev` is the same code with the test tooling installed, for compose.yaml.
#
# Third-party dependencies are installed from the lockfile before the source is copied, so an
# edit to a module rebuilds only the final layer. The workspace packages themselves are
# installed editable, pointing at /app: a bind mount over /app makes a host edit live without
# a rebuild, and without a mount the copied source is what runs.

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS base

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

# uid 1000 so files a container writes into a bind-mounted checkout stay owned by the
# developer, not root.
RUN useradd --uid 1000 --create-home app \
    && mkdir -p /app /opt/venv \
    && chown app:app /app /opt/venv
USER app
WORKDIR /app

# Resolving the workspace needs every member's pyproject; nothing else yet.
COPY --chown=app:app pyproject.toml uv.lock ./
COPY --chown=app:app libs/contract/pyproject.toml libs/contract/
COPY --chown=app:app libs/config/pyproject.toml libs/config/
COPY --chown=app:app libs/dbkit/pyproject.toml libs/dbkit/
COPY --chown=app:app platform/pyproject.toml platform/
RUN --mount=type=cache,target=/home/app/.cache/uv,uid=1000,gid=1000 \
    uv sync --locked --all-packages --no-install-workspace --no-dev

COPY --chown=app:app . .
RUN --mount=type=cache,target=/home/app/.cache/uv,uid=1000,gid=1000 \
    uv sync --locked --all-packages --no-dev


FROM base AS app
EXPOSE 8080
CMD ["uvicorn", "meridian.web.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]


FROM base AS platform
EXPOSE 8081
CMD ["uvicorn", "meridian_platform.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8081"]


# The dev group adds pytest, ruff, mypy and the MLflow client, so the test suite and the
# eval harness run inside the same container the services do. git is for the harness's
# provenance (it records the commit and whether the tree is dirty) and the tests of it.
FROM base AS dev
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
USER app
RUN --mount=type=cache,target=/home/app/.cache/uv,uid=1000,gid=1000 \
    uv sync --locked --all-packages
