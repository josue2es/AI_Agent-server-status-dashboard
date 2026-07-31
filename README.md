# AI Agents Dashboard

A self-hosted server monitoring dashboard for hosts running AI agents. Built with Python and [NiceGUI](https://nicegui.io). Runs as a system (root) service and aggregates agent data — cron jobs, memories, and issues — from **multiple user accounts** on the same server. Agent data is currently read from each user's [openclaw](https://openclaw.ai) workspace.

![Dashboard](https://img.shields.io/badge/python-3.10%2B-blue) ![License](https://img.shields.io/badge/license-MIT-green)

> **Working on this repo (human or AI agent)?** Read [CLAUDE.md](CLAUDE.md) first — function-level code map, threading model, and conventions — so you can jump straight to the right part of `dashboard.py` instead of reading all ~860 lines. The task queue and the multi-server feature spec are in [NEXT_STEPS.md](NEXT_STEPS.md).

## Features

- **Live system stats** — CPU, RAM, disk, network throughput, ping/jitter/packet loss
- **Historical charts** — rolling window of the last 720 samples (~3 h), 24-hour retention in SQLite (ECharts)
- **Multi-user openclaw integration** — displays active cron jobs, agent memories, and issues from every configured user (default: `hermes` and `openclaw`), each entry tagged with its owner
- **Model configuration** — a per-agent table of each profile's primary provider/model, reasoning level, and fallback chain, across every agent of every user
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
| Model config | `/home/<user>/.openclaw/agents/<agent>/[agent/]{models,profiles,model-profiles,config}.json` |

The user list is controlled by the `DASHBOARD_USERS` environment variable (comma-separated, default `hermes,openclaw`). Missing files for a user are skipped silently.

### Model configuration format

Each agent under `/home/<user>/.openclaw/agents/` is scanned for a model-config file (the candidate filenames are configurable via `DASHBOARD_MODEL_CONFIG`, tried in order, both directly in the agent directory and in its `agent/` subdirectory). The expected shape is a `profiles` map where each profile carries its own primary model, reasoning level, and ordered fallback chain:

```json
{
  "profiles": {
    "default": {
      "provider": "zai",
      "model": "glm-5.2",
      "reasoning": "max",
      "fallbacks": [
        { "provider": "deepseek", "model": "deepseek-v4-pro" },
        { "provider": "kimi-coding", "model": "kimi-k2.6" },
        { "provider": "gemini", "model": "gemini-3-pro-preview" }
      ]
    }
  }
}
```

The parser is tolerant of common variations: the primary can be a nested `primary` object or `provider`/`model` fields on the profile; each model reference may be a `{provider, model}` object or a `"provider/model"` string; `reasoning` also matches `reasoningEffort`/`effort` and shows `–` when absent; and `fallbacks` may be a single value or a list. In the table, the first two fallbacks appear as **Fallback 1** / **Fallback 2** and any beyond that collapse into **Final Fallback** (`(none)` when there are two or fewer). If your openclaw build uses different field names, adjust `_parse_profile` in `dashboard.py`.

## Installation

### 1. Copy the script and install dependencies

Install into a virtualenv so the Python dependencies stay off your system Python:

```bash
sudo mkdir -p /opt/ai-agents-dashboard
sudo cp dashboard.py requirements.txt /opt/ai-agents-dashboard/
sudo python3 -m venv /opt/ai-agents-dashboard/venv
sudo /opt/ai-agents-dashboard/venv/bin/pip install -r /opt/ai-agents-dashboard/requirements.txt
```

### 2. Set your password

The login password is a SHA-256 hash. Generate one and pass it via environment:

```bash
echo -n 'your-password' | sha256sum
```

### 3. Create the systemd service (runs as root)

```bash
sudo tee /etc/systemd/system/ai-agents-dashboard.service << 'EOF'
[Unit]
Description=AI Agents Dashboard
After=network.target

[Service]
ExecStart=/opt/ai-agents-dashboard/venv/bin/python /opt/ai-agents-dashboard/dashboard.py
Environment=DASHBOARD_USERS=hermes,openclaw
Environment=DASHBOARD_TZ=America/El_Salvador
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
sudo systemctl enable --now ai-agents-dashboard
```

The dashboard will be available at `http://localhost:8080`.

The service runs as root because it reads each user's `/home/<user>/.openclaw` directory and host-level facilities (`/proc`, `ps`, `ping`, login history). It is a single process with no inbound dependencies beyond the login page.

### Updating

```bash
sudo cp dashboard.py /opt/ai-agents-dashboard/
sudo /opt/ai-agents-dashboard/venv/bin/pip install -r /opt/ai-agents-dashboard/requirements.txt  # if deps changed
sudo systemctl restart ai-agents-dashboard
```

## Configuration

All configuration is via environment variables ([`.env.example`](.env.example) lists them all). The app reads real environment variables — it does **not** auto-load a `.env` file; set them in the shell or as `Environment=` lines in the systemd unit:

| Variable | Default | Description |
|---|---|---|
| `DASHBOARD_USERS` | `hermes,openclaw` | Comma-separated users whose openclaw data is aggregated |
| `DASHBOARD_PORT` | `8080` | Port to listen on |
| `DASHBOARD_DATA_DIR` | `/var/lib/ai-agents-dashboard` | Directory for the SQLite database and session secret |
| `DASHBOARD_DISK_PATH` | `/` | Filesystem whose disk usage is shown (point it at a host bind mount when containerized) |
| `DASHBOARD_MODEL_CONFIG` | `models.json,profiles.json,model-profiles.json,config.json` | Candidate model-config filenames per agent, tried in order |
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
7. Model Configuration — All Profiles (per agent)
8. Recent Memories (per user)
9. Active & Resolved Issues (tagged by user)

## Roadmap — multi-server monitoring (planned, not implemented yet)

The next milestone turns this into a hub that monitors **many** servers — servers running AI agents and plain servers alike: a server selector on the main page, a headless "node mode" of the same `dashboard.py` exposing a token-protected JSON API, and a popup dialog to register new servers from the UI — asking, per server, whether it runs AI agents (e.g. Hermes/Openclaw) and which users to monitor (no CLI). The full spec with acceptance criteria is [NEXT_STEPS.md](NEXT_STEPS.md) section F1; implementation is assigned to the next agent.

## Architecture (for contributors & AI agents)

Everything lives in one file, [`dashboard.py`](dashboard.py) (~860 lines), organized in sections marked with `# ---------------- <name>` comments: env config → `database` → `metric collector` (daemon thread, 15 s sampling) → `multi-user agent data` → `model / profile config` → `API test` → `speed test` → `UI` (NiceGUI pages) → `main`.

**Read [CLAUDE.md](CLAUDE.md) before touching the code** — it has the function-level code map, threading model, conventions, and gotchas, so you can grep straight to the right section instead of reading the whole file.

## Development

```bash
pip install -r requirements.txt
python3 -m py_compile dashboard.py   # syntax check — there is no test suite
# Run locally without root (uses your own ~/.openclaw if present):
DASHBOARD_DATA_DIR=./data DASHBOARD_USERS=$USER python3 dashboard.py
```

Conventions: single file, plain functions, env-var config (`DASHBOARD_*`, read once at import time). New config needs a default at the top of `dashboard.py`, a row in the table above, and a line in `.env.example`.

## Security notes

- **Always set `DASHBOARD_PASSWORD_HASH`.** The source ships a default hash, so an unconfigured deployment has a known password (fix pending — see NEXT_STEPS.md P1).
- The dashboard serves plain HTTP on `0.0.0.0`. Put it behind a TLS reverse proxy (nginx/caddy) or keep it on a private interface/VPN before exposing it.
- It runs as **root** and reads every configured user's `~/.openclaw`, including `auth-profiles.json` (API keys) for the API test panel. Only configure users whose data the dashboard operator may see.

## Notes

- The dashboard runs entirely on the server — no external services, no token usage
- System metrics (CPU, RAM, network) are sampled every 15 seconds by a background collector
- Agent data (cron jobs, model config, memories, issues) refreshes every 60 seconds
- All metric data is stored locally in SQLite with 24-hour retention

## License

MIT
