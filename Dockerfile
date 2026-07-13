FROM python:3.12-slim
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
ENTRYPOINT ["/app/datadog-init"]
CMD ["ddtrace-run", "python", "app.py"]
