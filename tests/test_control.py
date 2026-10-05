"""Control API tests with temporary configuration and mocked Windows commands."""
import importlib.util
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load_control(root):
    spec = importlib.util.spec_from_file_location('isolated_control', ROOT / 'control.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.ROOT = root
    return module


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='pc-pulse-control-test-')
        self.root = Path(self.tmp.name)
        self.module = load_control(self.root)
        self.config = {'port': 8765, 'watch': ['C:\\'], 'exclude': [], 'reads': False,
                       'retentionDays': 14, 'maxEvents': 100000, 'pollSeconds': 10,
                       'scanSeconds': 900, 'untouched': 'preserve this'}
        self.module.atomic_json(self.root / 'config.json', self.config)
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), self.module.Handler)
        self.config['controlPort'] = self.httpd.server_port
        self.module.atomic_json(self.root / 'config.json', self.config)
        self.base = 'http://127.0.0.1:' + str(self.httpd.server_port)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.cookie = ''
        self.csrf = ''

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def request(self, path, value=None, *, origin=None, csrf=True, cookie=True):
        headers = {'Origin': self.base if origin is None else origin}
        if cookie:
            headers['Cookie'] = self.cookie
        if value is not None:
            headers['Content-Type'] = 'application/json'
            if csrf:
                headers['X-CSRF-Token'] = self.csrf
        request = Request(self.base + '/api/settings/' + path,
                          data=None if value is None else json.dumps(value).encode(), headers=headers)
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, json.load(response), response.headers
        except HTTPError as error:
            return error.code, json.load(error), error.headers

    def login(self):
        code, data, headers = self.request('setup', {'password': 'local-settings-test-password'})
        self.assertEqual(code, 200)
        self.cookie = headers['Set-Cookie'].split(';')[0]
        self.csrf = data['csrfToken']

    def test_initial_setup_session_and_logout(self):
        self.assertFalse(self.request('session')[1]['configured'])
        self.assertEqual(self.request('config')[0], 403)
        self.assertEqual(self.request('setup', {'password': 'short'})[0], 400)
        self.login()
        self.assertTrue(self.request('session')[1]['authenticated'])
        self.assertNotIn('local-settings-test-password', (self.root / 'settings-auth.json').read_text())
        self.assertEqual(self.request('setup', {'password': 'another-strong-password'})[0], 409)
        self.assertEqual(self.request('logout', {})[0], 200)
        self.assertEqual(self.request('config')[0], 403)
        self.assertEqual(self.request('login', {'password': 'wrong'})[0], 401)
        self.assertEqual(self.request('login', {'password': 'local-settings-test-password'})[0], 200)

    def test_save_backup_revision_and_preserve_unknown_fields(self):
        self.login()
        payload = self.request('config')[1]
        value = {'watch': ['D:\\', 'C:\\Users\\alice'], 'reads': True,
                 'readWatch': ['C:\\Users\\alice'], 'retentionDays': 30}
        code, result, _ = self.request('config', {'config': value, 'revision': payload['revision']})
        self.assertEqual(code, 200)
        self.assertEqual(result['config']['watch'], value['watch'])
        self.assertEqual(result['config']['untouched'], 'preserve this')
        self.assertEqual(result['config']['port'], 8765)
        self.assertEqual(json.loads((self.root / 'config.json.bak').read_text()), self.config)
        self.assertEqual(self.request('config', {'config': value, 'revision': payload['revision']})[0], 409)

    def test_bad_origin_and_csrf_never_mutate(self):
        self.assertEqual(self.request('setup', {'password': 'valid-long-password'}, origin='http://evil.example')[0], 403)
        self.login()
        before = (self.root / 'config.json').read_bytes()
        value = {'config': {'watch': []}, 'revision': self.module.disk_config()[1]}
        self.assertEqual(self.request('config', value, csrf=False)[0], 403)
        self.assertEqual(self.request('config', value, cookie=False)[0], 403)
        self.assertEqual(self.request('config', value, origin='null')[0], 403)
        self.assertEqual((self.root / 'config.json').read_bytes(), before)

    def test_reject_invalid_paths_numbers_and_arbitrary_commands(self):
        self.login()
        revision = self.module.disk_config()[1]
        for value in ({'watch': ['C:relative']}, {'watch': ['C:\\*']}, {'pollSeconds': True},
                      {'scanSeconds': 0}, {'port': 9999}, {'readWatch': ['D:\\outside']}):
            self.assertEqual(self.request('config', {'config': value, 'revision': revision})[0], 400)
        for action in ('install', 'audit', 'Uninstall', 'cmd /c anything'):
            self.assertEqual(self.request('action', {'action': action})[0], 400)
        with self.assertRaises(ValueError):
            self.module.run_command('audit')

    def test_async_action_and_error_reporting(self):
        self.login()
        state = {'installed': True, 'enabled': True, 'state': 'Ready'}
        def command(action):
            if action == 'restart':
                state['state'] = 'Running'
                return dict(state)
            raise RuntimeError('test command failure')
        with patch.object(self.module, 'run_command', side_effect=command):
            code, data, _ = self.request('action', {'action': 'restart'})
            self.assertEqual(code, 202)
            for _ in range(100):
                job = self.request('jobs/' + data['job'])[1]
                if job['state'] != 'running':
                    break
                time.sleep(.01)
            self.assertEqual(job['state'], 'done')
            self.assertEqual(job['status']['state'], 'Running')
            code, data, _ = self.request('action', {'action': 'stop'})
            for _ in range(100):
                job = self.request('jobs/' + data['job'])[1]
                if job['state'] != 'running':
                    break
                time.sleep(.01)
            self.assertEqual(job['state'], 'error')
            self.assertIn('test command failure', job['error'])

    def test_only_fixed_cmd_is_passed_to_subprocess(self):
        script = self.root / 'Start-Background.cmd'
        script.write_text('@echo off\n')
        completed = type('Result', (), {'returncode': 0, 'stdout': '{"state":"Running"}', 'stderr': ''})()
        with patch.object(self.module.subprocess, 'run', return_value=completed) as run:
            self.assertEqual(self.module.run_command('start')['state'], 'Running')
            args, kwargs = run.call_args
            self.assertIn('"' + str(script) + '" api', args[0])
            self.assertTrue(args[0].endswith('api"'))
            self.assertNotIn('shell', kwargs)
            self.assertEqual(kwargs['stdin'], self.module.subprocess.DEVNULL)

    @unittest.skipUnless(os.name == 'nt', 'Windows CMD smoke test')
    def test_status_cmd_works_from_path_with_spaces(self):
        # Status only: never starts, stops, installs, or configures audit.
        folder = self.root / 'path with spaces'
        (folder / 'windows').mkdir(parents=True)
        shutil.copyfile(ROOT / 'Background-Status.cmd', folder / 'Background-Status.cmd')
        shutil.copyfile(ROOT / 'windows' / 'Control-Background.ps1', folder / 'windows' / 'Control-Background.ps1')
        module = load_control(folder)
        status = module.run_command('status')
        self.assertIsInstance(status['installed'], bool)
        self.assertIn('state', status)


if __name__ == '__main__':
    unittest.main()
