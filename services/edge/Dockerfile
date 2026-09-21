# syntax=docker/dockerfile:1
# The edge service. The build context is the repository root (see docker-compose.yml), because the
# service depends on the libraries of the workspace:
#
#   docker build -f services/edge/Dockerfile -t factotum-edge .

ARG PYTHON_VERSION=3.13

FROM python:${PYTHON_VERSION}-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.17 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
COPY libs libs
COPY services/edge services/edge
# Only this service and what it needs, installed (not editable) from the locked versions. The
# clients of the workspace are not part of it.
RUN uv sync --frozen --no-dev --no-editable --package factotum-edge

FROM python:${PYTHON_VERSION}-slim
RUN useradd --system --uid 10001 --no-create-home factotum
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FACTOTUM_EDGE_HOST=0.0.0.0
USER 10001
EXPOSE 8084
HEALTHCHECK --interval=10s --timeout=3s --retries=5 --start-period=10s \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8084/healthz', timeout=2)"]
CMD ["python", "-m", "factotum.edge"]
