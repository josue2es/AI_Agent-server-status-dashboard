# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Single-file server monitoring dashboard (`dashboard.py`, ~860 lines) built with [NiceGUI](https://nicegui.io). Runs as root under systemd, samples system metrics into SQLite, and aggregates AI-agent data (cron jobs, memories, issues, API auth profiles, model/profile config) from multiple Linux users — currently read from each user's `~/.openclaw` directory. The dashboard itself makes no LLM calls; the API Test panel sends one ~10-token prompt only when a human clicks Test.

There is no build step, no linter config, and no test suite.

## Commands

```bash
pip install -r requirements.txt        # only dependency: nicegui (fastapi ships with it)
python3 -m py_compile dashboard.py     # syntax check — the only automated verification
# Local run without root (skips other users' data, uses your own ~/.openclaw if present):
DASHBOARD_DATA_DIR=./data DASHBOARD_USERS=$USER DASHBOARD_PORT=8080 python3 dashboard.py
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
| `speed test` | `do_speedtest` | Ookla CLI wrapper (~20 s, blocking), 1 h cooldown |
| `UI` | `make_chart`, `set_chart_data`, `section_card`, `login_page`, `main_page` | NiceGUI pages; `main_page` is rebuilt per client and driven by two `ui.timer`s (15 s stats, 60 s openclaw data) |
| `main` | `load_storage_secret`, `__main__` guard | startup: `init_db`, collector thread, `ui.run` |

## Architecture invariants

- **Threading:** `collector()` (daemon thread) is the only writer of the `latest` snapshot dict (guarded by `latest_lock`) and the only metrics writer to SQLite. UI code only reads. Blocking work triggered from the UI must go through `run.io_bound(...)`.
- **Blocking helpers:** `get_sys_info` (~4 s ping), `test_api`, `do_speedtest` are synchronous by design — never call them directly from an async UI handler.
- **Timestamps:** SQLite stores UTC (`CURRENT_TIMESTAMP`); conversion to `LOCAL_TZ` happens only at display time (`fetch_history`, cron/memory formatting).
- **Charts:** `fetch_history` returns parallel lists; `set_chart_data(chart, labels, *series)` takes one list per series in the order the chart's series were declared in `make_chart`.
- **Multiprocessing guard:** the `__name__ in {'__main__', '__mp_main__'}` check is required — NiceGUI re-imports the module in a child process. Don't remove it.
- **Missing user files are normal:** all agent-data readers skip absent files/dirs silently (users may not have cron jobs / memories / issues / model config). That is intentional, not a bug.

## Conventions

- Single file, plain functions, no classes. Keep it that way unless explicitly asked to restructure.
- New config = env var `DASHBOARD_*` with a default at the top of the file, plus a row in the README table and a line in `.env.example`.
- Dark theme: page bg `#1e1e1e`, cards `#2d2d2d` with border `#444`, accent orange `#ff9900`, monospace font. Build new page blocks with `section_card(title)`.
- English comments and strings; short docstrings on functions, section-divider comments between groups.

## Gotchas

- `PASSWORD_HASH` has a hard-coded default in the source — a known security issue (see NEXT_STEPS.md P1). Never commit a real password hash.
- `MODEL_OPTIONS` values must be exact provider model IDs. Never guess or construct an ID; verify against the provider's official docs before changing them.
- Network throughput only matches interfaces named `eth0`/`ens3` in `get_sys_info` (see NEXT_STEPS.md P2).
- Real collector cadence is ~19–20 s (15 s sleep after a ~4 s blocking ping), so the 720-sample chart window spans closer to 4 h than 3 h (see NEXT_STEPS.md P2).
- `test_api` uses raw `urllib` on purpose (zero extra dependencies). Do not introduce provider SDKs for it.

## Pending work

Read `NEXT_STEPS.md` before starting changes — it is the authoritative spec and task queue, with acceptance criteria per task. Current mandate: **F1 multi-server monitoring** (hub/agent modes, server selector, popup registration dialog), executed in the order P1 → F1 → P2 → P3. Nothing from F1 exists in the code yet; this file describes `dashboard.py` as it is today and must be updated when F1 lands.
