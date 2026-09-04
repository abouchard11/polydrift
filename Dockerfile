FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    POLYDRIFT_CONFIG=/app/config.docker.yaml \
    POLYDRIFT_DB_PATH=/data/polydrift.db

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY config.docker.yaml ./config.docker.yaml

# Railway mounts the persistent volume here. Without a volume the DB is
# ephemeral and every redeploy silently restarts the measurement clock.
VOLUME ["/data"]

CMD ["polydrift", "poll", "--loop"]
