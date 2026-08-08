# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Single-file fleet monitoring dashboard (`dashboard.py`, ~1400 lines) built with [NiceGUI](https://nicegui.io). Runs as root under systemd, samples system metrics into SQLite, and aggregates AI-agent data (cron jobs, memories, issues, API auth profiles, model/profile config) from Linux users — currently read from each user's `~/.openclaw` directory. The dashboard itself makes no LLM calls; the API Test panel sends one ~10-token prompt only when a human clicks Test.

The same file runs in two modes (`DASHBOARD_MODE`): **`hub`** (default) serves the UI and pulls from registered servers; **`node`** is headless — collector plus a Bearer-token JSON API, no pages registered. Metrics are never centralized: each server keeps its own SQLite history and the hub reads it on demand.

There is no build step, no linter config, and no test suite.

## Commands

```bash
pip install -r requirements.txt        # only dependency: nicegui (fastapi ships with it)
python3 -m py_compile dashboard.py     # syntax check — the only automated verification
# Local run without root (skips other users' data, uses your own ~/.openclaw if present):
DASHBOARD_DATA_DIR=./data DASHBOARD_USERS=$USER DASHBOARD_PORT=8080 python3 dashboard.py
# A second instance as a node, to exercise the multi-server path end to end:
DASHBOARD_MODE=node DASHBOARD_NODE_TOKEN=dev DASHBOARD_DATA_DIR=./node DASHBOARD_PORT=8091 python3 dashboard.py
curl -H 'Authorization: Bearer dev' localhost:8091/api/ping
```

All configuration is environment variables with a `DASHBOARD_` prefix, read once at import time at the top of `dashboard.py`. Full list: README.md table and `.env.example`.

## Code map — dashboard.py (only source file)

Sections are delimited by `# ---------------- <name>` comments. Grep the section name or function name and read only that block; do not read the whole file.

| Section marker | Functions | Purpose |
|---|---|---|
| (top of file) | — | env config (`AGENT_USERS`, `MODEL_CONFIG_FILES`, `DISK_PATH`, …), `MODEL_OPTIONS`, cooldown constants, module globals |
| `database` | `init_db`, `fetch_history` | SQLite schema (`metrics`, `speedtests`); history as parallel lists for charts |
| `metric collector` | `get_sys_info`, `collector` | daemon thread; samples /proc + ping every 15 s into SQLite, 24 h retention |
| `multi-user agent data` | `get_cron_jobs`, `get_memories`, `get_issues`, `find_api_key` | read `/home/<user>/.openclaw/...` for each user in `DASHBOARD_USERS` |
| `model / profile config` | `_fmt_model`, `_parse_profile`, `_load_profiles`, `get_model_config` | per-agent model/profile tables from `agents/<agent>/[agent/]{MODEL_CONFIG_FILES}`; deliberately tolerant parser |
| `API test` | `test_api` | one-shot latency probe to Anthropic / Google / Moonshot via raw `urllib` |
| `speed test` | `speedtest_flavor`, `find_speedtest`, `do_speedtest` | speed-test CLI wrapper (~20 s, blocking), 1 h cooldown; auto-detects the Ookla CLI or the Python `speedtest-cli` |
| `server registry` | `load_servers`, `save_servers`, `all_servers`, `get_server` | `servers.json` (0600, holds node tokens); `local` is implicit and always first |
| `data sources` | `source_snapshot`, `source_history`, `source_stats`, `source_agentdata`, `remote_apitest`, `remote_speedtest`, `_node_request` | the local/remote seam every panel reads through |
| `node API` | `register_health`, `register_node_api`, `_node_authorized` | Bearer-token JSON API under `/api/*`, registered whenever `DASHBOARD_NODE_TOKEN` is set; plus unauthenticated `/health` in both modes |
| `UI` | `make_chart`, `set_chart_data`, `section_card`, `servers_dialog`, `login_page`, `main_page` | NiceGUI pages; `main_page` is rebuilt per client, driven by two `ui.timer`s (15 s stats, 60 s agent data) plus a one-shot first paint |
| `main` | `load_password_hash`, `load_storage_secret`, `__main__` guard | startup: mode validation, `init_db`, node API registration, collector thread, `ui.run` |

## Architecture invariants

- **Threading:** `collector()` (daemon thread) is the only writer of the `latest` snapshot dict (guarded by `latest_lock`) and the only metrics writer to SQLite. UI code only reads. Blocking work triggered from the UI must go through `run.io_bound(...)`.
- **Blocking helpers:** `get_sys_info` (~4 s ping), `test_api`, `do_speedtest` are synchronous by design — never call them directly from an async UI handler.
- **Timestamps:** SQLite stores UTC (`CURRENT_TIMESTAMP`); conversion to `LOCAL_TZ` happens only at display time (`fetch_history`, cron/memory formatting).
- **Charts:** `fetch_history` returns parallel lists; `set_chart_data(chart, labels, *series)` takes one list per series in the order the chart's series were declared in `make_chart`.
- **Multiprocessing guard:** the `__name__ in {'__main__', '__mp_main__'}` check is required — NiceGUI re-imports the module in a child process. Don't remove it.
- **Missing user files are normal:** all agent-data readers skip absent files/dirs silently (users may not have cron jobs / memories / issues / model config). That is intentional, not a bug.
- **Never bypass the seam:** UI panels must read via `source_*(server)`, never call `fetch_history()` / `get_cron_jobs()` directly. Those return local data and would silently show the wrong server's numbers.
- **Remote reads are blocking:** every `source_*` call on a remote server is synchronous `urllib`. UI handlers touching them are `async` and go through `run.io_bound`, and every call site catches the exception — an unreachable node must render an inline error, never take the page down.
- **Agent-data readers take a user list:** `get_cron_jobs/get_memories/get_issues/get_model_config` accept `users=None` (meaning `AGENT_USERS`). The selected server supplies it, and the hub forwards its list to the node — that's what makes the dialog's usernames load-bearing.
- **Pages are registered conditionally** at the bottom of the UI section (`ui.page('/')(main_page)`), not with `@ui.page` decorators, so node mode can serve the API with no UI. Don't reintroduce the decorators.
- **`servers.json` holds tokens:** always write it through `save_servers()` (0600). A malformed file degrades to local-only rather than raising.

## Conventions

- Single file, plain functions, no classes. Keep it that way unless explicitly asked to restructure.
- New config = env var `DASHBOARD_*` with a default at the top of the file (comma-separated lists go through `env_list()`), plus a row in the README table and a line in `.env.example`.
- Dark theme: page bg `#1e1e1e`, cards `#2d2d2d` with border `#444`, accent orange `#ff9900`, monospace font. Build new page blocks with `section_card(title)`.
- English comments and strings; short docstrings on functions, section-divider comments between groups.

## Gotchas

- `PASSWORD_HASH` has no default: unset means a random password is generated and printed once at startup, its hash persisted at `DATA_DIR/.password_hash`. Never commit a real password hash, and never reintroduce a default.
- `MODEL_OPTIONS` values must be exact provider model IDs. Never guess or construct an ID; verify against the provider's official docs before changing them.
- The collector sleeps the *remainder* of `COLLECT_INTERVAL` after each sample, so the real cadence is 15 s even though the ping blocks ~4 s. Don't change it back to a flat `sleep(COLLECT_INTERVAL)`.
- `test_api` uses raw `urllib` on purpose (zero extra dependencies). Do not introduce provider SDKs for it.
- Two different CLIs answer to the name `speedtest`: Ookla's (flags `--accept-license -f json`, bandwidth in **bytes**/s) and the Python `speedtest-cli` (flags `--json`, throughput already in **bits**/s), which installs `speedtest` as a second entry point. `find_speedtest()` resolves the path and `speedtest_flavor()` identifies it via `--version` — never hard-code a binary name or assume one JSON shape.

## Pending work

Read `NEXT_STEPS.md` before starting changes — it is the authoritative spec and task queue, with acceptance criteria per task. Every task in it (P1 security, F1 multi-server monitoring, P2 robustness, P3 improvements) is now done; new work is appended there as it comes in from deployment. Keep this file in sync when a task changes the code map or an invariant.
