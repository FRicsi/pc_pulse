"""Persistent localhost settings/control service; never starts audit setup."""
import hashlib
import hmac
import json
import ntpath
import os
import secrets
import subprocess
import tempfile
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent
CONFIG_LOCK = threading.Lock()
ACTION_LOCK = threading.Lock()
SESSIONS = {}
SESSION_LOCK = threading.Lock()
JOBS = {}
COMMANDS = {'status': 'Background-Status.cmd', 'start': 'Start-Background.cmd',
            'stop': 'Stop-Background.cmd', 'restart': 'Restart-Background.cmd',
            'enable': 'Enable-Autostart.cmd', 'disable': 'Disable-Autostart.cmd'}
NUMBERS = {'retentionDays': (1, 3288), 'maxEvents': (1, 10000000),
           'pollSeconds': (5, 3600), 'scanSeconds': (60, 86400)}
TTL = 900


def disk_config():
    raw = (ROOT / 'config.json').read_bytes()
    return json.loads(raw.decode('utf-8-sig')), hashlib.sha256(raw).hexdigest()


def atomic_json(path, value):
    fd, name = tempfile.mkstemp(prefix='.pc-pulse-', suffix='.tmp', dir=ROOT)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def validate_config(value, current):
    if not isinstance(value, dict):
        raise ValueError('A konfiguráció objektum legyen.')
    editable = {'watch', 'exclude', 'readWatch', 'reads', *NUMBERS}
    if set(value) - editable:
        raise ValueError('Nem szerkeszthető konfigurációs mező.')
    result = dict(current)
    for key in ('watch', 'exclude', 'readWatch'):
        paths = value.get(key, current.get(key, []))
        if not isinstance(paths, list) or len(paths) > 128:
            raise ValueError('Legfeljebb 128 útvonal adható meg mezőnként.')
        normalized = []
        for path in paths:
            if not isinstance(path, str) or not path.strip() or len(path) > 4096 or any(ord(c) < 32 for c in path):
                raise ValueError('Érvénytelen mappaútvonal.')
            path = ntpath.normpath(os.path.expandvars(path.strip()))
            drive, _ = ntpath.splitdrive(path)
            if not drive or not ntpath.isabs(path) or '%' in path or any(c in path for c in '*?"<>|'):
                raise ValueError('Teljes Windows-útvonal szükséges: ' + path)
            if ntpath.normcase(path) not in {ntpath.normcase(p) for p in normalized}:
                normalized.append(path)
        result[key] = normalized
    reads = value.get('reads', current.get('reads', False))
    if not isinstance(reads, bool):
        raise ValueError('Az olvasás mező logikai érték legyen.')
    result['reads'] = reads
    for key, (low, high) in NUMBERS.items():
        number = value.get(key, current.get(key, {'retentionDays': 14, 'maxEvents': 100000, 'pollSeconds': 10, 'scanSeconds': 900}[key]))
        if type(number) is not int or not low <= number <= high:
            raise ValueError(f'{key}: {low}–{high} közötti egész szám szükséges.')
        result[key] = number
    if any(not any(storage_beneath(p, root) for root in result['watch']) for p in result['readWatch']):
        raise ValueError('Az olvasásra figyelt mappáknak a figyelt útvonalakon belül kell lenniük.')
    return result


def storage_beneath(path, root):
    try:
        return ntpath.commonpath([ntpath.normcase(path), ntpath.normcase(root)]) == ntpath.normcase(root)
    except ValueError:
        return False


def save_config(value, revision):
    with CONFIG_LOCK:
        current, actual = disk_config()
        if revision != actual:
            raise FileExistsError('A konfiguráció közben megváltozott. Töltsd újra a beállításokat.')
        result = validate_config(value, current)
        atomic_json(ROOT / 'config.json.bak', current)
        atomic_json(ROOT / 'config.json', result)
        return disk_config()


def auth_record():
    path = ROOT / 'settings-auth.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else None


def password_hash(password, salt):
    return hashlib.scrypt(password.encode('utf-8'), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()


def set_password(password):
    if not isinstance(password, str) or not 12 <= len(password) <= 256:
        raise ValueError('A jelszó 12–256 karakter hosszú legyen.')
    with CONFIG_LOCK:
        if auth_record() is not None:
            raise FileExistsError('A jelszó már be van állítva.')
        salt = secrets.token_hex(16)
        atomic_json(ROOT / 'settings-auth.json', {'salt': salt, 'hash': password_hash(password, salt)})


def run_command(action):
    if action not in COMMANDS:
        raise ValueError('Nem engedélyezett művelet.')
    if os.name != 'nt':
        raise OSError('A háttérvezérlés Windows alatt érhető el.')
    script = ROOT / COMMANDS[action]
    if not script.is_file():
        raise FileNotFoundError('Hiányzó vezérlőfájl: ' + script.name)
    if any(c in str(script) for c in '%!"\r\n'):
        raise ValueError('A vezérlőfájl elérési útja nem használható biztonságosan.')
    # File and argument come exclusively from this allowlist, never the HTTP request.
    executable = str(Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'cmd.exe')
    # A raw Windows command line avoids list2cmdline's backslash-escaped quotes,
    # which cmd.exe does not understand. /s removes this one outer quote pair.
    command_line = f'"{executable}" /d /v:off /s /c ""{script}" api"'
    completed = subprocess.run(command_line,
                               cwd=ROOT, stdin=subprocess.DEVNULL, capture_output=True,
                               encoding='utf-8-sig', errors='replace', timeout=90,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    output = completed.stdout.strip()
    if completed.returncode:
        raise RuntimeError((completed.stderr.strip() or output or 'A vezérlés sikertelen.')[-4000:])
    # PowerShell emits a single JSON object in API mode.
    return json.loads(output)


def queue_action(action):
    if action not in COMMANDS or action == 'status':
        raise ValueError('Nem engedélyezett művelet.')
    if not ACTION_LOCK.acquire(blocking=False):
        raise BlockingIOError('Már folyamatban van egy vezérlési művelet.')
    token = secrets.token_urlsafe(18)
    JOBS[token] = {'action': action, 'state': 'running'}

    def worker():
        try:
            status = run_command(action)
            JOBS[token] = {'action': action, 'state': 'done', 'status': status}
        except Exception as error:
            JOBS[token] = {'action': action, 'state': 'error', 'error': str(error)}
        finally:
            ACTION_LOCK.release()
    # Retain only a bounded number of previous results.
    for old in list(JOBS)[:-30]:
        JOBS.pop(old, None)
    threading.Thread(target=worker, daemon=True).start()
    return token


def drive_options(config):
    paths = {ntpath.splitdrive(p)[0] + '\\' for p in config.get('watch', []) if ntpath.splitdrive(p)[0]}
    if os.name == 'nt':
        import ctypes
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        paths.update(chr(65 + i) + ':\\' for i in range(26) if mask & (1 << i))
    return [{'path': p, 'available': os.path.isdir(p)} for p in sorted(paths)]


class Handler(BaseHTTPRequestHandler):
    def allowed(self, mutation=False):
        config, _ = disk_config()
        ports = (int(config['port']), int(config.get('controlPort', int(config['port']) + 1)))
        hosts = [f'{host}:{port}' for host in ('localhost', '127.0.0.1') for port in ports]
        if self.client_address[0] not in ('127.0.0.1', '::1') or self.headers.get('Host') not in hosts:
            return False
        origin = self.headers.get('Origin')
        return origin in ['http://' + host for host in hosts] if mutation or origin else True

    def respond(self, status, payload, headers=None):
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        if self.allowed() and self.headers.get('Origin'):
            self.send_header('Access-Control-Allow-Origin', self.headers['Origin'])
            self.send_header('Access-Control-Allow-Credentials', 'true')
            self.send_header('Vary', 'Origin')
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def session(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get('Cookie', ''))
        except Exception:
            return None
        item = cookie.get('pc_pulse_control')
        if not item:
            return None
        with SESSION_LOCK:
            entry = SESSIONS.get(item.value)
            if not entry or entry['expires'] < time.monotonic():
                SESSIONS.pop(item.value, None)
                return None
            entry['expires'] = time.monotonic() + TTL
            return entry

    def do_OPTIONS(self):
        if not self.allowed(mutation=True):
            self.respond(403, {'error': 'Érvénytelen Origin.'})
            return
        self.respond(200, {}, {'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
                               'Access-Control-Allow-Headers': 'Content-Type, X-CSRF-Token'})

    def do_GET(self):
        if not self.allowed():
            self.respond(403, {'error': 'Csak helyi elérés engedélyezett.'})
            return
        url = urlsplit(self.path)
        if url.path == '/api/control-info':
            config, _ = disk_config()
            self.respond(200, {'controlPort': int(config.get('controlPort', int(config['port']) + 1)),
                               'monitorPort': int(config['port']), 'isControl': True})
            return
        if url.path == '/api/settings/session':
            entry = self.session()
            self.respond(200, {'authenticated': entry is not None, 'configured': auth_record() is not None,
                               'csrfToken': entry['csrf'] if entry else None})
            return
        if url.path in ('/', '/PC-Pulse.html'):
            body = (ROOT / 'PC-Pulse.html').read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)
            return
        if url.path in ('/api/snapshot', '/api/events'):
            config, _ = disk_config()
            try:
                with urlopen(f'http://127.0.0.1:{config["port"]}' + self.path, timeout=15) as response:
                    self.respond(200, json.load(response))
            except (URLError, TimeoutError, OSError):
                self.respond(503, {'error': 'A monitorozás nem elérhető. A Beállítások vezérlőjéből elindítható.'})
            return
        if not self.session():
            self.respond(403, {'error': 'Jelentkezz be a beállításokhoz.'})
            return
        try:
            if url.path == '/api/settings/config':
                config, revision = disk_config()
                self.respond(200, {'config': config, 'revision': revision, 'configPath': str(ROOT / 'config.json'),
                                   'drives': drive_options(config)})
            elif url.path == '/api/settings/status':
                config, _ = disk_config()
                try:
                    with urlopen(f'http://127.0.0.1:{config["port"]}/api/runtime', timeout=2) as response:
                        monitor = json.load(response)
                except (URLError, TimeoutError, OSError):
                    monitor = None
                self.respond(200, {'status': run_command('status'), 'commands': COMMANDS, 'monitor': monitor,
                                   'controlRoot': str(ROOT)})
            elif url.path.startswith('/api/settings/jobs/'):
                job = JOBS.get(url.path.rsplit('/', 1)[-1])
                self.respond(200 if job else 404, job or {'error': 'Nincs ilyen művelet.'})
            else:
                self.respond(404, {'error': 'Nincs ilyen végpont.'})
        except Exception as error:
            self.respond(503, {'error': str(error)})

    def do_POST(self):
        if not self.allowed(mutation=True):
            self.respond(403, {'error': 'Érvénytelen Origin vagy Host.'})
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if self.headers.get_content_type() != 'application/json' or not 0 < length <= 65536:
                raise ValueError('Érvénytelen JSON-kérés.')
            value = json.loads(self.rfile.read(length))
            if not isinstance(value, dict):
                raise ValueError('Érvénytelen kérés.')
            path = urlsplit(self.path).path
            if path in ('/api/settings/setup', '/api/settings/login'):
                if path.endswith('setup'):
                    set_password(value.get('password'))
                record = auth_record()
                password = value.get('password')
                if not record or not isinstance(password, str) or len(password) > 256 or not hmac.compare_digest(password_hash(password, record['salt']), record['hash']):
                    self.respond(401, {'error': 'Hibás jelszó.'})
                    return
                token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
                with SESSION_LOCK:
                    for old in [k for k, v in SESSIONS.items() if v['expires'] < time.monotonic()]:
                        SESSIONS.pop(old, None)
                    SESSIONS[token] = {'csrf': csrf, 'expires': time.monotonic() + TTL}
                self.respond(200, {'csrfToken': csrf}, {'Set-Cookie': f'pc_pulse_control={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={TTL}'})
                return
            entry = self.session()
            if not entry or not hmac.compare_digest(self.headers.get('X-CSRF-Token', ''), entry['csrf']):
                self.respond(403, {'error': 'Lejárt munkamenet vagy érvénytelen CSRF-token. Jelentkezz be újra.'})
                return
            if path == '/api/settings/config':
                config, revision = save_config(value.get('config'), value.get('revision'))
                self.respond(200, {'config': config, 'revision': revision, 'restartRequired': True})
            elif path == '/api/settings/action':
                token = queue_action(value.get('action'))
                self.respond(202, {'job': token})
            elif path == '/api/settings/logout':
                cookie = SimpleCookie(self.headers.get('Cookie', ''))
                with SESSION_LOCK:
                    SESSIONS.pop(cookie['pc_pulse_control'].value, None)
                self.respond(200, {'ok': True}, {'Set-Cookie': 'pc_pulse_control=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0'})
            else:
                self.respond(404, {'error': 'Nincs ilyen végpont.'})
        except FileExistsError as error:
            self.respond(409, {'error': str(error)})
        except BlockingIOError as error:
            self.respond(409, {'error': str(error)})
        except (ValueError, TypeError) as error:
            self.respond(400, {'error': str(error)})
        except Exception as error:
            self.respond(500, {'error': str(error)})

    def log_message(self, *_):
        pass


if __name__ == '__main__':
    config, _ = disk_config()
    port = int(config.get('controlPort', int(config['port']) + 1))
    if port == int(config['port']) or not 1 <= port <= 65535:
        raise ValueError('A vezérlő portja különbözzön a monitorozás portjától.')
    httpd = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    print(f'PC Pulse vezérlő: http://127.0.0.1:{port}', flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
