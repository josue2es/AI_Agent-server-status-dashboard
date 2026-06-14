#!/usr/bin/env python3
"""AI agents status dashboard.

NiceGUI rewrite. Runs as root and aggregates agent data (cron jobs,
memories, issues, auth profiles) from multiple user accounts. Data is
currently read from each user's openclaw workspace.
"""

import hashlib
import hmac
import json
import os
import secrets
import shutil
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi.responses import RedirectResponse
from nicegui import app, run, ui

# Users whose agent workspaces are aggregated (cron jobs, memories, issues).
AGENT_USERS = os.environ.get('DASHBOARD_USERS', 'hermes,openclaw').split(',')

PORT = int(os.environ.get('DASHBOARD_PORT', '8080'))
DATA_DIR = os.environ.get('DASHBOARD_DATA_DIR', '/var/lib/ai-agents-dashboard')
DB_PATH = os.path.join(DATA_DIR, 'dashboard.db')
SPEEDTEST_BIN = os.environ.get('SPEEDTEST_BIN', 'speedtest-ookla')
# Filesystem to report disk usage for. In a container, set this to a bind mount
# of the host root (e.g. /host) so the panel shows the server's disk, not the overlay.
DISK_PATH = os.environ.get('DASHBOARD_DISK_PATH', '/')
LOCAL_TZ = ZoneInfo(os.environ.get('DASHBOARD_TZ', 'America/El_Salvador'))

# Password hash (sha256 of the actual password)
PASSWORD_HASH = os.environ.get(
    'DASHBOARD_PASSWORD_HASH',
    'd8e87dbe011188ad0968f1f57e259bd9a8465f353f5032485e8d482e46574dcc')

APITEST_COOLDOWN = 30  # seconds between tests per model
SPEEDTEST_COOLDOWN = 3600  # seconds between speed tests
COLLECT_INTERVAL = 15  # seconds between metric samples
DISPLAY_POINTS = 720  # 3 hours of 15s samples shown on charts

MODEL_OPTIONS = {
    'anthropic': ['claude-sonnet-4-6', 'claude-haiku-4-5-20251001', 'claude-opus-4-6'],
    'google': ['gemini-3.1-pro-preview', 'gemini-3.1-flash-lite-preview'],
    'moonshot': ['kimi-k2.5', 'moonshot-v1-8k', 'moonshot-v1-32k'],
}

apitest_times = {}  # (provider, model) -> last test timestamp
last_speedtest_time = 0.0
cached_speedtest_result = 'Not run yet.'

latest = {}  # most recent system snapshot, written by the collector thread
latest_lock = threading.Lock()


def user_path(user, *parts):
    """Path inside a given user's openclaw directory, e.g. /home/hermes/.openclaw/..."""
    return os.path.join('/home', user, '.openclaw', *parts)


# ---------------------------------------------------------------- database

def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute('''CREATE TABLE IF NOT EXISTS metrics
                    (timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                     cpu REAL, ram REAL, rx REAL, tx REAL, ping REAL, jitter REAL, loss REAL)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS speedtests
                    (timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                     download REAL, upload REAL, ping REAL)''')
    conn.commit()
    conn.close()


def fetch_history(limit=DISPLAY_POINTS):
    """Most recent metric samples as parallel lists ready for the charts.

    Timestamps are stored in UTC by SQLite and converted to LOCAL_TZ here.
    """
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        'SELECT timestamp, cpu, ram, rx, tx, ping, jitter, loss '
        'FROM metrics ORDER BY timestamp DESC LIMIT ?', (limit,)).fetchall()
    conn.close()
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
        with open('/proc/net/dev') as f:
            for line in f:
                if 'eth0:' in line or 'ens3:' in line:
                    parts = line.split()
                    info['net_rx'] = f"{int(parts[1])/1024/1024:.2f} MB"
                    info['net_tx'] = f"{int(parts[9])/1024/1024:.2f} MB"
                    info['net_rx_bytes'] = int(parts[1])
                    info['net_tx_bytes'] = int(parts[9])
                    break
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

                conn = sqlite3.connect(DB_PATH)
                conn.execute(
                    'INSERT INTO metrics (cpu, ram, rx, tx, ping, jitter, loss) VALUES (?, ?, ?, ?, ?, ?, ?)',
                    (round(cpu_pct, 1), data.get('ram_percent', 0),
                     round(rx_bps, 2), round(tx_bps, 2),
                     data.get('ping_avg', 0), data.get('jitter', 0), data.get('packet_loss', 0)))
                conn.execute('DELETE FROM metrics WHERE timestamp < datetime("now", "-24 hours")')
                conn.commit()
                conn.close()

            last_rx = data['net_rx_bytes']
            last_tx = data['net_tx_bytes']
            last_cpu_idle = data['cpu_raw']['idle']
            last_cpu_total = data['cpu_raw']['total']
            last_time = now

            with latest_lock:
                latest.clear()
                latest.update(data)
        except Exception as e:
            print(f"Collector error: {e}")
        time.sleep(COLLECT_INTERVAL)


# -------------------------------------------------- multi-user agent data

def get_cron_jobs():
    """Enabled cron jobs across all configured agent users."""
    jobs = []
    for user in AGENT_USERS:
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


def get_memories():
    """Last 5 memory entries of today per agent user."""
    memories = {}
    today = datetime.now(LOCAL_TZ).strftime('%Y-%m-%d')
    for user in AGENT_USERS:
        path = user_path(user, 'workspace', 'memory', f'{today}.md')
        try:
            with open(path) as f:
                lines = [l.strip() for l in f if l.strip().startswith('-')]
            memories[user] = lines[-5:] or ['No recent memories.']
        except Exception:
            memories[user] = ['No recent memories.']
    return memories


def get_issues():
    """Active and resolved issues across all agent users, tagged by user."""
    issues = {'active': [], 'fixed': []}
    for user in AGENT_USERS:
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
            url = f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}'
            req = urllib.request.Request(url, data=payload, headers={'Content-Type': 'application/json'})
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


# ------------------------------------------------------------ speed test

def do_speedtest():
    """Run the Ookla CLI (~20s, blocking — call via run.io_bound) and record the result."""
    global last_speedtest_time, cached_speedtest_result
    result = subprocess.run(
        [SPEEDTEST_BIN, '--accept-license', '--accept-gdpr', '-f', 'json'],
        capture_output=True, text=True)
    if result.returncode != 0:
        raise Exception(result.stderr.strip() or f'Process exited with {result.returncode}')
    out = result.stdout
    # Ookla CLI sometimes prints warnings before the JSON
    data = json.loads(out[out.find('{'):] if '{' in out else out)
    ping = data.get('ping', {}).get('latency', 0)
    down_mbps = data.get('download', {}).get('bandwidth', 0) * 8 / 1000000
    up_mbps = data.get('upload', {}).get('bandwidth', 0) * 8 / 1000000
    server = data.get('server', {}).get('name', 'Unknown')
    location = data.get('server', {}).get('location', 'Unknown')

    cached_speedtest_result = (f"Ping: {ping:.2f} ms\nDownload: {down_mbps:.2f} Mbps\n"
                               f"Upload: {up_mbps:.2f} Mbps\nServer: {server} ({location})")
    last_speedtest_time = time.time()

    conn = sqlite3.connect(DB_PATH)
    conn.execute('INSERT INTO speedtests (download, upload, ping) VALUES (?, ?, ?)',
                 (down_mbps, up_mbps, ping))
    conn.commit()
    conn.close()
    return cached_speedtest_result


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


@ui.page('/login')
def login_page():
    """Password prompt; on success marks the browser session as authenticated."""
    if app.storage.user.get('authenticated', False):
        return RedirectResponse('/')

    def try_login():
        submitted = hashlib.sha256(password.value.encode()).hexdigest()
        if hmac.compare_digest(submitted, PASSWORD_HASH):
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


@ui.page('/')
def main_page():
    """The dashboard itself. Built per connected client; two timers keep it
    live — system stats/charts every 15s, agent data every 60s."""
    if not app.storage.user.get('authenticated', False):
        return RedirectResponse('/login')

    ui.query('body').classes('bg-[#1e1e1e] font-mono')
    ui.colors(primary='#ff9900')

    with ui.column().classes('w-full max-w-screen-xl mx-auto p-4 gap-4'):
        ui.label('Foxy Server Monitor v3').classes('text-2xl font-bold text-[#ff9900]')
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
            with section_card('API Test').classes('flex-1'):
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
                    provider, model = provider_select.value, model_select.value
                    now = time.time()
                    wait = APITEST_COOLDOWN - (now - apitest_times.get((provider, model), 0))
                    if wait > 0:
                        api_result.classes(replace='text-sm text-orange-400 whitespace-pre-wrap')
                        api_result.set_text(f'Please wait {int(wait) + 1}s before testing {model} again.')
                        return
                    apitest_times[(provider, model)] = now
                    api_btn.disable()
                    api_result.classes(replace='text-sm text-gray-400 whitespace-pre-wrap')
                    api_result.set_text(f'Testing {provider} / {model}...')
                    try:
                        res = await run.io_bound(test_api, provider, model)
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
                    now = time.time()
                    if last_speedtest_time and now - last_speedtest_time < SPEEDTEST_COOLDOWN:
                        mins = int((SPEEDTEST_COOLDOWN - (now - last_speedtest_time)) // 60) + 1
                        speed_result.set_text(
                            f'Rate limited. Try again in {mins} minutes.\n\nLast result:\n{cached_speedtest_result}')
                        return
                    speed_btn.disable()
                    speed_result.set_text('Running speed test... (this takes ~20 seconds)')
                    try:
                        speed_result.set_text(await run.io_bound(do_speedtest))
                    except Exception as e:
                        speed_result.set_text(f'Speedtest failed: {e}')
                    finally:
                        speed_btn.enable()
                speed_btn.on_click(run_speed_test)

        with ui.row().classes('w-full items-stretch gap-4 no-wrap'):
            with section_card('Top Processes (CPU)').classes('flex-1'):
                top_label = ui.label('Loading...').classes('text-xs text-green-400 whitespace-pre font-mono')
            with section_card('Security & Logins').classes('flex-1'):
                sec_label = ui.label('Loading...').classes('text-xs text-green-400 whitespace-pre font-mono')

        with section_card('Active Cron Jobs (all users)'):
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

        with section_card('Recent Memories (Last 5 per user)'):
            memories_box = ui.column().classes('w-full gap-2')

        with ui.row().classes('w-full items-stretch gap-4 no-wrap'):
            with section_card('Current Issues').classes('flex-1'):
                active_issues_box = ui.column().classes('w-full gap-1')
            with section_card('Issues Fixed').classes('flex-1'):
                fixed_issues_box = ui.column().classes('w-full gap-1')

    def refresh_stats():
        """Update text metrics from the collector's snapshot and redraw charts from the DB."""
        with latest_lock:
            data = dict(latest)
        if data:
            ram_label.set_text(data.get('ram', 'Error'))
            disk_label.set_text(data.get('disk', 'Error'))
            net_label.set_text(f"RX: {data.get('net_rx', '?')} | TX: {data.get('net_tx', '?')}")
            ping_label.set_text(data.get('ping', ''))
            top_label.set_text(data.get('top_apps', ''))
            sec_label.set_text(data.get('security', ''))
        last_refresh.set_text(
            f"Live updating every 15s. Last refresh: {datetime.now(LOCAL_TZ).strftime('%H:%M:%S')}")

        h = fetch_history()
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

    def refresh_meta():
        """Re-read cron jobs, memories, and issues from every user's workspace."""
        cron_table.rows = get_cron_jobs()
        cron_table.update()

        memories_box.clear()
        with memories_box:
            for user, lines in get_memories().items():
                ui.label(user).classes('text-sm font-bold text-[#ff9900]')
                for line in lines:
                    ui.label(line).classes('text-sm text-white ml-4')

        issues = get_issues()
        issue_row(active_issues_box, issues['active'])
        issue_row(fixed_issues_box, issues['fixed'])

    ui.timer(COLLECT_INTERVAL, refresh_stats)
    ui.timer(60, refresh_meta)


# ------------------------------------------------------------------ main

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
    init_db()
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
