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

COPY requirements.txt /app/
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
