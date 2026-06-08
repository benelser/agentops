FROM python:3.12-slim

# uv — fast, deterministic dep installer. Copied from the official image so we
# don't shell-install during the build.
COPY --from=ghcr.io/astral-sh/uv:0.5.7 /uv /uvx /usr/local/bin/

WORKDIR /app

# Install deps from pyproject.toml. We pin in pyproject so this stays
# reproducible without committing a uv.lock to the demo repo.
COPY pyproject.toml ./
RUN uv pip install --system --no-cache \
        "opentelemetry-api==1.28.2" \
        "opentelemetry-sdk==1.28.2" \
        "opentelemetry-exporter-otlp-proto-http==1.28.2" \
        "opentelemetry-exporter-otlp==1.28.2"

COPY . /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=6 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')"

CMD ["python", "workload.py"]
