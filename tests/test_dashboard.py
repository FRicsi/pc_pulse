"""Isolated tests: no production database, collector threads, or audit setup."""
import ast
import gc
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_server():
    tree = ast.parse((ROOT / 'server.py').read_text(encoding='utf-8'))
    tree.body = [n for n in tree.body if not isinstance(n, (ast.With, ast.If))
                 and not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
                          and isinstance(n.value.func, ast.Attribute)
                          and n.value.func.attr == 'mkdir')]
    module = {'__file__': str(ROOT / 'server.py')}
    exec(compile(tree, 'server.py', 'exec'), module)
    return module


SCHEMA = '''
CREATE TABLE events(id INTEGER PRIMARY KEY,time TEXT,user TEXT,program TEXT,path TEXT,operation TEXT);
CREATE TABLE scans(time REAL,path TEXT,bytes INTEGER,errors INTEGER);
CREATE TABLE drives(time REAL,path TEXT,total INTEGER,free INTEGER);
CREATE TABLE state(key TEXT PRIMARY KEY,value TEXT);
'''


def fixture(module, database):
    module['DB'] = database
    module['WATCH'] = ['C:\\', 'D:\\']
    stamp = module['now']()
    end = time.time() - 1
    start = end - 900
    with module['connect']() as con:
        con.executescript(SCHEMA)
        con.executemany('INSERT INTO events VALUES(?,?,?,?,?,?)', [
            (1, stamp, 'DOMAIN-A\\alice', 'editor.exe', 'C:\\Users\\report.txt', 'Írás'),
            (2, stamp, 'DOMAIN-B\\alice', 'game.exe', 'C:\\Games\\game.bin', 'Írás'),
            (3, stamp, 'DOMAIN-A\\alice', 'reader.exe', 'D:\\Docs\\report.txt', 'Olvasás'),
        ])
        con.executemany('INSERT INTO drives VALUES(?,?,?,?)', [
            (start, 'C:\\', 1000, 600), (end, 'C:\\', 1000, 550),
            (start, 'D:\\', 1000, 800), (end, 'D:\\', 1000, 790),
        ])
        con.executemany('INSERT INTO scans VALUES(?,?,?,?)', [
            (start, 'C:\\', 400, 0), (end, 'C:\\', 440, 0),
            (start, 'C:\\Users', 100, 0), (end, 'C:\\Users', 180, 0),
            (start, 'C:\\Users\\alice', 60, 0), (end, 'C:\\Users\\alice', 120, 0),
            (start, 'C:\\Games', 200, 0), (end, 'C:\\Games', 160, 0),
            (start, 'D:\\Docs', 100, 0), (end, 'D:\\Docs', 110, 0),
        ])


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='pc-pulse-test-')
        self.server = load_server()
        fixture(self.server, Path(self.tmp.name) / 'test.sqlite')

    def tearDown(self):
        gc.collect()
        self.tmp.cleanup()

    def test_disjoint_signed_stack_and_residual(self):
        chart = self.server['snapshot']()['storageCharts']['C:\\']
        series = {s['path']: s['values'][-1] for s in chart['series']}
        self.assertEqual(series, {'C:\\Games': -40, 'C:\\Users': 80, None: 10})
        self.assertNotIn('C:\\Users\\alice', series)
        self.assertEqual(sum(series.values()), chart['totalDelta'][-1])
        self.assertEqual(chart['totalDelta'][-1], 50)
        self.assertEqual(self.server['snapshot']()['storageCharts']['D:\\']['totalDelta'][-1], 10)

    def test_user_scope_preserves_domain_and_physical_measurements(self):
        all_users = self.server['snapshot']()
        scoped = self.server['snapshot'](global_user='DOMAIN-A\\alice')
        self.assertEqual(scoped['eventCount'], 2)
        self.assertEqual(scoped['programStats'], [{'program': 'editor.exe', 'count': 1}])
        self.assertEqual(scoped['identities'], {'programs': 2, 'users': 1})
        self.assertEqual(sum(b['count'] for b in scoped['chart']['buckets']), 2)
        self.assertEqual(scoped['users'], ['DOMAIN-A\\alice', 'DOMAIN-B\\alice'])
        self.assertEqual(scoped['storageCharts'], all_users['storageCharts'])
        self.assertNotIn('C:\\Games', [f['path'] for f in scoped['folders']])
        self.assertEqual(len(self.server['fetch_events'](user='DOMAIN-B\\alice')), 1)

    def test_empty_user_and_local_filters(self):
        scoped = self.server['snapshot'](global_user='missing\\user')
        self.assertEqual(scoped['eventCount'], 0)
        self.assertEqual(scoped['folders'], [])
        self.assertEqual(scoped['programStats'], [])
        chart = self.server['snapshot'](global_user='DOMAIN-A\\alice', path='report', operation='Olvasás')['chart']
        self.assertEqual(sum(b['count'] for b in chart['buckets']), 1)

    def test_missing_and_partial_samples_do_not_invent_folder_deltas(self):
        with self.server['connect']() as con:
            con.execute("UPDATE scans SET errors=1 WHERE path=? AND time=(SELECT MAX(time) FROM scans)", ('C:\\Games',))
        chart = self.server['snapshot']()['storageCharts']['C:\\']
        self.assertEqual(chart['series'], [])
        self.assertEqual(len(chart['history']), 2)
        self.assertTrue(chart['incomplete'])

    def test_old_drive_only_history_keeps_working(self):
        with self.server['connect']() as con:
            con.execute('DELETE FROM scans')
        result = self.server['snapshot']()
        self.assertEqual(result['growth'], 60)
        self.assertEqual(result['storageCharts']['C:\\']['series'], [])

    def test_single_traversal_records_children_and_deleted_folder_zero(self):
        watch = Path(self.tmp.name) / 'watch'
        child = watch / 'nested'
        child.mkdir(parents=True)
        (watch / 'root.txt').write_bytes(b'abc')
        (child / 'file.bin').write_bytes(b'12345')
        breakdown = {}
        self.assertEqual(self.server['folder_size'](str(watch), breakdown), (8, 0))
        self.assertEqual(breakdown[str(child)], [5, 0])
        self.server['WATCH'] = [str(watch)]
        self.server['drive_sizes'] = lambda: []
        self.server['scan_once']()
        (child / 'file.bin').unlink()
        child.rmdir()
        self.server['scan_once']()
        with self.server['connect']() as con:
            latest = con.execute('SELECT bytes,errors FROM scans WHERE path=? ORDER BY time DESC LIMIT 1', (str(child),)).fetchone()
        self.assertEqual(latest, (0, 0))


if __name__ == '__main__':
    unittest.main()
