"""QA preview: the real server and UI with a labelled FAKE Google, seeded into one of three states.

    python3 qa/preview.py --port 18890 --scenario fresh|paste|connected

Adds /__qa/host.html, a same-origin stand-in for the Wizard shell that frames the app and posts
Appearance v1 messages (skin from ?skin=). Never use this outside local QA.
"""
import argparse
import json
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / 'app'), str(ROOT / 'tests')]
import server  # noqa: E402
from fakes import CLIENT_JSON, FakeGoogle, sign_in  # noqa: E402

TOKENS = json.loads((Path(__file__).resolve().parent / 'skins.json').read_text())

HOST = '''<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>QA host</title><style>:root{color-scheme:%(scheme)s}html,body{margin:0;height:100%%;background:%(backdrop)s}iframe{display:block;width:100%%;height:100%%;border:0;color-scheme:inherit}</style>
<iframe id="app" src="/"></iframe>
<script>
const skin=%(skin)s, frame=document.getElementById('app'); window.acks=[];
function send(){frame.contentWindow.postMessage({type:'wizard:appearance',appearance:{version:1,revision:Date.now(),skin:skin.name,colorScheme:skin.colorScheme,presentation:'window',tokens:skin.tokens}},location.origin);}
addEventListener('message',e=>{if(e.source!==frame.contentWindow)return;if(e.data&&e.data.type==='wizard:appearance:ready')send();if(e.data&&e.data.type==='wizard:appearance:ack')acks.push(e.data.revision);});
window.reskin=s=>{Object.assign(skin,s);send();};
</script>'''


def seed(app, scenario):
    broker, fake = app.broker, app.broker.transport
    if scenario == 'fresh':
        return
    broker.set_setup(step='project', done=True)
    broker.set_setup(step='branding', done=True)
    broker.set_setup(step='publish', done=True)
    if scenario == 'paste':
        return
    broker.save_client(CLIENT_JSON)
    personal = sign_in(broker, fake, 'seed', '111', 'sam@gmail.com', ['gmail.search', 'gmail.read', 'gmail.send', 'calendar.read', 'calendar.write', 'drive.search'])[0]['account']
    work = sign_in(broker, fake, 'seed', '222', 'sam@studio.example', ['gmail.search', 'calendar.read', 'drive.search', 'drive.read'])[0]['account']
    fake.disabled.add('drive')
    broker.check_apis(personal)
    broker.check_apis(work)
    broker.set_grant('agent:main', personal, ['gmail.search', 'gmail.read', 'calendar.read'])
    broker.set_grant('agent:main', work, ['calendar.read'])
    broker.set_grant('app:meeting-recorder', work, ['calendar.read'])
    broker.request_access('app:music', ['youtube.read'], reason='Match your playlists to YouTube videos')
    broker.request_access('agent:research', ['drive.search', 'drive.read'], work, 'Find the Q3 planning doc you mentioned')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=18890)
    parser.add_argument('--scenario', default='connected', choices=['fresh', 'paste', 'connected'])
    args = parser.parse_args()
    config = server.Config({'PORT': str(args.port), 'DATA_DIR': tempfile.mkdtemp(prefix='wizard-google-qa-'),
                            'GOOGLE_DEV_MODE': 'loopback-single-owner', 'WIZARD_SHELL_PATH': '/'})
    app = server.App(config, transport=FakeGoogle())
    seed(app, args.scenario)
    base = server.handler_for(app)

    class Handler(base):
        def do_GET(self):
            if self.path.startswith('/__qa/host.html'):
                name = self.path.partition('skin=')[2] or 'nebula'
                skin = TOKENS[name]
                raw = (HOST % {'skin': json.dumps({'name': name, **skin}), 'backdrop': skin['backdrop'], 'scheme': skin['colorScheme']}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                return self.wfile.write(raw)
            return super().do_GET()

    from http.server import ThreadingHTTPServer
    httpd = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    print(f'preview {args.scenario} on http://127.0.0.1:{args.port}/ (fake Google)', flush=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    threading.Event().wait()


if __name__ == '__main__':
    main()
