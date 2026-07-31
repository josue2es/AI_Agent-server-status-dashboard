# NEXT_STEPS — Instructions for the next agent (Opus 8)

Produced 2026-07-31 on branch `claude/code-review-readme-update-r5aazt`, from a full code review of `dashboard.py` plus the owner's feature mandate. **Nothing in this file is implemented yet — this is the spec.** Execute in this order: **P1 → F1 → P2 → P3**. P1 items are tiny security fixes worth landing before the feature enlarges the attack surface; F1 is the main mandate.

## Ground rules

1. Read `CLAUDE.md` first (code map, threading model, conventions). Read only the sections of `dashboard.py` a task touches.
2. Verify after every change: `python3 -m py_compile dashboard.py`. Where feasible, run locally (`DASHBOARD_DATA_DIR=./data DASHBOARD_USERS=$USER python3 dashboard.py`) and load http://localhost:8080. There is no test suite.
3. Commit per task (or per numbered sub-task for F1) with a descriptive message; push with `git push -u origin <branch>`.
4. Keep the single-file, stdlib-plus-nicegui design. All HTTP (including hub→agent calls in F1) stays on `urllib` — no new dependencies, no provider SDKs.
5. If a change alters behavior or config, update `README.md`, `CLAUDE.md`, and `.env.example` in the same commit.
6. Tick the checklist below as you finish tasks.

## Task list

- [ ] P1.1 Remove default password hash
- [ ] P1.2 Google API key out of the URL
- [ ] F1.1 Data-source seam (local vs remote)
- [ ] F1.2 Agent mode + JSON API + token auth
- [ ] F1.3 Server registry (`servers.json`)
- [ ] F1.4 Server selector on the main page
- [ ] F1.5 "Add server / Add AI agent" dialog
- [ ] F1.6 Remote actions (API test, speed test)
- [ ] F1.7 Documentation update
- [ ] P2.1 Configurable network interface
- [ ] P2.2 Safe SQLite handling (close + WAL)
- [ ] P2.3 Close cooldown races
- [ ] P2.4 Fix collector cadence drift
- [ ] P3.1 Memories fallback to latest file
- [ ] P3.2 Refresh MODEL_OPTIONS
- [ ] P3.3 Pin nicegui upper bound
- [ ] P3.4 /health endpoint

## P1 — Security (land these first; they are small)

### P1.1 Remove the hard-coded default password hash
- **Where:** `PASSWORD_HASH` constant (top of file).
- **Problem:** the source ships a default SHA-256 hash, so every deployment that forgets `DASHBOARD_PASSWORD_HASH` shares a known password.
- **Change:** when `DASHBOARD_PASSWORD_HASH` is unset, generate a random password on first start (`secrets.token_urlsafe(12)`), print it once to stdout/journal, and persist its hash in `DATA_DIR` (file mode 0600); reuse the persisted hash on later boots. Keep `hmac.compare_digest`. An explicitly set env var always wins.
- **Accept:** no default hash constant remains; booting without the env var logs a generated password once and that password logs in; setting the env var still works.

### P1.2 Move the Google API key out of the URL
- **Where:** `test_api`, `google` branch.
- **Problem:** the key travels as `?key=...`, which leaks into proxy/server logs.
- **Change:** drop the query param; send header `x-goog-api-key: <key>` instead.
- **Accept:** the request URL contains no key; a Google test still returns OK (or a normal HTTP error).

## F1 — Multi-server monitoring (main mandate)

**Goal:** the dashboard monitors many servers — servers running AI agents (openclaw users) and plain servers with no agents. The main page gets a **server selector**; picking a server loads every metric that applies to it. A **popup dialog** (`ui.dialog`) registers new servers and new AI agents entirely from the UI — no CLI, no env editing, no restart.

**Architecture (decided — follow it, don't redesign):** one file, two modes selected by env `DASHBOARD_MODE`:

- **`hub`** (default — current behavior preserved): local collector + full UI, plus the selector and remote fetching. Existing deployments keep working unchanged.
- **`agent`** (headless): runs `init_db` + `collector` and serves **only** a token-protected JSON API — no login page, no dashboard UI. Deployed by copying the same `dashboard.py` to each monitored server. Each agent keeps its own SQLite history locally; the hub fetches on demand (no central metric storage).

Hub→agent transport: HTTP JSON via `urllib` with `Authorization: Bearer <token>`, validated with `hmac.compare_digest`. Tokens are per-server random secrets (≥32 hex chars).

### F1.1 Data-source seam
- **Change:** introduce dispatch functions the UI calls for *any* server — e.g. `source_snapshot(server)`, `source_history(server, limit)`, `source_openclaw(server)`. For the local server they return exactly what the UI consumes today (`latest` copy, `fetch_history()`, `get_cron_jobs()/get_memories()/get_issues()`); for a remote server they GET the agent API and return the same shapes. Do not change the data shapes the UI already renders.
- **Constraint:** remote fetches are blocking `urllib` calls — the UI refresh handlers must become async and fetch via `run.io_bound(...)` with a short timeout (~3 s), so a dead agent never blocks the event loop.
- **Accept:** with only "local" selected, the page renders identically to before, but through the seam.

### F1.2 Agent mode + JSON API + token auth
- **Change:** when `DASHBOARD_MODE=agent`: require `DASHBOARD_AGENT_TOKEN` (refuse to start without it), skip registering `/` and `/login`, and register FastAPI routes on NiceGUI's `app`:
  - `GET /api/snapshot` → JSON-safe copy of `latest`
  - `GET /api/history?limit=720` → `fetch_history()` output
  - `GET /api/openclaw` → `{"cron": ..., "memories": ..., "issues": ...}`
  - `POST /api/apitest` body `{"provider": ..., "model": ...}` → `test_api()` result (existing per-model cooldown enforced here, agent-side)
  - `POST /api/speedtest` → `do_speedtest()` result or the rate-limit message (existing 1 h cooldown enforced here)
  - `GET /api/ping` → `{"ok": true, "mode": "agent", "name": ...}` (used by the add-server dialog to validate)
  - Every route checks the Bearer token; wrong/missing token → 401.
  - Hub mode also serves these routes (so a hub can be monitored by another hub) — but only when `DASHBOARD_AGENT_TOKEN` is set.
- **Accept:** `curl -H "Authorization: Bearer $T" :8080/api/snapshot` returns JSON; no/wrong token → 401; in agent mode `/` returns 404.

### F1.3 Server registry
- **Change:** persist registered servers in `DATA_DIR/servers.json`, file mode 0600 (it contains tokens). Shape:
  ```json
  {"servers": [{"name": "web-1", "url": "http://10.0.0.5:8080", "token": "<hex>", "agents": ["hermes"]}]}
  ```
  Helpers `load_servers()` / `save_servers()`. The local server is an implicit first entry named `local` whose `agents` default to `DASHBOARD_USERS` (env stays as backward-compatible default; an `agents` override for `local` stored in the JSON wins). Enforce unique names; URLs must be http(s).
- **Accept:** registry survives restart; malformed JSON logs an error and falls back to local-only instead of crashing.

### F1.4 Server selector on the main page
- **Change:** at the top of `main_page`, a `ui.select` listing `local` + registered servers; persist the choice per browser session in `app.storage.user['server']`. On change, refresh every section from the selected source. Show only what applies:
  - System sections (CPU/RAM, network, throughput, memory/disk, top processes, security logins) — always.
  - Openclaw sections (cron, memories, issues) — only when the selected server has a non-empty `agents` list.
  - API test / speed test cards — local always; remote only when the agent API is reachable.
  - An unreachable agent renders a visible inline error in the affected cards (e.g. "agent unreachable: <err>") — never an unhandled exception, never a blank page.
- **Accept:** switching servers swaps charts and cards without reload; a plain server (no agents) shows no openclaw cards; killing a remote agent degrades to inline errors.

### F1.5 "Add server / Add AI agent" dialog (the popup menu)
- **Change:** a button in the header (e.g. ⚙ "Manage servers") opens a `ui.dialog` with:
  - **Add server:** fields name, base URL, token, optional comma-separated agent users. On save: call that agent's `GET /api/ping` with the token; only persist to `servers.json` if it validates. The selector updates immediately, no restart.
  - **Add AI agent:** pick an existing server (including `local`) + username; appends to that server's `agents` list in `servers.json`.
  - **Remove server:** list with a delete button per entry + confirm step.
  - Validation failures (bad URL, bad token, duplicate name, empty fields) show an inline error and persist nothing.
- **Accept:** the full add-server and add-agent flows work from the UI with zero CLI involvement; invalid input never mutates `servers.json`.

### F1.6 Remote actions
- **Change:** the API test and speed test buttons act on the *selected* server: local keeps today's path; remote POSTs to the agent's `/api/apitest` / `/api/speedtest` via `run.io_bound`. Cooldown/ratelimit messages returned by the agent are shown as-is.
- **Accept:** pressing Test with a remote server selected runs the probe *on that server* (its users' API keys), and the latency/result renders in the card.

### F1.7 Documentation update
- **Change:** update `README.md` (features; an "agent mode" install section with a systemd unit variant; config table rows for `DASHBOARD_MODE`, `DASHBOARD_AGENT_TOKEN`; `servers.json` description; remove the "Planned" marker from the multi-server roadmap note), `CLAUDE.md` (code map rows for the new sections, new invariants), and `.env.example` (new vars). 
- **Security notes to include:** tokens are secrets (`servers.json` is 0600); run agents on private interfaces or behind a firewall; hub→agent traffic is plain HTTP unless proxied — recommend LAN/VPN or a TLS reverse proxy.
- **Accept:** docs match the implemented behavior; no doc claims a feature that doesn't exist.

## P2 — Robustness

### P2.1 Configurable network interface
- **Where:** `get_sys_info`, network totals block.
- **Problem:** only `eth0:`/`ens3:` match; machines with `enp0s3`, `wlan0`, etc. report 0 / "Error". Matters more once F1 deploys agents to heterogeneous servers.
- **Change:** env `DASHBOARD_NET_IFACE` (default `auto`): on `auto`, pick the first non-`lo` interface in `/proc/net/dev` with nonzero rx bytes; otherwise match the configured name.
- **Accept:** throughput works unconfigured on a non-eth0 NIC; the env var overrides auto-detection.

### P2.2 Safe SQLite handling
- **Where:** `init_db`, `fetch_history`, `collector`, `do_speedtest`.
- **Problem:** connections leak when an `execute` raises (no try/finally); concurrent reader timers + the writer thread can hit `database is locked`.
- **Change:** wrap every connection in `contextlib.closing(...)` (or try/finally); enable WAL once in `init_db` (`PRAGMA journal_mode=WAL`).
- **Accept:** every connect path closes on error; `PRAGMA journal_mode` returns `wal`.

### P2.3 Close cooldown races
- **Where:** `run_speed_test` + `do_speedtest` globals; `run_api_test` + `apitest_times`.
- **Problem:** cooldown state is checked and set non-atomically across clients; `last_speedtest_time` is set only after success, so two near-simultaneous clicks start two ~20 s speedtests.
- **Change:** module-level `threading.Lock`; check-and-set the timestamp inside the lock *before* starting the work; roll back on failure so a retry is allowed. (After F1.2 these cooldowns also guard the POST endpoints — same lock.)
- **Accept:** two immediate clicks (or two tabs) produce one run; the second gets the rate-limit message.

### P2.4 Fix collector cadence drift
- **Where:** `collector` loop.
- **Problem:** `time.sleep(COLLECT_INTERVAL)` runs after a ~4 s blocking sample → real interval ~19–20 s; the 720-sample window is ~4 h, not the documented 3 h.
- **Change:** sleep `max(0, COLLECT_INTERVAL - elapsed)`.
- **Accept:** consecutive `metrics` timestamps ~15 s apart; README's window claim becomes accurate.

## P3 — Improvements (optional, in value order)

### P3.1 Memories fallback
`get_memories`: when today's file is missing, fall back to the newest `*.md` in `workspace/memory/`, labeled with its date, instead of "No recent memories."

### P3.2 Refresh MODEL_OPTIONS
Current IDs are valid but aging. Rules: exact provider model IDs only — never invent or date-suffix them. Anthropic (valid as of 2026-07): may add `claude-opus-5` and `claude-sonnet-5`; keep `claude-haiku-4-5-20251001`. Google/Moonshot: verify against their official docs first. Optionally support override env `DASHBOARD_MODELS` (JSON, same shape as `MODEL_OPTIONS`).

### P3.3 Pin dependency
`requirements.txt`: `nicegui>=2.0,<3`.

### P3.4 /health endpoint
Unauthenticated `GET /health` → 200 + `{"status": "ok", "last_sample_age_s": ...}` from `latest`, for uptime monitors. Read-only, secret-free, available in both modes.

## Out of scope (decided — do not do)

- No test framework, no multi-file refactor, no Docker, no provider SDKs, no central metrics database. The deployment story stays "copy one file".
- TLS/HTTPS stays a reverse-proxy concern (documented in README), not in-app.
- No push-based metrics or message queues — the hub pulls from agents on demand.
