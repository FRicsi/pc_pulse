"""PC Pulse local, read-only dashboard. Python 3.10+, standard library only."""
import calendar
import ctypes
import hmac
import json
import ntpath
import os
import secrets
import sqlite3
import stat
import subprocess
import threading
import time
from http.cookies import SimpleCookie
from datetime import datetime, timezone
from urllib.parse import urlsplit, parse_qs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CFG = json.loads((ROOT / 'config.json').read_text(encoding='utf-8-sig'))
# Set this once in source before enabling the protected settings panel.
SETTINGS_PASSWORD = 'SET_A_STRONG_PASSWORD_HERE'
SETTINGS_SESSION_TTL = 15 * 60
SETTINGS_SESSION_COOKIE = 'pc_pulse_settings'
SETTINGS_SESSIONS = {}
SETTINGS_SESSION_LOCK = threading.Lock()
WATCH = list(dict.fromkeys(os.path.normpath(os.path.expandvars(p)) for p in CFG['watch']))
EXCLUDE = [os.path.normcase(os.path.normpath(os.path.expandvars(p))) for p in CFG.get('exclude', [])]
DATA = ROOT / 'data'
DATA.mkdir(exist_ok=True)
DB = DATA / 'pulse.sqlite'
STOP = threading.Event()
LOCK = threading.Lock()
STATUS = {'audit': 'Indul…', 'scan': 'Indul…', 'lastScan': None, 'lastPoll': None, 'warnings': []}

def settings_config():
    return {
        'watch': list(CFG.get('watch', [])),
        'exclude': list(CFG.get('exclude', [])),
        'reads': bool(CFG.get('reads', False)),
        'readWatch': list(CFG.get('readWatch', [])),
        'retentionDays': CFG.get('retentionDays', 14),
        'maxEvents': CFG.get('maxEvents', 100000),
        'pollSeconds': CFG.get('pollSeconds', 10),
        'scanSeconds': CFG.get('scanSeconds', 900),
    }

def settings_origin_allowed(origin, port):
    try:
        parsed = urlsplit(origin)
        return (parsed.scheme == 'http' and parsed.path == '' and parsed.query == ''
                and parsed.fragment == '' and parsed.port == port
                and parsed.hostname in ('127.0.0.1', 'localhost'))
    except (TypeError, ValueError):
        return False

def settings_session(token, *, refresh=False):
    now_monotonic = time.monotonic()
    with SETTINGS_SESSION_LOCK:
        session = SETTINGS_SESSIONS.get(token)
        if not session or session['expires'] <= now_monotonic:
            SETTINGS_SESSIONS.pop(token, None)
            return None
        if refresh:
            session['expires'] = now_monotonic + SETTINGS_SESSION_TTL
        return session

def now():
    return datetime.now(timezone.utc).isoformat()

def connect():
    con = sqlite3.connect(DB, timeout=30)
    con.execute('PRAGMA journal_mode=WAL')
    return con

with connect() as con:
    con.executescript('''
    CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, time TEXT, user TEXT, program TEXT, path TEXT, operation TEXT);
    CREATE INDEX IF NOT EXISTS event_time ON events(time);
    CREATE TABLE IF NOT EXISTS scans(time REAL, path TEXT, bytes INTEGER, errors INTEGER);
    CREATE INDEX IF NOT EXISTS scan_time ON scans(time);
    CREATE TABLE IF NOT EXISTS drives(time REAL, path TEXT, total INTEGER, free INTEGER);
    CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT);
    ''')
    con.execute('PRAGMA auto_vacuum=INCREMENTAL')

def beneath(path, root):
    path, root = os.path.normcase(os.path.normpath(path)), os.path.normcase(os.path.normpath(root))
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False

def excluded(path):
    return beneath(path, str(DATA)) or any(beneath(path, p) for p in EXCLUDE)

def folder_size(root, breakdown=None):
    total, errors = 0, 0
    pending = [(root, None)]
    while pending and not STOP.is_set():
        directory, branch = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if excluded(entry.path):
                        continue
                    try:
                        info = entry.stat(follow_symlinks=False)
                        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                            continue  # Never follow NTFS junctions/reparse points.
                        if stat.S_ISDIR(info.st_mode):
                            child = branch or entry.path
                            if breakdown is not None:
                                breakdown.setdefault(child, [0, 0])
                            pending.append((entry.path, child))
                        elif stat.S_ISREG(info.st_mode):
                            total += info.st_size
                            if branch and breakdown is not None:
                                breakdown[branch][0] += info.st_size
                    except OSError:
                        errors += 1
                        if branch and breakdown is not None:
                            breakdown[branch][1] += 1
        except OSError:
            errors += 1
            if branch and breakdown is not None:
                breakdown[branch][1] += 1
    return total, errors

def drive_sizes():
    if os.name != 'nt':
        return []
    result = []
    kernel = ctypes.windll.kernel32
    mask = kernel.GetLogicalDrives()
    for index in range(26):
        path = chr(65 + index) + ':\\'
        if not (mask & (1 << index)) or kernel.GetDriveTypeW(ctypes.c_wchar_p(path)) != 3:
            continue
        available, total, free = ctypes.c_ulonglong(), ctypes.c_ulonglong(), ctypes.c_ulonglong()
        if kernel.GetDiskFreeSpaceExW(ctypes.c_wchar_p(path), ctypes.byref(available), ctypes.byref(total), ctypes.byref(free)):
            result.append((path, total.value, free.value))
    return result

def set_status(**values):
    with LOCK:
        STATUS.update(values)

def scan_once():
    stamp = time.time()
    drives = drive_sizes()
    measurements, warnings = {}, []
    with connect() as con:
        previous_paths = [r[0] for r in con.execute('SELECT DISTINCT path FROM scans')]
    for path in WATCH:
        if not os.path.isdir(path):
            warnings.append('Nem elérhető mappa: ' + path)
            continue
        set_status(scan='Felmérés: ' + path)
        breakdown = {}
        size, errors = folder_size(path, breakdown)
        measurements[path] = (stamp, path, size, errors)
        if not errors and not STOP.is_set():
            # A removed immediate child is a real zero, not a missing sample.
            for previous in previous_paths:
                if (os.path.normcase(os.path.dirname(previous)) == os.path.normcase(os.path.normpath(path))
                        and not os.path.lexists(previous)):
                    breakdown.setdefault(previous, [0, 0])
        for child, (child_size, child_errors) in breakdown.items():
            measurements[child] = (stamp, child, child_size, child_errors)
        if errors:
            warnings.append(f'{path}: {errors} kihagyott/olvashatatlan elem; részleges méret.')
    if STOP.is_set():
        return  # Do not persist a cancelled traversal as a complete measurement.
    with connect() as con:
        con.executemany('INSERT INTO scans VALUES(?,?,?,?)', measurements.values())
        con.executemany('INSERT INTO drives VALUES(?,?,?,?)', [(stamp, *d) for d in drives])
        cutoff = time.time() - max(1, int(CFG.get('retentionDays', 14))) * 86400
        for table in ('scans', 'drives'):
            con.execute(f'DELETE FROM {table} WHERE time < ?', (cutoff,))
        con.execute('DELETE FROM events WHERE time < ?', (datetime.fromtimestamp(cutoff, timezone.utc).isoformat(),))
        con.execute('DELETE FROM events WHERE id IN (SELECT id FROM events ORDER BY time DESC LIMIT -1 OFFSET ?)', (max(1, int(CFG.get('maxEvents', 100000))),))
    # Reclaim deleted pages even if audit is inaccessible. Database contains no file contents.
    with connect() as con:
        con.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        con.execute('VACUUM')
    set_status(scan='Kész', lastScan=now(), warnings=warnings)

def scan_loop():
    while not STOP.is_set():
        try:
            scan_once()
        except Exception as exc:
            set_status(scan='Hiba: ' + str(exc))
        STOP.wait(max(60, int(CFG.get('scanSeconds', 900))))

def decode_event(event):
    if event.get('type') != 'File' or not any(beneath(event.get('path', ''), p) for p in WATCH) or excluded(event.get('path', '')):
        return None
    mask = int(event.get('mask', '0'), 16)
    # 4663 indicates exercised access rights, not byte counts or proof of creation/deletion.
    if mask & 0x10000:
        operation = 'Törlési hozzáférés'
    elif mask & 6:
        operation = 'Írás'
    elif mask & 1 and CFG.get('reads', False):
        operation = 'Olvasás'
    else:
        return None
    return (event['id'], event['time'], event['user'], event.get('program') or 'Ismeretlen', event['path'], operation)

def collect_once():
    with connect() as con:
        row = con.execute("SELECT value FROM state WHERE key='cursor'").fetchone()
    cursor = int(row[0]) if row else 0
    run = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', str(ROOT / 'windows' / 'audit.ps1'), '-After', str(cursor)], capture_output=True, encoding='utf-8-sig', errors='replace', timeout=45, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if run.returncode:
        raise RuntimeError(run.stderr.strip() or 'Nem olvasható a Security napló. Indítsd rendszergazdaként.')
    payload = json.loads(run.stdout)
    events = [row for event in payload['events'] if (row := decode_event(event))]
    with connect() as con:
        if payload.get('reset'):
            # Record IDs can repeat after clearing the Security log; keep old rows under negative IDs.
            offset = con.execute('SELECT COALESCE(MAX(ABS(id)),0)+COALESCE(MAX(id),0)+1 FROM events').fetchone()[0]
            con.execute('UPDATE events SET id = id - ? WHERE id > 0', (offset,))
        con.executemany('INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?)', events)
        con.execute("INSERT OR REPLACE INTO state VALUES('cursor',?)", (str(payload['cursor']),))
        cutoff = datetime.fromtimestamp(time.time() - int(CFG.get('retentionDays', 14)) * 86400, timezone.utc).isoformat()
        con.execute('DELETE FROM events WHERE time < ?', (cutoff,))
        con.execute('DELETE FROM events WHERE id IN (SELECT id FROM events ORDER BY time DESC LIMIT -1 OFFSET ?)', (int(CFG.get('maxEvents', 100000)),))
        for table in ('scans', 'drives'):
            con.execute(f'DELETE FROM {table} WHERE time < ?', (time.time() - int(CFG.get('retentionDays', 14)) * 86400,))
    set_status(audit='Security napló olvasása működik; eseményekhez fájlaudit szükséges.', lastPoll=now())

def collect_loop():
    if os.name != 'nt':
        set_status(audit='A fájlaudit-gyűjtő Windows alatt működik.')
        return
    while not STOP.is_set():
        try:
            collect_once()
        except Exception as exc:
            set_status(audit='Hiba: ' + str(exc))
        STOP.wait(max(5, int(CFG.get('pollSeconds', 10))))

def period_start(amount=1, unit='day', end=None):
    if not isinstance(amount, int) or not 1 <= amount <= 9 or unit not in ('hour', 'day', 'week', 'month', 'year'):
        raise ValueError('amount: 1–9; unit: hour/day/week/month/year')
    end = end or datetime.now(timezone.utc)
    if unit in ('hour', 'day', 'week'):
        return end.timestamp() - amount * {'hour': 3600, 'day': 86400, 'week': 604800}[unit]
    months = amount * (12 if unit == 'year' else 1)
    index = end.year * 12 + end.month - 1 - months
    year, month = divmod(index, 12)
    month += 1
    return end.replace(year=year, month=month, day=min(end.day, calendar.monthrange(year, month)[1])).timestamp()


def chart_bucket_config(unit):
    return {
        'hour': (15 * 60, '15 perc'),
        'day': (60 * 60, '1 óra'),
        'week': (6 * 60 * 60, '6 óra'),
        'month': (24 * 60 * 60, '1 nap'),
        'year': (30 * 24 * 60 * 60, '1 hónap'),
    }.get(unit, (60 * 60, '1 óra'))


def filtered_event_rows(*, since, program=None, user=None, path=None, operation=None):
    clauses = ['time >= ?']
    params = [datetime.fromtimestamp(since, timezone.utc).isoformat()]
    if program:
        clauses.append('program = ?')
        params.append(program)
    if user:
        clauses.append('user = ?')
        params.append(user)
    if operation:
        clauses.append('operation = ?')
        params.append(operation)
    if path:
        clauses.append('INSTR(LOWER(REPLACE(path, CHAR(92), CHAR(47))), LOWER(?)) > 0')
        params.append(path.replace('\\', '/'))
    sql = 'SELECT time,user,program,path,operation FROM events WHERE ' + ' AND '.join(clauses)
    with connect() as con:
        return con.execute(sql + ' ORDER BY time DESC', params).fetchall()


def event_chart_data(amount=1, unit='day', *, program=None, user=None, path=None, operation=None):
    since = period_start(amount, unit)
    step, label = chart_bucket_config(unit)
    end = datetime.now(timezone.utc).timestamp()
    buckets = []
    size = max(1, int((end - since) // step) + 1)
    for i in range(size):
        start = since + i * step
        buckets.append({'start': start, 'end': min(start + step, end), 'count': 0})
    rows = filtered_event_rows(since=since, program=program, user=user, path=path, operation=operation)
    for stamp, *_ in rows:
        event_time = datetime.fromisoformat(stamp).timestamp()
        if event_time < since or event_time >= end:
            continue
        idx = min(max(int((event_time - since) // step), 0), len(buckets) - 1)
        buckets[idx]['count'] += 1
    return {'bucketLabel': label, 'buckets': buckets}

def storage_path(path):
    return ntpath.normcase(ntpath.normpath(path))


def storage_beneath(path, root):
    try:
        return ntpath.commonpath([storage_path(path), storage_path(root)]) == storage_path(root)
    except ValueError:
        return False


def storage_charts(drive_history, scan_rows, drive_paths):
    """Disjoint logical folder deltas, plus the physical-minus-logical residual.

    A folder stack begins only where all its components have complete samples.
    Missing or unreadable samples are gaps, never invented zero measurements.
    """
    scans = {}
    for stamp, path, size, errors in scan_rows:
        scans.setdefault(storage_path(path), {'path': path, 'samples': {}})['samples'][stamp] = (size, errors)
    charts = {}
    for drive in drive_paths:
        rows = [(t, total - free) for t, p, total, free in drive_history if storage_path(p) == storage_path(drive)]
        rows = sorted(dict(rows).items())
        candidates = sorted((p for p in scans if p != storage_path(drive) and storage_beneath(p, drive)), key=lambda p: (p.count('\\'), p))
        # Keep the outermost measured subtrees: parent and child must not be summed.
        selected = []
        for p in candidates:
            if not any(storage_beneath(p, ancestor) for ancestor in selected):
                selected.append(p)
        common = [t for t, _ in rows if selected and all(t in scans[p]['samples'] and scans[p]['samples'][t][1] == 0 for p in selected)]
        # Bound the response while retaining both ends of the interval.
        if len(rows) > 1000:
            rows = [rows[round(i * (len(rows) - 1) / 999)] for i in range(1000)]
        if len(common) > 1000:
            common = [common[round(i * (len(common) - 1) / 999)] for i in range(1000)]
        physical = {t: total - free for t, p, total, free in drive_history if storage_path(p) == storage_path(drive)}
        stack = []
        if len(common) >= 2:
            first = common[0]
            for p in selected:
                initial = scans[p]['samples'][first][0]
                stack.append({'path': scans[p]['path'], 'values': [scans[p]['samples'][t][0] - initial for t in common]})
            residual = [physical[t] - physical[first] - sum(s['values'][i] for s in stack) for i, t in enumerate(common)]
            stack.append({'path': None, 'values': residual})
        charts[drive] = {'history': rows, 'times': common if stack else [], 'series': stack,
                         'totalDelta': [physical[t] - physical[common[0]] for t in common] if stack else [],
                         'incomplete': any(e for p in selected for _, e in scans[p]['samples'].values())}
    return charts


def snapshot(amount=1, unit='day', *, program=None, user=None, path=None, operation=None, global_user=None):
    since = period_start(amount, unit)
    cutoff = datetime.fromtimestamp(since, timezone.utc).isoformat()
    with connect() as con:
        scope = 'time >= ?' + (' AND user = ?' if global_user else '')
        scope_params = (cutoff, global_user) if global_user else (cutoff,)
        event_rows = con.execute('SELECT time,user,program,path,operation FROM events WHERE ' + scope + ' ORDER BY time DESC LIMIT 5000', scope_params).fetchall()
        count = con.execute('SELECT COUNT(*) FROM events WHERE ' + scope, scope_params).fetchone()[0]
        program_stats = con.execute("SELECT program,COUNT(*) FROM events WHERE " + scope + " AND operation='Írás' GROUP BY program ORDER BY COUNT(*) DESC,program", scope_params).fetchall()
        users = con.execute('SELECT DISTINCT user FROM events WHERE user IS NOT NULL ORDER BY user').fetchall()
        programs = con.execute('SELECT DISTINCT program FROM events WHERE ' + scope + ' ORDER BY program', scope_params).fetchall()
        identities = con.execute('SELECT COUNT(DISTINCT program),COUNT(DISTINCT user) FROM events WHERE ' + scope, scope_params).fetchone()
        user_paths = [r[0] for r in con.execute('SELECT DISTINCT path FROM events WHERE ' + scope, scope_params)] if global_user else None
        earliest_event = con.execute('SELECT MIN(time) FROM events').fetchone()[0]
        earliest_scan = con.execute('SELECT MIN(time) FROM scans').fetchone()[0]
        drive_rows = con.execute('SELECT path,total,free FROM drives WHERE time=(SELECT MAX(time) FROM drives)').fetchall()
        folders = []
        scan_rows = con.execute('SELECT time,path,bytes,errors FROM scans WHERE time>=? ORDER BY time', (since,)).fetchall()
        scans_by_path = {}
        for t, measured_path, size, errors in scan_rows:
            scans_by_path.setdefault(measured_path, []).append((t, size, errors))
        for watched_path, rows in sorted(scans_by_path.items()):
            if user_paths is not None and not any(storage_beneath(p, watched_path) for p in user_paths):
                continue
            if rows:
                folders.append({'path': watched_path, 'bytes': rows[-1][1], 'growth': rows[-1][1] - rows[0][1] if len(rows) > 1 else None, 'errors': rows[-1][2], 'samples': len(rows)})
        # Avoid false growth when a drive appears or disappears between scans.
        drive_history = con.execute('SELECT time,path,total,free FROM drives WHERE time>=? ORDER BY time', (since,)).fetchall()
        grouped = {}
        for stamp,p,total,free in drive_history:
            grouped.setdefault(stamp, {})[p] = total-free
        paths = {p for p,_,_ in drive_rows}
        history = [(stamp,sum(sizes.values())) for stamp,sizes in grouped.items() if set(sizes)==paths]
        growth = history[-1][1]-history[0][1] if len(history)>1 else None
        # Bound graph payloads even for years of quarter-hour measurements.
        if len(history) > 1000:
            history = [history[round(i * (len(history)-1) / 999)] for i in range(1000)]
        storage = storage_charts(drive_history, scan_rows, [p for p, _, _ in drive_rows])
        chart = event_chart_data(amount, unit, program=program, user=global_user or user, path=path, operation=operation)
    with LOCK:
        status = dict(STATUS)
    return {'status': status, 'drives': [{'path': p, 'total': t, 'free': f} for p,t,f in drive_rows], 'folders': folders, 'events': [{'time': t,'user': u,'program': p,'path': f,'operation': o} for t,u,p,f,o in event_rows], 'history': history, 'growth': growth, 'eventCount': count, 'config': {'watch': WATCH, 'reads': CFG.get('reads', False), 'retention': CFG.get('retentionDays', 14)}, 'updated': now(), 'period': {'amount': amount, 'unit': unit, 'start': cutoff}, 'available': {'eventsSince': earliest_event, 'scansSince': earliest_scan}, 'programStats': [{'program': p, 'count': n} for p,n in program_stats], 'users': [u[0] for u in users], 'identities': {'programs': identities[0], 'users': identities[1]}, 'chart': chart, 'storageCharts': storage, 'programs': [p[0] for p in programs], 'globalUser': global_user}


def fetch_events(amount=1, unit='day', *, program=None, user=None, path=None, operation=None):
    since = period_start(amount, unit)
    rows = filtered_event_rows(since=since, program=program, user=user, path=path, operation=operation)[:5000]
    return [{'time': t, 'user': u, 'program': p, 'path': f, 'operation': o} for t, u, p, f, o in rows]


class Handler(BaseHTTPRequestHandler):
    def send_json(self, status, payload, headers=None):
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        if headers:
            for name, value in headers.items():
                self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def settings_request_is_local(self):
        return self.client_address[0] in ('127.0.0.1', '::1')

    def settings_origin_is_valid(self, *, required=False):
        origin = self.headers.get('Origin')
        if origin is None:
            return not required
        return settings_origin_allowed(origin, int(CFG['port']))

    def settings_cookie_token(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get('Cookie', ''))
        except Exception:
            return None
        morsel = cookie.get(SETTINGS_SESSION_COOKIE)
        return morsel.value if morsel else None

    def require_settings_session(self):
        token = self.settings_cookie_token()
        if not token:
            return None
        return settings_session(token, refresh=True)

    def do_GET(self):
        if self.headers.get('Host') not in (f'127.0.0.1:{CFG["port"]}', f'localhost:{CFG["port"]}'):
            self.send_error(403)
            return
        url = urlsplit(self.path)
        if url.path == '/api/runtime':
            with LOCK:
                status = dict(STATUS)
            self.send_json(200, {'root': str(ROOT), 'configPath': str(ROOT / 'config.json'), 'status': status})
            return
        if url.path == '/api/control-info':
            self.send_json(200, {'controlPort': int(CFG.get('controlPort', int(CFG['port']) + 1)),
                                 'monitorPort': int(CFG['port']), 'isControl': False})
            return
        if url.path == '/api/settings/config':
            session = self.require_settings_session()
            if (not self.settings_request_is_local() or session is None
                    or not self.settings_origin_is_valid()):
                self.send_json(403, {'error': 'Hitelesítés szükséges.'})
                return
            self.send_json(200, {'config': settings_config()})
            return
        if url.path == '/api/snapshot':
            query = parse_qs(url.query)
            try:
                payload = snapshot(
                    int(query.get('amount', ['1'])[0]),
                    query.get('unit', ['day'])[0],
                    program=query.get('program', [''])[0] or None,
                    user=query.get('user', [''])[0] or None,
                    path=query.get('path', [''])[0] or None,
                    operation=query.get('operation', [''])[0] or None,
                    global_user=query.get('globalUser', [''])[0] or None,
                )
            except (ValueError, OverflowError):
                self.send_error(400, 'Invalid period')
                return
            body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
            mime = 'application/json; charset=utf-8'
        elif url.path == '/api/events':
            query = parse_qs(url.query)
            try:
                payload = fetch_events(
                    int(query.get('amount', ['1'])[0]),
                    query.get('unit', ['day'])[0],
                    program=query.get('program', [''])[0] or None,
                    user=query.get('user', [''])[0] or None,
                    path=query.get('path', [''])[0] or None,
                    operation=query.get('operation', [''])[0] or None,
                )
            except (ValueError, OverflowError):
                self.send_error(400, 'Invalid period')
                return
            body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
            mime = 'application/json; charset=utf-8'
        elif self.path in ('/', '/PC-Pulse.html'):
            body = (ROOT / 'PC-Pulse.html').read_bytes()
            mime = 'text/html; charset=utf-8'
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        control_port = int(CFG.get('controlPort', int(CFG['port']) + 1))
        self.send_header('Content-Security-Policy', f"default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self' http://127.0.0.1:{control_port} http://localhost:{control_port}; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.headers.get('Host') not in (f'127.0.0.1:{CFG["port"]}', f'localhost:{CFG["port"]}'):
            self.send_error(403)
            return
        if not self.settings_request_is_local():
            self.send_json(403, {'error': 'Csak helyi kérés engedélyezett.'})
            return
        url = urlsplit(self.path)
        if url.path == '/api/settings/login':
            if not self.settings_origin_is_valid(required=True):
                self.send_json(403, {'error': 'Érvénytelen Origin.'})
                return
            if SETTINGS_PASSWORD == 'SET_A_STRONG_PASSWORD_HERE':
                self.send_json(503, {'error': 'A beállítási jelszó még nincs megadva a backendben.'})
                return
            if self.headers.get_content_type() != 'application/json':
                self.send_json(415, {'error': 'JSON kérés szükséges.'})
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 4096:
                    raise ValueError
                payload = json.loads(self.rfile.read(length))
                password = payload.get('password') if isinstance(payload, dict) else None
            except (ValueError, json.JSONDecodeError):
                self.send_json(400, {'error': 'Érvénytelen kérés.'})
                return
            if not isinstance(password, str) or not hmac.compare_digest(password.encode('utf-8'), SETTINGS_PASSWORD.encode('utf-8')):
                self.send_json(401, {'error': 'Hibás jelszó.'})
                return
            token = secrets.token_urlsafe(32)
            csrf_token = secrets.token_urlsafe(32)
            with SETTINGS_SESSION_LOCK:
                SETTINGS_SESSIONS[token] = {
                    'csrf': csrf_token,
                    'expires': time.monotonic() + SETTINGS_SESSION_TTL,
                }
            self.send_json(200, {'csrfToken': csrf_token, 'expiresIn': SETTINGS_SESSION_TTL}, {
                'Set-Cookie': f'{SETTINGS_SESSION_COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SETTINGS_SESSION_TTL}',
            })
            return
        if url.path == '/api/settings/logout':
            if not self.settings_origin_is_valid(required=True):
                self.send_json(403, {'error': 'Érvénytelen Origin.'})
                return
            session = self.require_settings_session()
            csrf = self.headers.get('X-CSRF-Token', '')
            if session is None or not hmac.compare_digest(csrf, session['csrf']):
                self.send_json(403, {'error': 'A munkamenet vagy a CSRF-token érvénytelen.'})
                return
            token = self.settings_cookie_token()
            with SETTINGS_SESSION_LOCK:
                SETTINGS_SESSIONS.pop(token, None)
            self.send_json(200, {'ok': True}, {
                'Set-Cookie': f'{SETTINGS_SESSION_COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0',
            })
            return
        self.send_json(404, {'error': 'Nincs ilyen végpont.'})

    def log_message(self, *_):
        pass

if __name__ == '__main__':
    server = ThreadingHTTPServer(('127.0.0.1', int(CFG['port'])), Handler)
    threading.Thread(target=scan_loop, daemon=True).start()
    threading.Thread(target=collect_loop, daemon=True).start()
    print(f'PC Pulse: http://127.0.0.1:{CFG["port"]}  (Ctrl+C: leállítás)', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        STOP.set()
        server.server_close()
