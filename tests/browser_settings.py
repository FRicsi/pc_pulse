"""Real settings UI/API integration; Windows command side effects are mocked."""
import gc
import json
import re
import subprocess
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

from test_control import load_control
from test_dashboard import ROOT, fixture, load_server

CHECKS = r'''
(async()=>{
 const errors=[];const check=(ok,msg)=>{if(!ok)errors.push(msg)};
 const until=async predicate=>{for(let i=0;i<200;i++){if(predicate())return;await new Promise(r=>setTimeout(r,20))}throw Error('UI wait timeout')};
 try{
  await refresh();clearTimeout(timer);await controlReady;show('settings');await until(()=>!settingsLoading);
  check(!$('settingsLogin').classList.contains('hidden'),'password gate');
  $('settingsPassword').value='browser-settings-password';
  if(!settingsConfigured)$('settingsPasswordRepeat').value='browser-settings-password';
  await $('settingsLogin').onsubmit({preventDefault(){}});
  check(settingsAuthenticated&&!$('settingsBody').classList.contains('hidden'),'setup/login');
  check($('configPath').textContent.includes('config.json'),'real config path');
  check($('retention').value==='14','config initial values');
  const drive=[...document.querySelectorAll('[data-watch-drive]')].find(e=>e.dataset.watchDrive==='D:\\');
  check(drive.checked,'configured drive checked');drive.checked=false;
  $('watch').value='C:\\New Folder';$('retention').value='30';
  await saveSettings(false);check($('saveStatus').textContent.includes('mentve'),'save');
  await loadConfig();check(![...document.querySelectorAll('[data-watch-drive]')].find(e=>e.dataset.watchDrive==='D:\\').checked,'drive saved');
  check($('watch').value==='C:\\New Folder'&&$('retention').value==='30','folder and retention persisted');
  await saveSettings(true);check(controlState.state==='Running','save and restart');
  document.querySelector('[data-settings-tab="control"]').click();await loadControlStatus();
  check(!$('settingsControl').classList.contains('hidden'),'control menu');
  await controlAction('restart');check(controlState.state==='Running','restart');
  await controlAction('stop');check(controlState.state==='Ready','stop');
  check((await manager('session')).authenticated,'control survives monitoring stop');
  await refresh();clearTimeout(timer);check(document.querySelector('.demo').textContent.includes('NINCS KAPCSOLAT'),'offline monitor visible');
  await controlAction('start');check(controlState.state==='Running','start after stop');
  await controlAction('disable');check(!controlState.enabled,'disable');
  check(document.querySelector('[data-control-action="restart"]').disabled,'disabled state prevents restart');
  await controlAction('enable');check(controlState.enabled&&controlState.state==='Running','enable');
  await $('settingsLogout').onclick();check(!settingsAuthenticated&&$('settingsBody').classList.contains('hidden'),'logout');
 }catch(error){errors.push(error.stack)}
 document.body.innerHTML='<pre id="verification">'+JSON.stringify({errors})+'</pre>';
})();
'''


def main():
    with tempfile.TemporaryDirectory(prefix='pc-pulse-settings-browser-', ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        module = load_server()
        fixture(module, root / 'test.sqlite')
        module['ROOT'] = root
        control = load_control(root)
        html = (ROOT / 'PC-Pulse.html').read_text(encoding='utf-8')
        page = html.replace('</html>', '<script>' + CHECKS + '</script></html>')
        (root / 'PC-Pulse.html').write_text(page, encoding='utf-8')
        state = {'installed': True, 'enabled': True, 'state': 'Running', 'lastResult': 0}

        def command(action):
            if action in ('start', 'restart', 'enable'):
                state['state'] = 'Running'
            if action in ('stop', 'disable'):
                state['state'] = 'Ready'
            if action == 'disable':
                state['enabled'] = False
            if action == 'enable':
                state['enabled'] = True
            return dict(state)
        control.run_command = command

        class MonitorHandler(module['Handler']):
            def do_GET(self):
                if state['state'] != 'Running' and self.path.startswith(('/api/runtime', '/api/snapshot', '/api/events')):
                    self.send_json(503, {'error': 'Test monitor is stopped'})
                else:
                    try:
                        super().do_GET()
                    except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                        pass

        monitor = ThreadingHTTPServer(('127.0.0.1', 0), MonitorHandler)
        manager = ThreadingHTTPServer(('127.0.0.1', 0), control.Handler)
        module['CFG'].update({'port': monitor.server_port, 'controlPort': manager.server_port, 'watch': ['C:\\', 'D:\\']})
        control.atomic_json(root / 'config.json', module['CFG'])
        for server in (monitor, manager):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            for index, page_server in enumerate((monitor, manager)):
                control.atomic_json(root / 'config.json', module['CFG'])
                state.update({'installed': True, 'enabled': True, 'state': 'Running' if index == 0 else 'Ready'})
                result = subprocess.run([
                    r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
                    '--headless', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
                    '--user-data-dir=' + str(root / ('edge-' + str(index))), '--dump-dom', '--virtual-time-budget=25000',
                    'http://127.0.0.1:' + str(page_server.server_port) + '/',
                ], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=45,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                match = re.search(r'<pre id="verification">(.*?)</pre>', result.stdout, re.S)
                if not match:
                    raise AssertionError(result.stderr[-1500:] + result.stdout[-1500:])
                report = json.loads(match.group(1))
                print('Edge settings checks, origin', index, '(mocked CMD actions):', report)
                assert not report['errors']
            saved = control.disk_config()[0]
            assert saved['watch'] == ['C:\\', 'C:\\New Folder']
            assert saved['retentionDays'] == 30
        finally:
            for server in (monitor, manager):
                server.shutdown()
                server.server_close()
            gc.collect()


if __name__ == '__main__':
    main()
