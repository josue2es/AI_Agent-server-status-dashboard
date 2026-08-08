# AI Agents Dashboard

A self-hosted monitoring dashboard for a fleet of servers — those running AI agents and plain servers alike. Built with Python and [NiceGUI](https://nicegui.io). Runs as a system (root) service, monitors any number of servers from one page, and aggregates agent data — cron jobs, memories, issues, and model configuration — from the user accounts you point it at. Agent data is currently read from each user's [openclaw](https://openclaw.ai) workspace.

![Dashboard](https://img.shields.io/badge/python-3.10%2B-blue) ![License](https://img.shields.io/badge/license-MIT-green)

> **Working on this repo (human or AI agent)?** Read [CLAUDE.md](CLAUDE.md) first — function-level code map, threading model, and conventions — so you can jump straight to the right part of `dashboard.py` instead of reading all ~860 lines. The task queue and the multi-server feature spec are in [NEXT_STEPS.md](NEXT_STEPS.md).

## Features

- **Multi-server monitoring** — pick a server from the header and the whole page reloads for it. Add servers from a popup dialog (no CLI, no restart); each one is asked whether it runs AI agents, and the agent panels only appear for those that do
- **Live system stats** — CPU, RAM, disk, network throughput, ping/jitter/packet loss
- **Historical charts** — rolling window of the last 720 samples (~3 h), 24-hour retention in SQLite (ECharts)
- **AI agent integration** — displays active cron jobs, agent memories, and issues from every configured user (default: `hermes` and `openclaw`), each entry tagged with its owner
- **Model configuration** — a per-agent table of each profile's primary provider/model, reasoning level, and fallback chain, across every agent of every user
- **API health test** — test Anthropic, Google, and Moonshot/Kimi keys directly from the dashboard with latency measurement (keys are discovered across all configured users' auth profiles)
- **Internet speed test** — powered by the Ookla or Python speedtest CLI (auto-detected), rate-limited to once per hour
- **Password login** — session-based auth, sessions survive restarts
- **Health endpoint** — unauthenticated `GET /health` for uptime monitors
- **Zero token usage** — the dashboard itself makes no LLM calls

## Requirements

- Python 3.10+
- [NiceGUI](https://nicegui.io) (`pip install -r requirements.txt`)
- Optional: [openclaw](https://openclaw.ai) under one or more user accounts, for the AI-agent panels
- A speed-test CLI on `PATH` (optional, for the speed test feature) — either the
  [Ookla CLI](https://www.speedtest.net/apps/cli) (installs as `speedtest`) or the Python
  `speedtest-cli` (`apt install speedtest-cli`). Both are detected automatically, and the
  one in use is logged at startup (`Speed test: /usr/bin/speedtest (ookla CLI)`)

## Monitoring more than one server

The same `dashboard.py` runs in two modes, selected by `DASHBOARD_MODE`:

| Mode | What it does |
|---|---|
| `hub` (default) | Serves the dashboard UI, monitors its own host, and pulls from every registered server |
| `node` | Headless: samples its own metrics and serves a token-protected JSON API. No login page, no UI — `/` returns 404 |

To monitor a server, copy `dashboard.py` there, run it in node mode with a shared secret, then register it from the hub's UI:

```bash
# On the server you want to monitor:
DASHBOARD_MODE=node DASHBOARD_NODE_TOKEN=$(openssl rand -hex 32) python3 dashboard.py
```

Then in the hub, click **⚙ Manage servers**, enter the server's name, base URL and that token, and answer whether it runs AI agents — if it does, list the usernames to monitor. The hub calls the node's `/api/ping` and only saves the entry once it validates; the new server shows up in the selector immediately, with no restart.

Registered servers live in `DASHBOARD_DATA_DIR/servers.json` (mode `0600` — it holds tokens). The local server is implicit and needs no entry; its agent users default to `DASHBOARD_USERS` and can be overridden from the same dialog. Metrics are **not** centralized: each node keeps its own SQLite history and the hub reads it on demand.

### Node API

Every route requires `Authorization: Bearer <DASHBOARD_NODE_TOKEN>` and answers 401 otherwise.

| Route | Returns |
|---|---|
| `GET /api/ping` | Reachability + auth check (`{"ok": true, "mode", "name", "agents"}`) |
| `GET /api/snapshot` | The latest system snapshot |
| `GET /api/history?limit=720` | Chart history |
| `GET /api/agentdata?users=a,b` | Cron jobs, memories, issues and model config for those users |
| `POST /api/apitest` | Runs the API probe on that server (body `{"provider", "model"}`) |
| `POST /api/speedtest` | Runs the speed test on that server |

Separately, **`GET /health`** needs no token and is served in both modes — `{"status":"ok","mode":...,"last_sample_age_s":...}` for external uptime monitors. It carries no secrets and says nothing about other servers.

Setting `DASHBOARD_NODE_TOKEN` on a hub also exposes these routes, so one hub can be monitored by another.

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

### 5. On each monitored server (node mode)

Install exactly as above, then use this unit instead — no password is needed, since a node has no UI:

```bash
[Service]
ExecStart=/opt/ai-agents-dashboard/venv/bin/python /opt/ai-agents-dashboard/dashboard.py
Environment=DASHBOARD_MODE=node
Environment=DASHBOARD_NODE_TOKEN=<openssl rand -hex 32>
Environment=DASHBOARD_USERS=hermes
Restart=on-failure
RestartSec=5
```

Then register it from the hub's **⚙ Manage servers** dialog.

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
| `DASHBOARD_USERS` | `hermes,openclaw` | Comma-separated users whose openclaw data is aggregated (default agent list for the local server) |
| `DASHBOARD_MODE` | `hub` | `hub` serves the dashboard UI; `node` is headless and serves only the JSON API |
| `DASHBOARD_NODE_TOKEN` | *(unset)* | Shared secret a hub presents to read this server's API. Required in node mode; setting it on a hub also exposes its API |
| `DASHBOARD_NODE_NAME` | hostname | Name this server reports on `/api/ping` |
| `DASHBOARD_PORT` | `8080` | Port to listen on |
| `DASHBOARD_DATA_DIR` | `/var/lib/ai-agents-dashboard` | Directory for the SQLite database and session secret |
| `DASHBOARD_DISK_PATH` | `/` | Filesystem whose disk usage is shown (point it at a host bind mount when containerized) |
| `DASHBOARD_NET_IFACE` | `auto` | Interface to measure throughput on. `auto` picks the first non-loopback interface with received traffic |
| `DASHBOARD_MODEL_CONFIG` | `models.json,profiles.json,model-profiles.json,config.json` | Candidate model-config filenames per agent, tried in order |
| `DASHBOARD_PASSWORD_HASH` | Generated on first run | SHA-256 hash of the login password. When unset, a random password is generated, printed once to the log, and its hash persisted in the data dir |
| `DASHBOARD_SECRET` | Auto-generated, persisted | Secret for session cookies |
| `DASHBOARD_TZ` | `America/El_Salvador` | Timezone for chart labels and cron times |
| `DASHBOARD_SPEEDTEST_BIN` | *(auto-detect)* | Path to the speed-test CLI. Unset searches `speedtest`, `speedtest-ookla`, `speedtest-cli` on `PATH`, then `/usr/bin`, `/usr/local/bin` and `/snap/bin`. The legacy name `SPEEDTEST_BIN` is still read |

## Dashboard Layout

0. Server selector + **⚙ Manage servers** (header)
1. Live Resource Usage (CPU & RAM)
2. Network Stats (latency, jitter, packet loss)
3. Network Throughput
4. API Test / Memory & Disk / Speed Test
5. Top Processes & Security Logins
6. Active Cron Jobs (all users)
7. Model Configuration — All Profiles (per agent)
8. Recent Memories (per user)
9. Active & Resolved Issues (tagged by user)

## Architecture (for contributors & AI agents)

Everything lives in one file, [`dashboard.py`](dashboard.py), organized in sections marked with `# ---------------- <name>` comments: env config → `database` → `metric collector` (daemon thread, 15 s sampling) → `multi-user agent data` → `model / profile config` → `API test` → `speed test` → `server registry` → `data sources` → `node API` → `UI` (NiceGUI pages) → `main`.

Every panel reads through the `data sources` seam (`source_snapshot` / `source_history` / `source_agentdata`), which returns the same shapes whether the selected server is local or remote — so the UI never branches on where the data came from.

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

- **Set `DASHBOARD_PASSWORD_HASH`.** Left unset, the dashboard generates a random password on first start and prints it once to the log (`journalctl -u ai-agents-dashboard`) — recoverable, but easy to miss. There is no default password in the source.
- **Node tokens are secrets.** Generate one per server (`openssl rand -hex 32`), never reuse them, and keep `servers.json` at mode `0600` — the dashboard writes it that way, so don't loosen it.
- **Hub → node traffic is plain HTTP** unless you proxy it. Keep nodes on a private interface, a LAN, or a VPN, or front them with a TLS reverse proxy. A node with a reachable port and a leaked token exposes its metrics and lets a caller trigger API and speed tests on it.
- The dashboard itself serves plain HTTP on `0.0.0.0`. Put it behind a TLS reverse proxy (nginx/caddy) or keep it on a private interface/VPN before exposing it.
- It runs as **root** and reads every configured user's `~/.openclaw`, including `auth-profiles.json` (API keys) for the API test panel. Only configure users whose data the dashboard operator may see.

## Notes

- The dashboard runs entirely on your own servers — no external services, no token usage
- System metrics (CPU, RAM, network) are sampled every 15 seconds by a background collector on each server
- Agent data (cron jobs, model config, memories, issues) refreshes every 60 seconds
- All metric data is stored locally in SQLite with 24-hour retention

## License

MIT
