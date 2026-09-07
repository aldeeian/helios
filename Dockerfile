# Multi-stage build: deps in a builder, only the venv + source in the runtime.
# Runs as a non-root user on a slim base for a small, hardened image.

FROM python:3.12-slim AS builder
WORKDIR /build
# Only the metadata first, so the dependency layer caches across code changes.
COPY pyproject.toml ./
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
 && /opt/venv/bin/pip install --no-cache-dir \
      "pandas>=2.2" "numpy>=1.26" "sqlalchemy>=2.0" "python-dateutil>=2.9" \
      "sqlglot>=25.0" "fastapi>=0.110" "uvicorn[standard]" \
      "openpyxl>=3.1" "statsmodels>=0.14"

FROM python:3.12-slim AS runtime
# Non-root user — the container never runs as root.
RUN useradd --create-home --uid 10001 helios
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HELIOS_CORS_ORIGINS="*"
COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY src ./src
COPY pyproject.toml ./
USER helios
EXPOSE 8080
# Container-level healthcheck mirrors the /health endpoint's 503 semantics.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health').status==200 else 1)" || exit 1
CMD ["sh", "-c", "uvicorn src.api.app:app --host 0.0.0.0 --port ${PORT:-8080}"]
