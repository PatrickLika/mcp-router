# Built for linux/arm64 (the Pi) by .github/workflows/build.yml.
FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # OAuth client registrations (Claude) and upstream tokens are kept here,
    # encrypted, so a pod restart does not force you to reconnect.
    FASTMCP_HOME=/data \
    ROUTER_CONFIG=/config/upstreams.yaml

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY router/ router/

RUN useradd --uid 10001 --no-create-home router && mkdir -p /data && chown router /data
USER 10001

EXPOSE 8000
CMD ["python", "-m", "router.main"]
