# Openclaw server status dashboard — container image.
#
# The dashboard monitors the HOST it runs on, so it must be run with the
# host's network and PID namespaces and a few bind mounts. See docker-compose.yml
# or the "Running in a container" section of the README for the full command.

FROM python:3.12-slim

# Tools the dashboard shells out to:
#   iputils-ping -> ping (latency/jitter/loss)   procps -> ps (top processes)
#   util-linux   -> last (login history; `who` ships with the base image)
RUN apt-get update && apt-get install -y --no-install-recommends \
        iputils-ping \
        procps \
        util-linux \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY dashboard.py .

# SQLite database + persisted session secret live here; mount a volume to keep them.
ENV DASHBOARD_DATA_DIR=/var/lib/openclaw-dashboard
VOLUME ["/var/lib/openclaw-dashboard"]

EXPOSE 8080

CMD ["python3", "dashboard.py"]
