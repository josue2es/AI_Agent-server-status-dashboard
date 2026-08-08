#!/usr/bin/env python3
"""AI agents status dashboard.

NiceGUI rewrite. Monitors this server and any number of remote ones. A
'hub' serves the dashboard UI; a 'node' is the same file running headless
on a monitored server, exposing a token-protected JSON API the hub pulls
from. Agent data (cron jobs, memories, issues, auth profiles, model
config) is read from each configured user's openclaw workspace.
"""

import contextlib
import hashlib
import hmac
import json
import os
import secrets
import shutil
import socket
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, RedirectResponse
from nicegui import app, run, ui

def env_list(name, default):
    """Comma-separated env var as a clean list.

    Whitespace and empty entries are dropped, so 'a, b' and a stray trailing
    comma don't turn into lookups for ' b' or ''.
    """
    return [item.strip() for item in os.environ.get(name, default).split(',') if item.strip()]


# Users whose agent workspaces are aggregated (cron jobs, memories, issues).
AGENT_USERS = env_list('DASHBOARD_USERS', 'hermes,openclaw')

# Deployment mode. 'hub' serves the dashboard UI and pulls from registered
# servers; 'node' is headless — collector plus the JSON API only.
MODE = os.environ.get('DASHBOARD_MODE', 'hub').strip().lower()
# Shared secret a hub presents to read this server's API. Required in node
# mode; setting it in hub mode also exposes the API, so one hub can be
# monitored by another.
NODE_TOKEN = os.environ.get('DASHBOARD_NODE_TOKEN', '').strip()
NODE_NAME = os.environ.get('DASHBOARD_NODE_NAME', '').strip() or socket.gethostname()

PORT = int(os.environ.get('DASHBOARD_PORT', '8080'))
DATA_DIR = os.environ.get('DASHBOARD_DATA_DIR', '/var/lib/ai-agents-dashboard')
DB_PATH = os.path.join(DATA_DIR, 'dashboard.db')
SERVERS_PATH = os.path.join(DATA_DIR, 'servers.json')
# Speed-test CLI. Empty (the default) means auto-detect: the Ookla package
# installs itself as 'speedtest', not 'speedtest-ookla', so a hard-coded name
# was wrong on most installs. SPEEDTEST_BIN is the pre-DASHBOARD_ spelling,
# still honoured so existing units keep working.
SPEEDTEST_BIN = (os.environ.get('DASHBOARD_SPEEDTEST_BIN')
                 or os.environ.get('SPEEDTEST_BIN', '')).strip()
# Candidate filenames (in order) for an agent's model/profile config, tried both
# directly under the agent dir and under its 'agent/' subdir. Override with
# DASHBOARD_MODEL_CONFIG (comma-separated).
MODEL_CONFIG_FILES = env_list(
    'DASHBOARD_MODEL_CONFIG', 'models.json,profiles.json,model-profiles.json,config.json')
# Filesystem to report disk usage for. In a container, set this to a bind mount
# of the host root (e.g. /host) so the panel shows the server's disk, not the overlay.
DISK_PATH = os.environ.get('DASHBOARD_DISK_PATH', '/')
# Network interface to measure throughput on. 'auto' picks the first
# non-loopback interface that has received traffic, which covers eth0, ens3,
# enp0s3, wlan0 and friends without configuration.
NET_IFACE = os.environ.get('DASHBOARD_NET_IFACE', 'auto').strip() or 'auto'
LOCAL_TZ = ZoneInfo(os.environ.get('DASHBOARD_TZ', 'America/El_Salvador'))

# Login password hash (sha256 of the actual password). Deliberately no default
# in the source: when DASHBOARD_PASSWORD_HASH is unset, load_password_hash()
# mints a random password on first start and persists only its hash.
PASSWORD_HASH = os.environ.get('DASHBOARD_PASSWORD_HASH', '').strip()

APITEST_COOLDOWN = 30  # seconds between tests per model
SPEEDTEST_COOLDOWN = 3600  # seconds between speed tests
COLLECT_INTERVAL = 15  # seconds between metric samples
DISPLAY_POINTS = 720  # 3 hours of 15s samples shown on charts
REMOTE_TIMEOUT = 4  # seconds for a hub -> node data call
REMOTE_ACTION_TIMEOUT = 90  # seconds for a hub -> node action (API / speed test)

# Exact provider model IDs only — never construct or date-suffix one. The
# Google and Moonshot lists are left as-is: they were not re-verified against
# those providers' docs, and a wrong ID here is a 404 at test time.
MODEL_OPTIONS = {
    'anthropic': ['claude-opus-5', 'claude-sonnet-5', 'claude-haiku-4-5-20251001',
                  'claude-opus-4-6', 'claude-sonnet-4-6'],
    'google': ['gemini-3.1-pro-preview', 'gemini-3.1-flash-lite-preview'],
    'moonshot': ['kimi-k2.5', 'moonshot-v1-8k', 'moonshot-v1-32k'],
}

apitest_times = {}  # (provider, model) -> last test timestamp
last_speedtest_time = 0.0
cached_speedtest_result = 'Not run yet.'
# Cooldown state is shared by every connected client and by the node API,
# so claiming a slot has to be atomic.
cooldown_lock = threading.Lock()

latest = {}  # most recent system snapshot, written by the collector thread
latest_lock = threading.Lock()


def user_path(user, *parts):
    """Path inside a given user's openclaw directory, e.g. /home/hermes/.openclaw/..."""
    return os.path.join('/home', user, '.openclaw', *parts)


# ---------------------------------------------------------------- database

def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    with contextlib.closing(sqlite3.connect(DB_PATH)) as conn:
        # WAL lets the UI and API read while the collector thread writes.
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('''CREATE TABLE IF NOT EXISTS metrics
                        (timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                         cpu REAL, ram REAL, rx REAL, tx REAL, ping REAL, jitter REAL, loss REAL)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS speedtests
                        (timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                         download REAL, upload REAL, ping REAL)''')
        conn.commit()


def fetch_history(limit=DISPLAY_POINTS):
    """Most recent metric samples as parallel lists ready for the charts.

    Timestamps are stored in UTC by SQLite and converted to LOCAL_TZ here.
    """
    with contextlib.closing(sqlite3.connect(DB_PATH)) as conn:
        rows = conn.execute(
            'SELECT timestamp, cpu, ram, rx, tx, ping, jitter, loss '
            'FROM metrics ORDER BY timestamp DESC LIMIT ?', (limit,)).fetchall()
    rows.reverse()
    labels = []
    for r in rows:
        ts = datetime.strptime(r[0], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
        labels.append(ts.astimezone(LOCAL_TZ).strftime('%H:%M:%S'))
    cols = list(zip(*rows)) if rows else [[]] * 8
    return {
        'labels': labels,
        'cpu': list(cols[1]), 'ram': list(cols[2]),
        'rx': list(cols[3]), 'tx': list(cols[4]),
        'ping': list(cols[5]), 'jitter': list(cols[6]), 'loss': list(cols[7]),
    }


# --------------------------------------------------------- metric collector

def read_net_counters():
    """(rx_bytes, tx_bytes) for the monitored interface, from /proc/net/dev.

    Raises when nothing matches, so the caller reports it rather than
    silently charting zeroes.
    """
    with open('/proc/net/dev') as f:
        lines = f.readlines()
    for line in lines:
        name, sep, rest = line.partition(':')
        name = name.strip()
        if not sep or name == 'lo':
            continue
        fields = rest.split()
        rx, tx = int(fields[0]), int(fields[8])
        if NET_IFACE != 'auto':
            if name == NET_IFACE:
                return rx, tx
        elif rx > 0:
            return rx, tx
    raise RuntimeError(f'no usable network interface (DASHBOARD_NET_IFACE={NET_IFACE})')


def get_sys_info():
    """One snapshot of system stats, read from /proc and standard CLI tools.

    Blocking (the ping alone takes ~4s), so this only runs in the collector
    thread — pages render from the shared `latest` dict instead.
    """
    info = {}

    # CPU raw ticks (percentage is computed from deltas between samples)
    try:
        with open('/proc/stat') as f:
            for line in f:
                if line.startswith('cpu '):
                    parts = line.split()
                    idle = float(parts[4]) + float(parts[5])
                    non_idle = sum(float(parts[i]) for i in (1, 2, 3, 6, 7, 8))
                    info['cpu_raw'] = {'idle': idle, 'total': idle + non_idle}
                    break
    except Exception:
        info['cpu_raw'] = {'idle': 0, 'total': 0}

    # RAM
    try:
        with open('/proc/meminfo') as f:
            mem = {}
            for line in f:
                parts = line.split(':')
                mem[parts[0]] = int(parts[1].strip().split()[0])
            total = mem.get('MemTotal', 1)
            free = mem.get('MemAvailable', mem.get('MemFree', 0))
            used = total - free
            info['ram_percent'] = round((used / total) * 100, 1)
            info['ram'] = f"{used/1024/1024:.2f} GB / {total/1024/1024:.2f} GB ({info['ram_percent']:.1f}%)"
    except Exception:
        info['ram'] = 'Error'
        info['ram_percent'] = 0

    # Disk
    try:
        total, used, _free = shutil.disk_usage(DISK_PATH)
        info['disk'] = f"{used/(1024**3):.2f} GB / {total/(1024**3):.2f} GB ({(used/total)*100:.1f}%)"
    except Exception:
        info['disk'] = 'Error fetching disk space'

    # Top processes
    try:
        top = subprocess.check_output(['ps', '-eo', 'pid,user,%cpu,%mem,comm', '--sort=-%cpu'], text=True)
        info['top_apps'] = '\n'.join(top.split('\n')[:10])
    except Exception:
        info['top_apps'] = 'Error fetching processes'

    # Network totals
    try:
        rx, tx = read_net_counters()
        info['net_rx'] = f"{rx/1024/1024:.2f} MB"
        info['net_tx'] = f"{tx/1024/1024:.2f} MB"
        info['net_rx_bytes'] = rx
        info['net_tx_bytes'] = tx
    except Exception:
        info['net_rx'] = 'Error'
        info['net_tx'] = 'Error'
        info['net_rx_bytes'] = 0
        info['net_tx_bytes'] = 0

    # Ping / jitter / packet loss
    try:
        out = subprocess.check_output(['ping', '-c', '5', '-W', '1', '8.8.8.8'], text=True)
        stats = 'Ping failed'
        avg_ping = jitter = packet_loss = 0.0
        for line in out.splitlines():
            if 'packets transmitted' in line:
                packet_loss = float(line.split(', ')[2].split('%')[0])
            if 'min/avg/max' in line:
                stats = line.strip()
                parts = line.split('=')[1].split('/')
                avg_ping = float(parts[1])
                jitter = float(parts[3].split(' ')[0])
        info['ping'] = f"Google DNS (8.8.8.8): {stats}"
        info['ping_avg'] = avg_ping
        info['jitter'] = jitter
        info['packet_loss'] = packet_loss
    except Exception:
        info['ping'] = 'Error running ping'
        info['ping_avg'] = 0.0
        info['jitter'] = 0.0
        info['packet_loss'] = 100.0

    # Security (last logins)
    try:
        info['security'] = subprocess.check_output(['last', '-n', '5'], text=True)
    except Exception:
        try:
            info['security'] = 'Recent active sessions:\n' + subprocess.check_output(['who'], text=True)
        except Exception:
            info['security'] = 'Cannot read login logs.'

    return info


def collector():
    """Background thread: samples system metrics every 15s into SQLite."""
    last_rx = last_tx = last_cpu_idle = last_cpu_total = last_time = None
    while True:
        started = time.monotonic()
        try:
            data = get_sys_info()
            now = time.time()

            if last_rx is not None and now > last_time:
                diff = now - last_time
                rx_bps = (data['net_rx_bytes'] - last_rx) / diff
                tx_bps = (data['net_tx_bytes'] - last_tx) / diff

                idle_diff = data['cpu_raw']['idle'] - last_cpu_idle
                total_diff = data['cpu_raw']['total'] - last_cpu_total
                cpu_pct = ((total_diff - idle_diff) / total_diff) * 100.0 if total_diff > 0 else 0.0

                with contextlib.closing(sqlite3.connect(DB_PATH)) as conn:
                    conn.execute(
                        'INSERT INTO metrics (cpu, ram, rx, tx, ping, jitter, loss) '
                        'VALUES (?, ?, ?, ?, ?, ?, ?)',
                        (round(cpu_pct, 1), data.get('ram_percent', 0),
                         round(rx_bps, 2), round(tx_bps, 2),
                         data.get('ping_avg', 0), data.get('jitter', 0),
                         data.get('packet_loss', 0)))
                    conn.execute('DELETE FROM metrics WHERE timestamp < datetime("now", "-24 hours")')
                    conn.commit()

            last_rx = data['net_rx_bytes']
            last_tx = data['net_tx_bytes']
            last_cpu_idle = data['cpu_raw']['idle']
            last_cpu_total = data['cpu_raw']['total']
            last_time = now

            with latest_lock:
                latest.clear()
                latest.update(data)
                latest['sampled_at'] = time.time()  # drives /health
        except Exception as e:
            print(f"Collector error: {e}")
        # Sleep the remainder of the interval, not a full interval on top of a
        # sample that already took ~4s on the ping — otherwise the real cadence
        # is ~19s and the 720-point window spans 4h instead of the stated 3h.
        time.sleep(max(0.0, COLLECT_INTERVAL - (time.monotonic() - started)))


# -------------------------------------------------- multi-user agent data

def agent_workspaces(users=None):
    """The subset of the given users that actually has an openclaw workspace.

    Configuring a username doesn't make a server an agent host: DASHBOARD_USERS
    carries a default, so every server claims 'hermes, openclaw' until told
    otherwise. An existing ~/.openclaw is the evidence, and the UI hides the
    agent panels when this comes back empty.
    """
    return [user for user in (AGENT_USERS if users is None else users)
            if os.path.isdir(user_path(user))]


def get_cron_jobs(users=None):
    """Enabled cron jobs across the given agent users (default: AGENT_USERS)."""
    jobs = []
    for user in (AGENT_USERS if users is None else users):
        path = user_path(user, 'cron', 'jobs.json')
        try:
            with open(path) as f:
                data = json.load(f)
            for job in data.get('jobs', []):
                if job.get('enabled', False):
                    next_run = job.get('state', {}).get('nextRunAtMs', 0)
                    jobs.append({
                        'user': user,
                        'name': job.get('name', 'unnamed'),
                        'desc': job.get('payload', {}).get('message', 'No description'),
                        'next': datetime.fromtimestamp(next_run / 1000, tz=LOCAL_TZ)
                                .strftime('%Y-%m-%d %H:%M:%S') if next_run else 'N/A',
                    })
        except FileNotFoundError:
            continue
        except Exception as e:
            jobs.append({'user': user, 'name': 'Error', 'desc': str(e), 'next': 'N/A'})
    jobs.sort(key=lambda j: j['next'])
    return jobs


def _latest_memory_file(memory_dir, today):
    """(path, date) of today's memory file, else the newest one there.

    An agent that hasn't written yet today still has history worth showing,
    so fall back rather than claiming there are no memories at all.
    """
    todays = os.path.join(memory_dir, f'{today}.md')
    if os.path.exists(todays):
        return todays, today
    try:
        names = sorted(n for n in os.listdir(memory_dir) if n.endswith('.md'))
    except Exception:
        return None, None
    if not names:
        return None, None
    return os.path.join(memory_dir, names[-1]), names[-1][:-3]


def get_memories(users=None):
    """Last 5 memory entries per agent user, from today's file or the newest."""
    memories = {}
    today = datetime.now(LOCAL_TZ).strftime('%Y-%m-%d')
    for user in (AGENT_USERS if users is None else users):
        path, day = _latest_memory_file(user_path(user, 'workspace', 'memory'), today)
        if not path:
            memories[user] = ['No recent memories.']
            continue
        try:
            with open(path) as f:
                lines = [l.strip() for l in f if l.strip().startswith('-')]
        except Exception:
            memories[user] = ['No recent memories.']
            continue
        # Label anything older than today so it can't be mistaken for current.
        stale = '' if day == today else f' (from {day})'
        memories[user] = [f'{line}{stale}' for line in lines[-5:]] or ['No recent memories.']
    return memories


def get_issues(users=None):
    """Active and resolved issues across the given agent users, tagged by user."""
    issues = {'active': [], 'fixed': []}
    for user in (AGENT_USERS if users is None else users):
        path = user_path(user, 'workspace', 'ISSUES.md')
        try:
            with open(path) as f:
                section = None
                for line in f:
                    if '## Active Issues' in line:
                        section = 'active'
                    elif '## Resolved Issues' in line:
                        section = 'fixed'
                    elif line.strip().startswith('-') and section:
                        issues[section].append({'user': user, 'text': line.strip()[2:]})
        except Exception:
            continue
    return issues


def find_api_key(provider):
    """Search every user's auth profiles for a key for the given provider."""
    for user in AGENT_USERS:
        path = user_path(user, 'agents', 'main', 'agent', 'auth-profiles.json')
        try:
            with open(path) as f:
                profiles = json.load(f)
            for p in profiles.get('profiles', {}).values():
                if p.get('provider') == provider and p.get('key'):
                    return p['key']
        except Exception:
            continue
    return None


# --------------------------------------------------- model / profile config

def _fmt_model(entry):
    """Format a model reference as 'provider / model', from a dict or string."""
    if isinstance(entry, str):
        s = entry.strip()
        if '/' in s:
            provider, _, model = s.partition('/')
            return f'{provider.strip()} / {model.strip()}'
        return s
    if isinstance(entry, dict):
        provider = str(entry.get('provider') or entry.get('vendor') or '').strip()
        model = str(entry.get('model') or entry.get('name') or entry.get('id') or '').strip()
        if provider and model:
            return f'{provider} / {model}'
        # Provider field absent: the model may itself be a "provider/model" string.
        return _fmt_model(model) if model else (provider or '?')
    return '?'


def _parse_profile(name, cfg):
    """Normalize one profile into display fields: primary, reasoning, fallbacks[]."""
    if not isinstance(cfg, dict):
        return {'name': name, 'primary': _fmt_model(cfg), 'reasoning': '–', 'fallbacks': []}

    # Primary model: an explicit 'primary', or provider+model set on the profile itself.
    if isinstance(cfg.get('primary'), (dict, str)):
        primary = cfg['primary']
    elif 'provider' in cfg or 'model' in cfg:
        primary = {'provider': cfg.get('provider'), 'model': cfg.get('model')}
    else:
        primary = cfg.get('model', '')

    reasoning = (cfg.get('reasoning') or cfg.get('reasoningEffort')
                 or cfg.get('reasoning_effort') or cfg.get('effort'))
    reasoning = str(reasoning) if reasoning else '–'

    raw = cfg.get('fallbacks') or cfg.get('fallback') or []
    if isinstance(raw, (dict, str)):
        raw = [raw]
    fallbacks = [_fmt_model(fb) for fb in raw]

    return {'name': name, 'primary': _fmt_model(primary),
            'reasoning': reasoning, 'fallbacks': fallbacks}


def _looks_like_profile(v):
    return isinstance(v, dict) and any(
        k in v for k in ('primary', 'model', 'provider', 'fallbacks', 'fallback'))


def _load_profiles(cfg):
    """Pull a {name: profile} mapping out of a loaded config file, tolerantly."""
    if not isinstance(cfg, dict):
        return []
    profiles = cfg.get('profiles')
    if not isinstance(profiles, dict):
        # No 'profiles' key: only treat the top level as the map if its values
        # actually look like profiles (avoids misreading an unrelated config.json).
        if cfg and all(_looks_like_profile(v) for v in cfg.values()):
            profiles = cfg
        else:
            return []
    return [_parse_profile(name, pcfg) for name, pcfg in profiles.items()]


def get_model_config(users=None):
    """Model assignments for every agent of every given user.

    Returns [{'user', 'agent', 'profiles': [...]}], one entry per agent that has
    a readable config. Each agent's config is the first match from
    MODEL_CONFIG_FILES, looked up under the agent dir and its 'agent/' subdir
    (mirroring where auth-profiles.json lives).
    """
    result = []
    for user in (AGENT_USERS if users is None else users):
        agents_dir = user_path(user, 'agents')
        try:
            agents = sorted(a for a in os.listdir(agents_dir)
                            if os.path.isdir(os.path.join(agents_dir, a)))
        except Exception:
            continue
        for agent in agents:
            cfg = None
            for sub in ('', 'agent'):
                base = os.path.join(agents_dir, agent, sub)
                for fname in MODEL_CONFIG_FILES:
                    try:
                        with open(os.path.join(base, fname)) as f:
                            cfg = json.load(f)
                        break
                    except FileNotFoundError:
                        continue
                    except Exception:
                        cfg = None
                        break
                if cfg is not None:
                    break
            profiles = _load_profiles(cfg) if cfg is not None else []
            if profiles:
                result.append({'user': user, 'agent': agent, 'profiles': profiles})
    return result


# ------------------------------------------------------------ API test

def test_api(provider, model):
    """Send a minimal one-shot prompt to the provider and measure latency.

    Blocking (urllib); call via run.io_bound from the UI.
    """
    start = time.time()
    try:
        key = find_api_key(provider)
        if not key:
            return {'status': 'error', 'message': f"No API key configured for provider '{provider}'"}

        if provider == 'anthropic':
            payload = json.dumps({
                'model': model,
                'max_tokens': 10,
                'messages': [{'role': 'user', 'content': 'Reply with just: OK'}],
            }).encode()
            req = urllib.request.Request(
                'https://api.anthropic.com/v1/messages',
                data=payload,
                headers={'Content-Type': 'application/json', 'x-api-key': key,
                         'anthropic-version': '2023-06-01'})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
                reply = data.get('content', [{}])[0].get('text', '').strip()

        elif provider == 'google':
            payload = json.dumps({
                'contents': [{'parts': [{'text': 'Reply with just: OK'}]}],
                'generationConfig': {'maxOutputTokens': 10},
            }).encode()
            # Key goes in a header, never the URL — query strings land in proxy logs.
            url = f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent'
            req = urllib.request.Request(
                url, data=payload,
                headers={'Content-Type': 'application/json', 'x-goog-api-key': key})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
                reply = data.get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', '').strip()

        elif provider == 'moonshot':
            payload = json.dumps({
                'model': model,
                'max_tokens': 10,
                'messages': [{'role': 'user', 'content': 'Reply with just: OK'}],
            }).encode()
            req = urllib.request.Request(
                'https://api.moonshot.ai/v1/chat/completions',
                data=payload,
                headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {key}'})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
                reply = data.get('choices', [{}])[0].get('message', {}).get('content', '').strip()

        else:
            return {'status': 'error', 'message': f'Unknown provider: {provider}'}

        return {'status': 'ok', 'reply': reply, 'latency_ms': round((time.time() - start) * 1000)}

    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            err = json.loads(body)
            msg = (err.get('error', {}).get('message')
                   or err.get('error', {}).get('type')
                   or body[:400])
        except Exception:
            msg = body[:400]
        return {'status': 'error', 'message': f'HTTP {e.code}: {msg}'}
    except Exception as e:
        return {'status': 'error', 'message': str(e)}


# ------------------------------------------------------------ cooldowns

def claim_apitest(provider, model):
    """Reserve the per-model API-test slot, or return the wait message.

    Check-and-set happens under one lock, so two clients clicking at the
    same moment can't both get through.
    """
    with cooldown_lock:
        now = time.time()
        wait = APITEST_COOLDOWN - (now - apitest_times.get((provider, model), 0))
        if wait > 0:
            return f'Please wait {int(wait) + 1}s before testing {model} again.'
        apitest_times[(provider, model)] = now
        return None


def claim_speedtest():
    """Reserve the speed-test slot, or return the rate-limit message.

    Claimed *before* the ~20 s run rather than after it, so a second click
    while the first test is still running is refused instead of starting a
    second one.
    """
    global last_speedtest_time
    with cooldown_lock:
        now = time.time()
        if last_speedtest_time and now - last_speedtest_time < SPEEDTEST_COOLDOWN:
            mins = int((SPEEDTEST_COOLDOWN - (now - last_speedtest_time)) // 60) + 1
            return (f'Rate limited. Try again in {mins} minutes.\n\n'
                    f'Last result:\n{cached_speedtest_result}')
        last_speedtest_time = now
        return None


def release_speedtest():
    """Undo a claim whose run never produced a result, so the next attempt
    isn't locked out for an hour by a test that never happened."""
    global last_speedtest_time
    with cooldown_lock:
        last_speedtest_time = 0.0


# ------------------------------------------------------------ speed test

# Names tried, in order, when no binary is configured. Ookla's own package
# installs 'speedtest'; a few distros ship it as 'speedtest-ookla'. The
# absolute paths cover snap, whose /snap/bin is absent from systemd's PATH.
SPEEDTEST_CANDIDATES = ['speedtest', 'speedtest-ookla', 'speedtest-cli',
                        '/usr/bin/speedtest', '/usr/local/bin/speedtest',
                        '/snap/bin/speedtest']
SPEEDTEST_TIMEOUT = 75  # seconds; under REMOTE_ACTION_TIMEOUT so a hub sees the error
SPEEDTEST_INSTALL_HINT = ('install the Ookla CLI (https://www.speedtest.net/apps/cli) '
                          'or run "apt install speedtest-cli"')


def speedtest_flavor(path):
    """'ookla' or 'python' — the two CLIs share names but not flags or JSON.

    Ookla's --version mentions Ookla; the Python clone (sivel/speedtest-cli,
    what 'apt install speedtest-cli' gives you) prints 'speedtest-cli 2.x'.
    It also installs a 'speedtest' entry point, so the name alone proves
    nothing. Anything unrecognised is treated as Ookla, the documented one.
    """
    try:
        probe = subprocess.run([path, '--version'], capture_output=True, text=True, timeout=15)
    except Exception:
        return 'ookla'
    text = f'{probe.stdout} {probe.stderr}'.lower()
    return 'python' if 'speedtest-cli' in text and 'ookla' not in text else 'ookla'


def find_speedtest():
    """Locate the speed-test CLI; returns (path, flavor). Raises with an install hint.

    Resolved per run rather than at import, so installing the CLI doesn't
    require restarting the service.
    """
    if SPEEDTEST_BIN:
        path = shutil.which(SPEEDTEST_BIN)  # also accepts an absolute path
        if not path:
            raise Exception(f"configured speed-test CLI '{SPEEDTEST_BIN}' not found — "
                            f'fix DASHBOARD_SPEEDTEST_BIN, or {SPEEDTEST_INSTALL_HINT}')
        return path, speedtest_flavor(path)
    for name in SPEEDTEST_CANDIDATES:
        path = shutil.which(name)
        if path:
            return path, speedtest_flavor(path)
    raise Exception(f'no speed-test CLI on PATH — {SPEEDTEST_INSTALL_HINT}, '
                    'or set DASHBOARD_SPEEDTEST_BIN to its path')


def do_speedtest():
    """Run the speed-test CLI (~20s, blocking — call via run.io_bound) and record the result."""
    global last_speedtest_time, cached_speedtest_result
    path, flavor = find_speedtest()
    args = ([path, '--accept-license', '--accept-gdpr', '-f', 'json'] if flavor == 'ookla'
            else [path, '--json', '--secure'])
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=SPEEDTEST_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise Exception(f'{path} did not finish within {SPEEDTEST_TIMEOUT}s')
    if result.returncode != 0:
        raise Exception(result.stderr.strip() or result.stdout.strip()
                        or f'{path} exited with {result.returncode}')
    out = result.stdout
    try:
        # Either CLI may print warnings before the JSON
        data = json.loads(out[out.find('{'):] if '{' in out else out)
    except ValueError:
        # A captive portal or a proxy's error page reaches us as an exit-0 non-JSON
        # body; show what it actually said instead of a bare JSONDecodeError.
        noise = (out or result.stderr).strip()
        raise Exception(f'{path} returned no JSON: {noise[:200] if noise else "no output"}')
    srv = data.get('server', {})
    if flavor == 'ookla':
        ping = data.get('ping', {}).get('latency', 0)
        down_mbps = data.get('download', {}).get('bandwidth', 0) * 8 / 1000000  # bytes/s
        up_mbps = data.get('upload', {}).get('bandwidth', 0) * 8 / 1000000
        server = srv.get('name', 'Unknown')
        location = srv.get('location', 'Unknown')
    else:
        ping = data.get('ping', 0)
        down_mbps = data.get('download', 0) / 1000000  # already bits/s
        up_mbps = data.get('upload', 0) / 1000000
        server = srv.get('sponsor', 'Unknown')
        location = srv.get('name', 'Unknown')

    cached_speedtest_result = (f"Ping: {ping:.2f} ms\nDownload: {down_mbps:.2f} Mbps\n"
                               f"Upload: {up_mbps:.2f} Mbps\nServer: {server} ({location})")
    # Re-stamp: the cooldown runs from when the test finished, not when it started.
    with cooldown_lock:
        last_speedtest_time = time.time()

    with contextlib.closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute('INSERT INTO speedtests (download, upload, ping) VALUES (?, ?, ?)',
                     (down_mbps, up_mbps, ping))
        conn.commit()
    return cached_speedtest_result


# ------------------------------------------------------- server registry

LOCAL_SERVER = 'local'  # reserved name for the machine the hub runs on


def load_servers():
    """Registered servers from servers.json. Never raises — a broken file
    degrades to local-only monitoring rather than taking the page down."""
    try:
        with open(SERVERS_PATH) as f:
            data = json.load(f)
        raw = data.get('servers', [])
        if not isinstance(raw, list):
            raise ValueError('"servers" must be a list')
    except FileNotFoundError:
        return []
    except Exception as e:
        print(f'servers.json unusable ({e}) — monitoring the local server only')
        return []
    servers = []
    for s in raw:
        if isinstance(s, dict) and s.get('name'):
            servers.append({
                'name': str(s['name']),
                'url': str(s.get('url', '')).rstrip('/'),
                'token': str(s.get('token', '')),
                'agents': [u for u in (str(a).strip() for a in s.get('agents') or []) if u],
            })
    return servers


def save_servers(servers):
    """Persist the registry with 0600 — it holds node tokens."""
    os.makedirs(DATA_DIR, exist_ok=True)
    fd = os.open(SERVERS_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump({'servers': servers}, f, indent=2)


def all_servers():
    """Local server first, then every registered remote.

    'local' is implicit: it needs no entry in servers.json, but storing one
    lets the UI override which agent users it monitors (DASHBOARD_USERS is
    only the default).
    """
    stored = load_servers()
    local = next((s for s in stored if s['name'] == LOCAL_SERVER), None)
    return [{'name': LOCAL_SERVER, 'url': '', 'token': '',
             'agents': (local['agents'] if local else None) or list(AGENT_USERS)}] \
        + [s for s in stored if s['name'] != LOCAL_SERVER]


def get_server(name):
    """Look up a server by name; unknown names fall back to local."""
    servers = all_servers()
    return next((s for s in servers if s['name'] == name), servers[0])


# ------------------------------------------------------------ data sources

def is_local(server):
    return server['name'] == LOCAL_SERVER


def _node_request(server, path, payload=None, params=None, timeout=REMOTE_TIMEOUT):
    """Call a node's JSON API. Raises on transport, HTTP, or auth failure."""
    url = server['url'] + path
    if params:
        url += '?' + urllib.parse.urlencode(params)
    headers = {'Authorization': f"Bearer {server['token']}"}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def source_problem(server):
    """How to label a failed read, so a local fault isn't blamed on the network."""
    return 'local read failed' if is_local(server) else 'node unreachable'


def source_snapshot(server):
    """Latest system snapshot for a server. Blocking for remotes."""
    if is_local(server):
        with latest_lock:
            return dict(latest)
    return _node_request(server, '/api/snapshot')


def source_history(server, limit=DISPLAY_POINTS):
    """Chart history for a server, in fetch_history()'s shape."""
    if is_local(server):
        return fetch_history(limit)
    return _node_request(server, '/api/history', params={'limit': limit})


def source_stats(server):
    """(snapshot, history) — one blocking call for the stats timer."""
    return source_snapshot(server), source_history(server)


def source_agentdata(server):
    """Cron jobs, memories, issues, and model config for a server's agents.

    'present' is the subset of those users that really has a workspace — what
    the UI hides the agent panels on.
    """
    users = server['agents']
    if is_local(server):
        return {'cron': get_cron_jobs(users), 'memories': get_memories(users),
                'issues': get_issues(users), 'models': get_model_config(users),
                'present': agent_workspaces(users)}
    # The hub decides which users to monitor; the node reads only those.
    return _node_request(server, '/api/agentdata', params={'users': ','.join(users)})


def remote_apitest(server, provider, model):
    """Run the API probe on a remote node — its keys, its cooldown."""
    try:
        return _node_request(server, '/api/apitest',
                             payload={'provider': provider, 'model': model},
                             timeout=REMOTE_ACTION_TIMEOUT)
    except Exception as e:
        return {'status': 'error', 'message': f'node unreachable: {e}'}


def remote_speedtest(server):
    """Run the speed test on a remote node; returns text ready to display."""
    try:
        res = _node_request(server, '/api/speedtest', payload={},
                            timeout=REMOTE_ACTION_TIMEOUT)
        return res.get('result') or 'No result.'
    except Exception as e:
        return f'node unreachable: {e}'


# --------------------------------------------------------------- node API

def _node_authorized(request):
    """Constant-time Bearer check against DASHBOARD_NODE_TOKEN."""
    scheme, _, token = request.headers.get('authorization', '').partition(' ')
    return (bool(NODE_TOKEN) and scheme.lower() == 'bearer'
            and hmac.compare_digest(token.strip(), NODE_TOKEN))


def _unauthorized():
    return JSONResponse({'error': 'unauthorized'}, status_code=401)


def _users_param(raw):
    """Parse a 'users' query value; None means 'this server's own list'."""
    users = [u.strip() for u in (raw or '').split(',') if u.strip()]
    return users or None


def register_health():
    """Unauthenticated liveness probe for external uptime monitors.

    Deliberately free of secrets and of anything about other servers — it
    reports only that this process is up and how fresh its own sample is.
    """

    @app.get('/health')
    def health():
        with latest_lock:
            stamp = latest.get('sampled_at')
        return {'status': 'ok', 'mode': MODE,
                'last_sample_age_s': round(time.time() - stamp, 1) if stamp else None}


def register_node_api():
    """Token-protected JSON API a hub pulls from.

    Registered whenever DASHBOARD_NODE_TOKEN is set, so a hub can be
    monitored by another hub as well.
    """

    @app.get('/api/ping')
    def api_ping(request: Request):
        """Cheap reachability + auth check used by the add-server dialog."""
        if not _node_authorized(request):
            return _unauthorized()
        return {'ok': True, 'mode': MODE, 'name': NODE_NAME,
                'agents': all_servers()[0]['agents']}

    @app.get('/api/snapshot')
    def api_snapshot(request: Request):
        if not _node_authorized(request):
            return _unauthorized()
        with latest_lock:
            return dict(latest)

    @app.get('/api/history')
    def api_history(request: Request, limit: int = DISPLAY_POINTS):
        if not _node_authorized(request):
            return _unauthorized()
        return fetch_history(max(1, min(limit, 5000)))

    @app.get('/api/agentdata')
    def api_agentdata(request: Request, users: str = ''):
        if not _node_authorized(request):
            return _unauthorized()
        who = _users_param(users)
        return {'cron': get_cron_jobs(who), 'memories': get_memories(who),
                'issues': get_issues(who), 'models': get_model_config(who),
                'present': agent_workspaces(who)}

    @app.post('/api/apitest')
    async def api_apitest(request: Request):
        """Blocking probe, so it runs in a worker thread, not the event loop."""
        if not _node_authorized(request):
            return _unauthorized()
        try:
            body = await request.json()
        except Exception:
            body = {}
        provider, model = str(body.get('provider', '')), str(body.get('model', ''))
        wait_msg = claim_apitest(provider, model)
        if wait_msg:
            return {'status': 'error', 'message': wait_msg}
        return await run_in_threadpool(test_api, provider, model)

    @app.post('/api/speedtest')
    async def api_speedtest(request: Request):
        if not _node_authorized(request):
            return _unauthorized()
        limited = claim_speedtest()
        if limited:
            return {'status': 'ratelimited', 'result': limited}
        try:
            return {'status': 'ok', 'result': await run_in_threadpool(do_speedtest)}
        except Exception as e:
            release_speedtest()  # the run failed; don't burn the hour
            return {'status': 'error', 'result': f'Speedtest failed: {e}'}


# ------------------------------------------------------------------ UI

AXIS_STYLE = {'axisLabel': {'color': '#888'}, 'splitLine': {'lineStyle': {'color': '#444'}}}
# JS formatter (runs in the browser) for the throughput chart's y-axis labels.
BYTES_FORMATTER = ('v => v >= 1048576 ? (v / 1048576).toFixed(1) + " MB/s" '
                   ': v >= 1024 ? (v / 1024).toFixed(1) + " KB/s" : v + " B/s"')


def make_chart(series, y_axis_extra=None):
    """Line chart with the dashboard's dark style. series = [(name, color), ...]"""
    y_axis = {'type': 'value', **AXIS_STYLE, **(y_axis_extra or {})}
    return ui.echart({
        'backgroundColor': 'transparent',
        'tooltip': {'trigger': 'axis'},
        'legend': {'textStyle': {'color': '#fff'}},
        'grid': {'left': 60, 'right': 20, 'top': 40, 'bottom': 30},
        'xAxis': {'type': 'category', 'data': [], **AXIS_STYLE},
        'yAxis': y_axis,
        'series': [{
            'name': name,
            'type': 'line',
            'smooth': True,
            'showSymbol': False,
            'color': color,
            'areaStyle': {'opacity': 0.2},
            'data': [],
        } for name, color in series],
    }).classes('w-full h-52')


def set_chart_data(chart, labels, *series_data):
    """Replace a chart's x-axis labels and series data (one list per series)."""
    chart.options['xAxis']['data'] = labels
    for i, data in enumerate(series_data):
        chart.options['series'][i]['data'] = data
    chart.update()


def section_card(title):
    """Dark card with an orange title — the basic building block of the page."""
    card = ui.card().classes('w-full bg-[#2d2d2d] border border-[#444]')
    with card:
        ui.label(title).classes('text-lg font-bold text-[#ff9900]')
    return card


def servers_dialog(on_saved):
    """Popup for registering, editing, and removing monitored servers.

    Adding walks the user through the info needed to reach the server, then
    asks whether it runs AI agents and collects their usernames if so. The
    entry is only persisted once the node answers /api/ping with its token.
    `on_saved` (async) runs after every change so the page can pick it up.
    """
    dialog = ui.dialog()
    with dialog, ui.card().classes('bg-[#2d2d2d] border border-[#444] w-[46rem] max-w-full'):
        ui.label('Manage servers').classes('text-lg font-bold text-[#ff9900]')
        listing = ui.column().classes('w-full gap-2')

        ui.separator()
        ui.label('Add a server').classes('text-sm font-bold text-[#ff9900]')
        with ui.row().classes('w-full gap-2 no-wrap'):
            name_in = ui.input('Name', placeholder='web-1').props('dark dense outlined').classes('flex-1')
            url_in = ui.input('Base URL', placeholder='http://10.0.0.5:8080') \
                .props('dark dense outlined').classes('flex-[2]')
        token_in = ui.input('Node token — DASHBOARD_NODE_TOKEN on that server', password=True) \
            .props('dark dense outlined').classes('w-full')
        agents_sw = ui.switch('This server runs AI agents to monitor (Hermes / Openclaw)')
        users_in = ui.input('Agent users, comma-separated', placeholder='hermes') \
            .props('dark dense outlined').classes('w-full')
        users_in.bind_visibility_from(agents_sw, 'value')
        feedback = ui.label('').classes('text-sm text-red-400 whitespace-pre-wrap')
        with ui.row().classes('w-full justify-end gap-2'):
            ui.button('Close', on_click=dialog.close).props('flat color=grey')
            add_btn = ui.button('Validate & add').props('color=orange')

    def fail(message):
        feedback.classes(replace='text-sm text-red-400 whitespace-pre-wrap')
        feedback.set_text(message)

    def parse_users(raw):
        return [u.strip() for u in (raw or '').split(',') if u.strip()]

    async def save_agents(name, raw):
        """Add or remove the agent users monitored on an already-known server."""
        stored = load_servers()
        entry = next((s for s in stored if s['name'] == name), None)
        if entry is None:
            # 'local' has no stored entry until its agent list is overridden.
            stored.append({'name': name, 'url': '', 'token': '', 'agents': parse_users(raw)})
        else:
            entry['agents'] = parse_users(raw)
        save_servers(stored)
        render_listing()
        ui.notify(f'Updated the agents monitored on {name}', color='orange')
        await on_saved()

    async def delete_server(name):
        confirm = ui.dialog()
        with confirm, ui.card().classes('bg-[#2d2d2d] border border-[#444]'):
            ui.label(f'Stop monitoring "{name}"?').classes('text-sm text-white')
            with ui.row().classes('w-full justify-end gap-2'):
                ui.button('Cancel', on_click=lambda: confirm.submit(False)).props('flat color=grey')
                ui.button('Remove', on_click=lambda: confirm.submit(True)).props('color=red')
        if await confirm:
            save_servers([s for s in load_servers() if s['name'] != name])
            render_listing()
            ui.notify(f'Removed {name}', color='orange')
            await on_saved()

    def render_listing():
        listing.clear()
        with listing:
            ui.label('Monitored servers').classes('text-sm font-bold text-[#ff9900]')
            for s in all_servers():
                with ui.row().classes('w-full items-center gap-2 no-wrap'):
                    ui.label(s['name']).classes('text-sm font-bold text-white w-24 shrink-0')
                    ui.label(s['url'] or 'this machine').classes('text-xs text-gray-400 flex-1')
                    field = ui.input(value=', '.join(s['agents']), placeholder='no AI agents') \
                        .props('dark dense outlined').classes('w-52')
                    ui.button(icon='save',
                              on_click=lambda n=s['name'], f=field: save_agents(n, f.value)) \
                        .props('flat dense color=orange').tooltip('Save agent users')
                    if s['name'] != LOCAL_SERVER:
                        ui.button(icon='delete', on_click=lambda n=s['name']: delete_server(n)) \
                            .props('flat dense color=red').tooltip('Stop monitoring')

    async def add_server():
        name = (name_in.value or '').strip()
        url = (url_in.value or '').strip().rstrip('/')
        token = (token_in.value or '').strip()
        users = parse_users(users_in.value) if agents_sw.value else []

        if not name or not url or not token:
            return fail('Name, base URL and node token are all required.')
        if name in {s['name'] for s in all_servers()}:
            return fail(f'A server named "{name}" is already registered.')
        if not url.startswith(('http://', 'https://')):
            return fail('Base URL must start with http:// or https://')
        if agents_sw.value and not users:
            return fail('Name at least one agent user, or turn the AI-agents switch off.')

        candidate = {'name': name, 'url': url, 'token': token, 'agents': users}
        add_btn.disable()
        feedback.classes(replace='text-sm text-gray-400 whitespace-pre-wrap')
        feedback.set_text(f'Contacting {url} ...')
        try:
            res = await run.io_bound(_node_request, candidate, '/api/ping')
        except Exception as e:
            return fail(f'Could not reach that server: {e}')
        finally:
            add_btn.enable()
        if not (isinstance(res, dict) and res.get('ok')):
            return fail('That URL answered, but not like a dashboard node.')

        save_servers(load_servers() + [candidate])
        for field in (name_in, url_in, token_in, users_in):
            field.set_value('')
        agents_sw.set_value(False)
        feedback.set_text('')
        render_listing()
        ui.notify(f'Now monitoring {name}', color='positive')
        await on_saved()

    add_btn.on_click(add_server)
    render_listing()
    return dialog


def login_page():
    """Password prompt; on success marks the browser session as authenticated."""
    if app.storage.user.get('authenticated', False):
        return RedirectResponse('/')

    def try_login():
        submitted = hashlib.sha256(password.value.encode()).hexdigest()
        # No configured hash means no way in — fail closed rather than open.
        if PASSWORD_HASH and hmac.compare_digest(submitted, PASSWORD_HASH):
            app.storage.user['authenticated'] = True
            ui.navigate.to('/')
        else:
            error.set_text('Invalid password.')

    ui.query('body').classes('bg-[#1e1e1e]')
    with ui.column().classes('absolute-center items-center'):
        with ui.card().classes('bg-[#2d2d2d] border border-[#444] p-10 items-stretch'):
            ui.label('Foxy Server Monitor').classes('text-xl font-bold text-[#ff9900] text-center')
            password = ui.input('Password', password=True).props('autofocus').classes('w-64')
            password.on('keydown.enter', try_login)
            ui.button('Login', on_click=try_login).props('color=orange')
            error = ui.label('').classes('text-red-400 text-center')


def main_page():
    """The dashboard. Built per connected client; two timers keep it live —
    system stats/charts every 15s, agent data every 60s.

    Every panel reads through the data-source seam, so a remote server
    renders exactly like the local one, and only the panels that apply to
    the selected server are shown.
    """
    if not app.storage.user.get('authenticated', False):
        return RedirectResponse('/login')

    ui.query('body').classes('bg-[#1e1e1e] font-mono')
    ui.colors(primary='#ff9900')

    def current_server():
        return get_server(app.storage.user.get('server', LOCAL_SERVER))

    with ui.column().classes('w-full max-w-screen-xl mx-auto p-4 gap-4'):
        with ui.row().classes('w-full items-center gap-3 no-wrap'):
            ui.label('Foxy Server Monitor v3').classes('text-2xl font-bold text-[#ff9900]')
            ui.space()
            server_select = ui.select([LOCAL_SERVER], value=LOCAL_SERVER, label='Server') \
                .props('dark dense outlined').classes('w-56')
            manage_btn = ui.button(icon='settings').props('flat color=orange') \
                .tooltip('Add or edit monitored servers')
        last_refresh = ui.label('Live updating every 15s.').classes('text-xs text-gray-500')

        with section_card('Live Resource Usage (CPU & RAM)'):
            resource_chart = make_chart(
                [('CPU Usage (%)', '#ff9900'), ('RAM Usage (%)', '#00ff00')],
                {'max': 100, 'min': 0})

        with section_card('Network Stats'):
            ping_chart = make_chart(
                [('Latency (ms)', '#ffff00'), ('Jitter (ms)', '#00ff00'), ('Packet Loss (%)', '#ff3366')])
            ping_label = ui.label('Loading...').classes('text-sm text-gray-400')

        with section_card('Network Throughput (Up/Down)'):
            net_chart = make_chart(
                [('Download', '#00ccff'), ('Upload', '#ff3366')],
                {'axisLabel': {'color': '#888', ':formatter': BYTES_FORMATTER}})
            net_label = ui.label('Loading...').classes('text-sm text-gray-400')

        with ui.row().classes('w-full items-stretch gap-4 no-wrap'):
            # Also agent-derived: the keys it probes come from the agents'
            # auth profiles, so it has nothing to test on a plain server.
            api_card = section_card('API Test').classes('flex-1')
            with api_card:
                with ui.row().classes('items-center gap-2'):
                    provider_select = ui.select(list(MODEL_OPTIONS), value='anthropic').classes('w-36')
                    model_select = ui.select(MODEL_OPTIONS['anthropic'],
                                             value=MODEL_OPTIONS['anthropic'][0]).classes('w-64')
                    api_btn = ui.button('Test')
                api_result = ui.label('').classes('text-sm text-gray-400 whitespace-pre-wrap')

                def on_provider_change():
                    models = MODEL_OPTIONS[provider_select.value]
                    model_select.set_options(models, value=models[0])
                provider_select.on_value_change(on_provider_change)

                async def run_api_test():
                    server = current_server()
                    provider, model = provider_select.value, model_select.value
                    if is_local(server):
                        # Remote runs are rate-limited by the node itself.
                        wait_msg = claim_apitest(provider, model)
                        if wait_msg:
                            api_result.classes(replace='text-sm text-orange-400 whitespace-pre-wrap')
                            api_result.set_text(wait_msg)
                            return
                    api_btn.disable()
                    api_result.classes(replace='text-sm text-gray-400 whitespace-pre-wrap')
                    api_result.set_text(f"Testing {provider} / {model} on {server['name']}...")
                    try:
                        if is_local(server):
                            res = await run.io_bound(test_api, provider, model)
                        else:
                            res = await run.io_bound(remote_apitest, server, provider, model)
                    finally:
                        api_btn.enable()
                    if res['status'] == 'ok':
                        api_result.classes(replace='text-sm text-green-400 whitespace-pre-wrap')
                        api_result.set_text(f"OK — {res['latency_ms']}ms\nReply: \"{res['reply']}\"")
                    else:
                        api_result.classes(replace='text-sm text-red-400 whitespace-pre-wrap')
                        api_result.set_text(f"Error\n{res['message']}")
                api_btn.on_click(run_api_test)

            with section_card('Memory & Disk').classes('flex-1'):
                ui.label('RAM').classes('text-xs text-gray-500')
                ram_label = ui.label('Loading...').classes('text-base font-bold text-white')
                ui.label('Disk (/)').classes('text-xs text-gray-500 mt-2')
                disk_label = ui.label('Loading...').classes('text-base font-bold text-white')

            with section_card('Internet Speed Test (Max 1/hour)').classes('flex-1'):
                speed_btn = ui.button('Run Speed Test')
                speed_result = ui.label(cached_speedtest_result).classes(
                    'text-sm text-gray-400 whitespace-pre-wrap')

                async def run_speed_test():
                    server = current_server()
                    if is_local(server):
                        # Remote runs are rate-limited by the node itself.
                        limited = claim_speedtest()
                        if limited:
                            speed_result.set_text(limited)
                            return
                    speed_btn.disable()
                    speed_result.set_text(
                        f"Running speed test on {server['name']}... (this takes ~20 seconds)")
                    try:
                        if is_local(server):
                            speed_result.set_text(await run.io_bound(do_speedtest))
                        else:
                            speed_result.set_text(await run.io_bound(remote_speedtest, server))
                    except Exception as e:
                        if is_local(server):
                            release_speedtest()  # the run failed; don't burn the hour
                        speed_result.set_text(f'Speedtest failed: {e}')
                    finally:
                        speed_btn.enable()
                speed_btn.on_click(run_speed_test)

        with ui.row().classes('w-full items-stretch gap-4 no-wrap'):
            with section_card('Top Processes (CPU)').classes('flex-1'):
                top_label = ui.label('Loading...').classes('text-xs text-green-400 whitespace-pre font-mono')
            with section_card('Security & Logins').classes('flex-1'):
                sec_label = ui.label('Loading...').classes('text-xs text-green-400 whitespace-pre font-mono')

        # The agent panels below (and the API test above) only apply to servers
        # that run AI agents; apply_visibility() builds them hidden and reveals
        # them once a read confirms the agents are really there.
        cron_card = section_card('Active Cron Jobs (all agents)')
        with cron_card:
            cron_table = ui.table(
                columns=[
                    {'name': 'user', 'label': 'User', 'field': 'user', 'align': 'left'},
                    {'name': 'name', 'label': 'Job', 'field': 'name', 'align': 'left'},
                    {'name': 'desc', 'label': 'Description', 'field': 'desc', 'align': 'left',
                     'classes': 'text-xs text-gray-400'},
                    {'name': 'next', 'label': 'Next Trigger', 'field': 'next', 'align': 'left'},
                ],
                rows=[],
            ).classes('w-full bg-transparent text-white').props('dark flat dense')

        models_card = section_card('Model Configuration — All Profiles')
        with models_card:
            model_config_box = ui.column().classes('w-full gap-4')

        memories_card = section_card('Recent Memories (Last 5 per user)')
        with memories_card:
            memories_box = ui.column().classes('w-full gap-2')

        issues_row = ui.row().classes('w-full items-stretch gap-4 no-wrap')
        with issues_row:
            with section_card('Current Issues').classes('flex-1'):
                active_issues_box = ui.column().classes('w-full gap-1')
            with section_card('Issues Fixed').classes('flex-1'):
                fixed_issues_box = ui.column().classes('w-full gap-1')

    def clear_charts():
        set_chart_data(resource_chart, [], [], [])
        set_chart_data(ping_chart, [], [], [], [])
        set_chart_data(net_chart, [], [], [])

    async def refresh_stats():
        """Redraw the system panels from the selected server's source.

        Remote sources are blocking HTTP, so the fetch goes through
        run.io_bound and a dead node degrades to an inline error.
        """
        server = current_server()
        try:
            data, h = await run.io_bound(source_stats, server)
        except Exception as e:
            last_refresh.classes(replace='text-xs text-red-400')
            last_refresh.set_text(f"{server['name']}: {source_problem(server)} — {e}")
            for label in (ram_label, disk_label, net_label, ping_label, top_label, sec_label):
                label.set_text('—')
            clear_charts()
            return

        last_refresh.classes(replace='text-xs text-gray-500')
        if data:
            ram_label.set_text(data.get('ram', 'Error'))
            disk_label.set_text(data.get('disk', 'Error'))
            net_label.set_text(f"RX: {data.get('net_rx', '?')} | TX: {data.get('net_tx', '?')}")
            ping_label.set_text(data.get('ping', ''))
            top_label.set_text(data.get('top_apps', ''))
            sec_label.set_text(data.get('security', ''))
        last_refresh.set_text(
            f"{server['name']} — live every 15s. "
            f"Last refresh: {datetime.now(LOCAL_TZ).strftime('%H:%M:%S')}")

        set_chart_data(resource_chart, h['labels'], h['cpu'], h['ram'])
        set_chart_data(ping_chart, h['labels'], h['ping'], h['jitter'], h['loss'])
        set_chart_data(net_chart, h['labels'], h['rx'], h['tx'])

    def issue_row(container, items):
        """Render a list of issues into a column, each tagged with its owner."""
        container.clear()
        with container:
            if not items:
                ui.label('None.').classes('text-sm text-gray-500')
            for item in items:
                with ui.row().classes('items-baseline gap-2 no-wrap'):
                    ui.label(f"[{item['user']}]").classes('text-xs text-[#ff9900]')
                    ui.label(item['text']).classes('text-sm text-white')

    def render_model_config(agents):
        """One table of profile → model assignments per agent, grouped by user."""
        model_config_box.clear()
        with model_config_box:
            if not agents:
                ui.label('No model configuration found.').classes('text-sm text-gray-500')
            for entry in agents:
                ui.label(f"{entry['user']} / {entry['agent']}").classes(
                    'text-sm font-bold text-[#ff9900]')
                rows = []
                for p in entry['profiles']:
                    fb = p['fallbacks']
                    rows.append({
                        'profile': p['name'],
                        'primary': p['primary'],
                        'reasoning': p['reasoning'],
                        'fb1': fb[0] if len(fb) > 0 else '–',
                        'fb2': fb[1] if len(fb) > 1 else '–',
                        # Everything past the first two collapses into "Final Fallback".
                        'final': ', '.join(fb[2:]) if len(fb) > 2 else '(none)',
                    })
                ui.table(
                    columns=[
                        {'name': 'profile', 'label': 'Profile', 'field': 'profile', 'align': 'left'},
                        {'name': 'primary', 'label': 'Primary Provider/Model', 'field': 'primary', 'align': 'left'},
                        {'name': 'reasoning', 'label': 'Reasoning', 'field': 'reasoning', 'align': 'left'},
                        {'name': 'fb1', 'label': 'Fallback 1', 'field': 'fb1', 'align': 'left'},
                        {'name': 'fb2', 'label': 'Fallback 2', 'field': 'fb2', 'align': 'left'},
                        {'name': 'final', 'label': 'Final Fallback', 'field': 'final', 'align': 'left'},
                    ],
                    rows=rows,
                ).classes('w-full bg-transparent text-white').props('dark flat dense')

    def apply_visibility(server, present=None):
        """Show the agent panels only for servers that really run AI agents.

        Being listed in the registry isn't enough: DASHBOARD_USERS defaults to
        'hermes, openclaw', so a plain server claims agent users it has no
        workspace for and would show a column of empty boxes. `present` is what
        the last agent-data read reported — None means 'not read yet', so the
        panels stay hidden until the answer is in rather than flashing empty.
        """
        show = bool(server['agents']) and bool(present)
        for card in (api_card, cron_card, models_card, memories_card, issues_row):
            card.set_visibility(show)

    def agent_error(message):
        cron_table.rows = []
        cron_table.update()
        for box in (model_config_box, memories_box, active_issues_box, fixed_issues_box):
            box.clear()
            with box:
                ui.label(message).classes('text-sm text-red-400')

    async def refresh_meta():
        """Re-read cron jobs, model config, memories, and issues for the
        selected server's agent users."""
        server = current_server()
        if not server['agents']:
            apply_visibility(server)
            return
        try:
            data = await run.io_bound(source_agentdata, server)
        except Exception as e:
            # Keep the panels up: the server is meant to have agents, and the
            # error explains why they're empty.
            apply_visibility(server, server['agents'])
            agent_error(f'{source_problem(server)} — {e}')
            return

        cron_table.rows = data.get('cron', [])
        cron_table.update()

        render_model_config(data.get('models', []))

        memories_box.clear()
        with memories_box:
            for user, lines in (data.get('memories') or {}).items():
                ui.label(user).classes('text-sm font-bold text-[#ff9900]')
                for line in lines:
                    ui.label(line).classes('text-sm text-white ml-4')

        issues = data.get('issues') or {}
        issue_row(active_issues_box, issues.get('active', []))
        issue_row(fixed_issues_box, issues.get('fixed', []))

        # Rendered before revealing, so switching servers never flashes the
        # previous one's data. A node too old to report 'present' falls back to
        # its configured list, keeping the pre-existing behavior.
        apply_visibility(server, data.get('present', server['agents']))

    async def refresh_all():
        await refresh_stats()
        await refresh_meta()

    def sync_server_options():
        """Rebuild the selector from the registry, keeping the current pick."""
        names = [s['name'] for s in all_servers()]
        chosen = app.storage.user.get('server', LOCAL_SERVER)
        if chosen not in names:
            chosen = LOCAL_SERVER
            app.storage.user['server'] = chosen
        server_select.set_options(names, value=chosen)

    async def on_server_change():
        app.storage.user['server'] = server_select.value or LOCAL_SERVER
        await refresh_all()

    async def on_registry_change():
        sync_server_options()
        await refresh_all()

    sync_server_options()
    server_select.on_value_change(on_server_change)
    manage_btn.on_click(servers_dialog(on_registry_change).open)
    apply_visibility(current_server())

    ui.timer(COLLECT_INTERVAL, refresh_stats)
    ui.timer(60, refresh_meta)
    ui.timer(0.1, refresh_all, once=True)  # don't leave the page blank until the first tick


# A node is headless: it serves the JSON API only, so the pages are
# registered explicitly here rather than with @ui.page decorators.
if MODE != 'node':
    ui.page('/login')(login_page)
    ui.page('/')(main_page)


# ------------------------------------------------------------------ main

def load_password_hash():
    """Login password hash: the env var, else a persisted one, else generated.

    With DASHBOARD_PASSWORD_HASH unset we mint a random password on first
    start, print it once, and store only its sha256 (0600) so later boots
    keep working. The source ships no default hash on purpose.
    """
    if PASSWORD_HASH:
        return PASSWORD_HASH
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, '.password_hash')
    try:
        with open(path) as f:
            stored = f.read().strip()
        if stored:
            return stored
    except FileNotFoundError:
        pass
    password = secrets.token_urlsafe(12)
    digest = hashlib.sha256(password.encode()).hexdigest()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(digest)
    print('=' * 66, flush=True)
    print('  DASHBOARD_PASSWORD_HASH is not set — generated a login password:', flush=True)
    print(f'      {password}', flush=True)
    print('  Shown once. Set DASHBOARD_PASSWORD_HASH to choose your own.', flush=True)
    print('=' * 66, flush=True)
    return digest


def load_storage_secret():
    """Persistent secret so login sessions survive restarts."""
    secret = os.environ.get('DASHBOARD_SECRET')
    if secret:
        return secret
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, '.storage_secret')
    try:
        with open(path) as f:
            return f.read().strip()
    except FileNotFoundError:
        secret = secrets.token_hex(32)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as f:
            f.write(secret)
        return secret


# NiceGUI may re-import this module in a child process, hence the '__mp_main__' guard.
if __name__ in {'__main__', '__mp_main__'}:
    if MODE not in {'hub', 'node'}:
        raise SystemExit(f"DASHBOARD_MODE must be 'hub' or 'node', not '{MODE}'")
    if MODE == 'node' and not NODE_TOKEN:
        raise SystemExit('DASHBOARD_MODE=node requires DASHBOARD_NODE_TOKEN '
                         '(the shared secret the hub presents).')
    init_db()
    # Report the speed-test CLI at boot: the panel is otherwise the only place
    # a missing or misnamed binary shows up, an hour-long cooldown away.
    try:
        _st_path, _st_flavor = find_speedtest()
        print(f'Speed test: {_st_path} ({_st_flavor} CLI)', flush=True)
    except Exception as e:
        print(f'Speed test unavailable: {e}', flush=True)
    if MODE == 'hub':
        PASSWORD_HASH = load_password_hash()
    register_health()
    if NODE_TOKEN:
        register_node_api()
    app.on_startup(lambda: threading.Thread(target=collector, daemon=True).start())
    ui.run(
        host='0.0.0.0',
        port=PORT,
        title='Foxy Server Monitor',
        dark=True,
        show=False,
        reload=False,
        storage_secret=load_storage_secret(),
    )
