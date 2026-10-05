"""Browse only the embedded APK catalog; no CDN or decryption key is needed."""
import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from nnnotes.addressables import parse
from nnnotes.apkset import ApkSet
from nnnotes.config import Config

ROOT = Path(__file__).resolve().parents[1]
PAGE = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>nnnotes · APK 资源目录</title>
<style>body{font:16px system-ui;margin:32px auto;padding:0 24px;max-width:1200px;color:#202530}
input{font:inherit;width:95%;padding:12px}table{width:100%;border-collapse:collapse;margin-top:24px}
td,th{padding:10px;text-align:left;border-bottom:1px solid #ddd;overflow-wrap:anywhere}
th:first-child{width:40%}p{line-height:1.7}small{color:#586174}</style>
<h1>nnnotes · APK 资源目录</h1>
<p>当前显示安装包自带的离线目录，可搜索资源键和包内路径。<br>
在线资源浏览、资源解密及 masterdata 解码需要补齐本地配置中的 API/CDN 和解密参数。</p>
<input id="search" aria-label="搜索资源" placeholder="搜索资源键或路径，例如 UI/、Live/、Adv/">
<p id="status">正在读取目录…</p><small>为便于浏览，每次最多显示前 200 项。</small>
<table><thead><tr><th>资源键</th><th>包内路径</th></tr></thead><tbody id="rows"></tbody></table>
<script>
let entries=[];const search=document.querySelector('#search'), rows=document.querySelector('#rows');
function render(){const q=search.value.toLowerCase(),found=entries.filter(e=>(e.primary_key+' '+e.internal_id).toLowerCase().includes(q));
document.querySelector('#status').textContent=`匹配 ${found.length} / ${entries.length} 项`;
rows.replaceChildren(...found.slice(0,200).map(e=>{const tr=document.createElement('tr');
for(const value of [e.primary_key,e.internal_id]){const td=document.createElement('td');td.textContent=value;tr.append(td);}return tr;}));}
search.addEventListener('input',render);
fetch('/catalog.json').then(r=>{if(!r.ok)throw Error(r.status);return r.json();}).then(data=>{entries=data;render();})
.catch(()=>document.querySelector('#status').textContent='目录读取失败，请查看终端日志。');
</script></html>'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['browse', 'catalog'])
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--prefix', default='')
    parser.add_argument('--limit', type=int, default=20)
    args = parser.parse_args()
    cfg = Config.load(ROOT / 'nnnotes.toml')
    with ApkSet(cfg.require_path('paths', 'apk')) as apk:
        entries = parse(apk.read('assets/aa/catalog.bin'))
    if args.command == 'catalog':
        keys = sorted({entry['primary_key'] for entry in entries if entry['primary_key'].startswith(args.prefix)})
        print('\n'.join(keys[:args.limit]))
        print(f'# {len(keys)} keys in the embedded APK catalog')
        return
    data = json.dumps(entries, ensure_ascii=False).encode('utf-8')

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = urlsplit(self.path).path
            if path == '/':
                body, kind = PAGE.encode('utf-8'), 'text/html; charset=utf-8'
            elif path == '/catalog.json':
                body, kind = data, 'application/json; charset=utf-8'
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(body)

    with ThreadingHTTPServer(('127.0.0.1', args.port), Handler) as server:
        print(f'nnnotes APK browser: http://127.0.0.1:{args.port}/ ({len(entries)} entries)', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
