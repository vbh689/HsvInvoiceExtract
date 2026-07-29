# syntax=docker/dockerfile:1

# ---- uv binary source (pinned, not :latest) ----
FROM ghcr.io/astral-sh/uv:0.7.2 AS uv

# ---- builder: resolve + install deps, then install the project ----
FROM python:3.12-slim AS builder

COPY --from=uv /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Layer-cache-friendly: deps only re-resolve when the lock or pyproject
# actually change, not on every app code edit.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY app/ ./app/
COPY prompts/ ./prompts/
# app/llm.py::DEFAULT_FIXTURE_DIR resolves mock fixtures relative to this tree
# at runtime (MOCK_MODE + X-Mock-Fixture is a documented runtime feature, not
# just a pytest fixture dir), so it has to ship even though the rest of tests/
# doesn't.
COPY tests/fixtures/mock_responses/ ./tests/fixtures/mock_responses/
RUN uv sync --frozen --no-dev --no-editable

# ---- runtime: no uv, no build tooling, no dev deps ----
FROM python:3.12-slim AS runtime

RUN groupadd --gid 1000 app \
    && useradd --uid 1000 --gid app --home-dir /app --shell /usr/sbin/nologin --no-create-home app

WORKDIR /app
COPY --from=builder --chown=app:app /app /app

# WORKDIR creates /app as root before the COPY above, so the directory entry
# itself stays root-owned even though its contents are app:app -- the app
# user can't mkdir a fresh ./data (DATABASE_PATH's default parent) inside it
# without this. A bind mount at runtime overlays this anyway, but the image
# should also work standalone (e.g. `docker run` without compose).
RUN mkdir -p /app/data && chown app:app /app/data

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER app
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
