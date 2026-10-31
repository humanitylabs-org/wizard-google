"""QA preview: the real server and UI with a labelled FAKE Google, seeded into one of three states.

    python3 qa/preview.py --port 18890 --scenario fresh|paste|connected|quick|quick-connected|composio|composio-ready [--core <wizard-ux>/web/core]

composio*: Quick connect through a FAKE Composio (tests/test_quick.py). Paste ak_good_key_1234567890 to see a key accepted,
anything else of key shape to see Composio's 401 path.

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
import urllib.parse  # noqa: E402
from fakes import CLIENT_JSON, SERVICE, FakeGog, FakeGoogle, FakeSigninService, sign_in  # noqa: E402
from test_quick import GOOD, FakeComposio  # noqa: E402

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


def quick_sign_in(broker, google_fake, sub, email, services):
    started = broker.start('seed', 'https://box.tail1234.ts.net', '/wizard-google/', None, [], services)
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(started['authorization_url']).query))
    google_fake.last_nonce = query['nonce']
    google_fake.issue('c-' + sub, sub, email, sorted(query['scope'].split()))
    return broker.finish(query['state'], 'seed', 'c-' + sub)['account']


def seed(app, scenario):
    broker, fake = app.broker, app.broker.transport
    if scenario in ('fresh', 'quick', 'composio'):
        return
    if scenario == 'composio-ready':
        q = broker.quick
        q.add_key(GOOD, 'Personal')
        for service in ('gmail', 'calendar'):
            account = next(iter(q.section()['accounts']), None)
            q.start('seed', service, account, None)
            app.composio_fake.activate()
            q.status('seed')
        broker.mailcal_call('agent:main', 'mail_search', {'query': 'is:unread'})
        return
    if scenario == 'quick-connected':
        work = quick_sign_in(broker, fake.google, '222', 'alex@studio.example', ['gmail', 'calendar', 'drive', 'contacts'])
        personal = quick_sign_in(broker, fake.google, '111', 'alex.p@gmail.com', ['gmail', 'calendar'])
        broker.set_label(work, 'Work')
        broker.set_label(personal, 'Personal')
        broker.run_gog('agent:main', ['gmail', 'search', 'is:unread newer_than:1d'], 'work')
        broker.run_gog('agent:main', ['calendar', 'events', '--today'], 'personal')
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
    parser.add_argument('--scenario', default='connected', choices=['fresh', 'paste', 'connected', 'quick', 'quick-connected', 'composio', 'composio-ready'])
    parser.add_argument('--core', default='', help='wizard-ux web/core folder: serves the real shared app-client.js at /wizard-os-home/core/')
    args = parser.parse_args()
    quick = args.scenario.startswith('quick')
    config = server.Config({'PORT': str(args.port), 'DATA_DIR': tempfile.mkdtemp(prefix='wizard-google-qa-'),
                            'GOOGLE_DEV_MODE': 'loopback-single-owner', 'WIZARD_SHELL_PATH': '/', 'GOOGLE_SIGNIN_SERVICE': SERVICE if quick else 'off'})
    google_fake = FakeGoogle()
    composio_fake = FakeComposio()
    app = server.App(config, transport=FakeSigninService(google_fake) if quick else google_fake, composio_transport=composio_fake)
    app.composio_fake = composio_fake
    app.broker.gog = FakeGog()
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
            if self.path.startswith('/wizard-os-home/core/') and args.core:
                name = self.path.split('?')[0].rsplit('/', 1)[1]
                path = Path(args.core) / name
                if path.is_file() and path.suffix == '.js':
                    raw = path.read_bytes()
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/javascript; charset=utf-8')
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
