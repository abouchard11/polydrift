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

# NOTE: no Dockerfile VOLUME directive — Railway rejects it ("docker VOLUME
# ... is not supported, use Railway Volumes") and the build fails. Persistence
# comes from the Railway volume attached to this service at /data. Without that
# volume the DB is ephemeral and every redeploy silently restarts the clock.

CMD ["polydrift", "poll", "--loop"]
