"""Run the actual dashboard in headless Edge against an isolated fixture."""
import gc
import json
import re
import subprocess
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

from test_dashboard import ROOT, fixture, load_server

CHECKS = r'''
(async()=>{
 const errors=[];const check=(ok,message)=>{if(!ok)errors.push(message)};
 try{
  await refresh();clearTimeout(timer);
  check($('userFilter').value==='', 'all users default');
  check($('userFilter').querySelectorAll('optgroup').length===2,'domain groups');
  check($('count').textContent==='3','all event count');
  for(const button of document.querySelectorAll('nav button')){
   button.click();check(!$(button.dataset.view).classList.contains('hidden'),'navigation');
  }
  document.querySelector('[data-view="overview"]').click();
  document.querySelector('.drive-choice').click();
  check(selectedDrive==='C:\\','drive selection');
  check(document.querySelectorAll('#chart polygon').length===3,'folder stack');
  check(new Set([...document.querySelectorAll('#chart polygon')].map(p=>p.getAttribute('fill'))).size===3,'distinct colors');
  check(!$('storageLegend').textContent.includes('alice'),'no parent child double count');
  check($('storageLegend').textContent.includes('-'),'negative folder delta');
  document.querySelectorAll('.drive-choice')[1].click();
  check(selectedDrive==='D:\\'&&$('storageLegend').textContent.includes('Docs'),'second drive');
  check(!$('storageLegend').textContent.includes('Games'),'only selected drive folders');
  $('userFilter').value='DOMAIN-A\\alice';$('userFilter').dispatchEvent(new Event('change'));
  await refresh();clearTimeout(timer);
  check($('count').textContent==='2','global dashboard user scope');
  check($('identities').textContent.includes('1 '),'one user identity');
  check(!$('programList').textContent.includes('game.exe'),'global program scope');
  check(!$('folderList').textContent.includes('Games'),'global folder scope');
  check(events.length===2&&events.every(e=>e.user==='DOMAIN-A\\alice'),'global event scope');
  check(data.chart.buckets.reduce((n,b)=>n+b.count,0)===2,'global event chart scope');
  check(selectedDrive==='D:\\','drive preserved across user filter');
  check($('storageNote').textContent.includes('User'),'physical measurement scope explained');
  const program=document.querySelector('#programList button');program.click();await refreshEvents();
  check(events.length===1&&events[0].program==='editor.exe','program drilldown');
  reset();await refreshEvents();
  check($('userFilter').value==='DOMAIN-A\\alice'&&events.length===2,'local reset preserves global user');
  $('operation').value=$('operation').options[2].value;await refreshEvents();
  check(events.length===1&&events[0].program==='reader.exe','operation filter');
  reset();await refreshEvents();
  let csv;const create=URL.createObjectURL;
  URL.createObjectURL=b=>{csv=b;return create(b)};
  HTMLAnchorElement.prototype.click=function(){};$('export').click();
  const text=await csv.text();check(text.includes('DOMAIN-A')&&!text.includes('DOMAIN-B'),'scoped CSV');
  $('userFilter').value='DOMAIN-B\\alice';await refresh();clearTimeout(timer);
  check($('count').textContent==='1'&&$('programList').textContent.includes('game.exe'),'same username different domain');
  $('userFilter').value='';await refresh();clearTimeout(timer);$('allDrives').click();
  check(selectedDrive===''&&$('count').textContent==='3','restore all drives/users');
  const older=JSON.parse(JSON.stringify(data));older.storageCharts['C:\\'].series=[];
  apply(older);document.querySelector('.drive-choice').click();
  check(!document.querySelector('#chart polygon')&&document.querySelector('#chart polyline'),'older data fallback');
 }catch(error){errors.push(error.stack)}
 document.body.innerHTML='<pre id="verification">'+JSON.stringify({errors})+'</pre>';
})();
'''


def main():
    module = load_server()
    html = (ROOT / 'PC-Pulse.html').read_text(encoding='utf-8')
    page = html.replace('</html>', '<script>' + CHECKS + '</script></html>')
    with tempfile.TemporaryDirectory(prefix='pc-pulse-browser-', ignore_cleanup_errors=True) as tmp:
        fixture(module, Path(tmp) / 'fixture.sqlite')

        class VerifyHandler(module['Handler']):
            def do_GET(self):
                if self.path == '/verify':
                    body = page.encode('utf-8')
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html; charset=utf-8')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    try:
                        super().do_GET()
                    except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                        pass  # Switching the global filter aborts the previous request.

        server = ThreadingHTTPServer(('127.0.0.1', 0), VerifyHandler)
        module['CFG']['port'] = server.server_port
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            result = subprocess.run([
                r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
                '--headless', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
                '--user-data-dir=' + str(Path(tmp) / 'edge'), '--dump-dom',
                '--virtual-time-budget=15000', 'http://127.0.0.1:' + str(server.server_port) + '/verify',
            ], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=45,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            match = re.search(r'<pre id="verification">(.*?)</pre>', result.stdout, re.S)
            if not match:
                raise AssertionError(result.stderr[-1500:] + result.stdout[-1500:])
            report = json.loads(match.group(1))
            print('Edge dashboard checks:', report)
            assert not report['errors']
        finally:
            server.shutdown()
            server.server_close()
            gc.collect()


if __name__ == '__main__':
    main()
