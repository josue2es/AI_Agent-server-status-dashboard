# Openclaw Server Status Dashboard

A self-hosted server monitoring dashboard for servers running [openclaw](https://openclaw.ai). Built with Python and [NiceGUI](https://nicegui.io). Runs as a system (root) service and aggregates openclaw data — cron jobs, agent memories, and issues — from **multiple user accounts** on the same server.

![Dashboard](https://img.shields.io/badge/python-3.10%2B-blue) ![License](https://img.shields.io/badge/license-MIT-green)

## Features

- **Live system stats** — CPU, RAM, disk, network throughput, ping/jitter/packet loss
- **Historical charts** — 3-hour rolling window, 24-hour retention in SQLite (ECharts)
- **Multi-user openclaw integration** — displays active cron jobs, agent memories, and issues from every configured user (default: `hermes` and `openclaw`), each entry tagged with its owner
- **API health test** — test Anthropic, Google, and Moonshot/Kimi keys directly from the dashboard with latency measurement (keys are discovered across all configured users' auth profiles)
- **Internet speed test** — powered by Ookla speedtest CLI, rate-limited to once per hour
- **Password login** — session-based auth, sessions survive restarts
- **Zero token usage** — the dashboard itself makes no LLM calls

## Requirements

- Python 3.10+
- [NiceGUI](https://nicegui.io) (`pip install -r requirements.txt`)
- A server running [openclaw](https://openclaw.ai) under one or more user accounts
- Ookla speedtest CLI on `PATH` as `speedtest-ookla` (optional, for the speed test feature)

## How multi-user aggregation works

The dashboard runs as root and reads each configured user's openclaw workspace:

| Data | Path per user |
|---|---|
| Cron jobs | `/home/<user>/.openclaw/cron/jobs.json` |
| Agent memories | `/home/<user>/.openclaw/workspace/memory/<today>.md` |
| Issues | `/home/<user>/.openclaw/workspace/ISSUES.md` |
| API auth profiles | `/home/<user>/.openclaw/agents/main/agent/auth-profiles.json` |

The user list is controlled by the `DASHBOARD_USERS` environment variable (comma-separated, default `hermes,openclaw`). Missing files for a user are skipped silently.

## Installation

### 1. Copy the script and install dependencies

```bash
sudo mkdir -p /opt/openclaw-dashboard
sudo cp dashboard.py requirements.txt /opt/openclaw-dashboard/
sudo pip install -r /opt/openclaw-dashboard/requirements.txt
```

### 2. Set your password

The login password is a SHA-256 hash. Generate one and pass it via environment:

```bash
echo -n 'your-password' | sha256sum
```

### 3. Create the systemd service (runs as root)

```bash
sudo tee /etc/systemd/system/openclaw-dashboard.service << 'EOF'
[Unit]
Description=Openclaw Server Status Dashboard
After=network.target

[Service]
ExecStart=/usr/bin/python3 /opt/openclaw-dashboard/dashboard.py
Environment=DASHBOARD_USERS=hermes,openclaw
# Environment=DASHBOARD_PASSWORD_HASH=<your sha256 hash>
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
```

### 4. Enable and start

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now openclaw-dashboard
```

The dashboard will be available at `http://localhost:8080`.

## Configuration

All configuration is via environment variables:

| Variable | Default | Description |
|---|---|---|
| `DASHBOARD_USERS` | `hermes,openclaw` | Comma-separated users whose openclaw data is aggregated |
| `DASHBOARD_PORT` | `8080` | Port to listen on |
| `DASHBOARD_DATA_DIR` | `/var/lib/openclaw-dashboard` | Directory for the SQLite database and session secret |
| `DASHBOARD_PASSWORD_HASH` | See script | SHA-256 hash of the login password |
| `DASHBOARD_SECRET` | Auto-generated, persisted | Secret for session cookies |
| `DASHBOARD_TZ` | `America/El_Salvador` | Timezone for chart labels and cron times |
| `SPEEDTEST_BIN` | `speedtest-ookla` | Path to the Ookla speedtest CLI |

## Dashboard Layout

1. Live Resource Usage (CPU & RAM)
2. Network Stats (latency, jitter, packet loss)
3. Network Throughput
4. API Test / Memory & Disk / Speed Test
5. Top Processes & Security Logins
6. Active Cron Jobs (all users)
7. Recent Memories (per user)
8. Active & Resolved Issues (tagged by user)

## Notes

- The dashboard runs entirely on the server — no external services, no token usage
- System metrics (CPU, RAM, network) are sampled every 15 seconds by a background collector
- Openclaw data (cron jobs, memories, issues) refreshes every 60 seconds
- All metric data is stored locally in SQLite with 24-hour retention

## License

MIT
