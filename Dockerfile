# Export stage: materialize the production requirement set from uv.lock.
# uv.lock is the single source of truth — there is no tracked requirements.txt.
# A stale tracked copy once shipped an image missing half its runtime deps
# (v0.3.0 migrate job died on ModuleNotFoundError: flask_cors; see
# docs/ci/CI-AUDIT-REPORT.md). --frozen means the lockfile is used exactly as
# committed, never re-resolved at build time.
FROM ghcr.io/astral-sh/uv:0.11.29-python3.13-trixie-slim AS export
WORKDIR /export
COPY pyproject.toml uv.lock ./
RUN uv export --format requirements-txt --no-dev --extra postgres \
    --no-hashes --no-emit-project --frozen -o /requirements.txt

FROM python:3.13-slim
COPY --from=datadog/serverless-init:1 /datadog-init /app/datadog-init

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV DD_SERVICE=flask-backend
ENV DD_SITE=us5.datadoghq.com
ENV DD_LOGS_ENABLED=true
ENV DD_LOGS_INJECTION=true
ENV DD_SOURCE=python
ENV DD_PROFILING_ENABLED=true
ENV DD_APPSEC_ENABLED=true

WORKDIR /app

RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=export /requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app/

EXPOSE 5000
# serverless-init needs DD_API_KEY at runtime or telemetry silently goes nowhere;
# production injects it via --set-secrets in the cookbook repo's cloudbuild.yaml.
ENTRYPOINT ["/app/datadog-init"]
# Gunicorn, never `python app.py`: the __main__ path runs Werkzeug's debug
# server (app.run(debug=True)), which must not serve production traffic.
# Cloud Run injects PORT; 5000 matches the old local-run default.
CMD ["sh", "-c", "exec ddtrace-run gunicorn --bind 0.0.0.0:${PORT:-5000} --workers 1 --threads 8 --timeout 0 app:app"]
