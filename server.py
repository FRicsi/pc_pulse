"""PC Pulse local, read-only dashboard. Python 3.10+, standard library only."""
import calendar
import ctypes
import json
import os
import sqlite3
import stat
import subprocess
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit, parse_qs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CFG = json.loads((ROOT / 'config.json').read_text(encoding='utf-8-sig'))
WATCH = list(dict.fromkeys(os.path.normpath(os.path.expandvars(p)) for p in CFG['watch']))
EXCLUDE = [os.path.normcase(os.path.normpath(os.path.expandvars(p))) for p in CFG.get('exclude', [])]
DATA = ROOT / 'data'
DATA.mkdir(exist_ok=True)
DB = DATA / 'pulse.sqlite'
STOP = threading.Event()
LOCK = threading.Lock()
STATUS = {'audit': 'Indul…', 'scan': 'Indul…', 'lastScan': None, 'lastPoll': None, 'warnings': []}

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

def folder_size(root):
    total, errors = 0, 0
    pending = [root]
    while pending and not STOP.is_set():
        directory = pending.pop()
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
                            pending.append(entry.path)
                        elif stat.S_ISREG(info.st_mode):
                            total += info.st_size
                    except OSError:
                        errors += 1
        except OSError:
            errors += 1
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
    rows, warnings = [], []
    for path in WATCH:
        if not os.path.isdir(path):
            warnings.append('Nem elérhető mappa: ' + path)
            continue
        set_status(scan='Felmérés: ' + path)
        size, errors = folder_size(path)
        rows.append((stamp, path, size, errors))
        if errors:
            warnings.append(f'{path}: {errors} kihagyott/olvashatatlan elem; részleges méret.')
    with connect() as con:
        con.executemany('INSERT INTO scans VALUES(?,?,?,?)', rows)
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

def snapshot(amount=1, unit='day'):
    since = period_start(amount, unit)
    cutoff = datetime.fromtimestamp(since, timezone.utc).isoformat()
    with connect() as con:
        event_rows = con.execute('SELECT time,user,program,path,operation FROM events WHERE time >= ? ORDER BY time DESC LIMIT 5000', (cutoff,)).fetchall()
        count = con.execute('SELECT COUNT(*) FROM events WHERE time >= ?', (cutoff,)).fetchone()[0]
        program_stats = con.execute("SELECT program,COUNT(*) FROM events WHERE time>=? AND operation='Írás' GROUP BY program ORDER BY COUNT(*) DESC", (cutoff,)).fetchall()
        identities = con.execute('SELECT COUNT(DISTINCT program),COUNT(DISTINCT user) FROM events WHERE time>=?', (cutoff,)).fetchone()
        earliest_event = con.execute('SELECT MIN(time) FROM events').fetchone()[0]
        earliest_scan = con.execute('SELECT MIN(time) FROM scans').fetchone()[0]
        drive_rows = con.execute('SELECT path,total,free FROM drives WHERE time=(SELECT MAX(time) FROM drives)').fetchall()
        folders = []
        for path in WATCH:
            rows = con.execute('SELECT time,bytes,errors FROM scans WHERE path=? AND time>=? ORDER BY time', (path, since)).fetchall()
            if rows:
                folders.append({'path': path, 'bytes': rows[-1][1], 'growth': rows[-1][1] - rows[0][1] if len(rows) > 1 else None, 'errors': rows[-1][2], 'samples': len(rows)})
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
    with LOCK:
        status = dict(STATUS)
    return {'status': status, 'drives': [{'path': p, 'total': t, 'free': f} for p,t,f in drive_rows], 'folders': folders, 'events': [{'time': t,'user': u,'program': p,'path': f,'operation': o} for t,u,p,f,o in event_rows], 'history': history, 'growth': growth, 'eventCount': count, 'config': {'watch': WATCH, 'reads': CFG.get('reads', False), 'retention': CFG.get('retentionDays', 14)}, 'updated': now(), 'period': {'amount': amount, 'unit': unit, 'start': cutoff}, 'available': {'eventsSince': earliest_event, 'scansSince': earliest_scan}, 'programStats': [{'program': p, 'count': n} for p,n in program_stats], 'identities': {'programs': identities[0], 'users': identities[1]}}


def fetch_events(amount=1, unit='day', *, program=None, user=None, path=None, operation=None):
    since = period_start(amount, unit)
    cutoff = datetime.fromtimestamp(since, timezone.utc).isoformat()
    with connect() as con:
        rows = con.execute(
            'SELECT time,user,program,path,operation FROM events WHERE time >= ? ORDER BY time DESC',
            (cutoff,),
        ).fetchall()
    selected = []
    for time_value, user_name, program_name, event_path, event_operation in rows:
        if program and program_name != program:
            continue
        if user and user_name != user:
            continue
        if operation and event_operation != operation:
            continue
        if path:
            match_path = os.path.normcase(os.path.normpath(event_path))
            root = os.path.normcase(os.path.normpath(path))
            if match_path != root and not beneath(match_path, root):
                continue
        selected.append({'time': time_value, 'user': user_name, 'program': program_name, 'path': event_path, 'operation': event_operation})
    return selected


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.headers.get('Host') not in (f'127.0.0.1:{CFG["port"]}', f'localhost:{CFG["port"]}'):
            self.send_error(403)
            return
        url = urlsplit(self.path)
        if url.path == '/api/snapshot':
            query = parse_qs(url.query)
            try:
                payload = snapshot(int(query.get('amount', ['1'])[0]), query.get('unit', ['day'])[0])
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
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

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
