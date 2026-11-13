"""cutvelvet local server: review page + feedback JSON + asset library. Stdlib only, binds 127.0.0.1.

    python -m cutvelvet.server <project_dir> [--port 8771]

Review (same contract as the original Node review server):
    POST /save?f=<version>        body = feedback JSON, written verbatim to <project>/feedback/<version>.json
    GET  /feedback?f=<version>    that file, or {} when absent
    GET  /versions                ["ch1_v1", ...] from <project>/review/*_v<N>.mp4
    GET  /<file>                  static: the page (web/), then <project>/review/ (mp4 with HTTP Range)

Library:
    GET  /api/library             index: {folders, clips, status, scanned_at, sprite}
    GET  /api/used                {src: [{timeline, shot, a, b}]} footage seconds used by timelines
    POST /api/library/folders     {"add": "<path>"} or {"remove": "<path>"} -> persists project.json "library"
    POST /api/library/rescan      start an incremental rescan (background)
    GET  /media?src=<src>         a source clip (Range); only files inside registered library folders
    GET  /thumbs/<key>_{p,s}.jpg  cached poster / scrub sprite
    POST /api/library/pick        open the OS folder chooser on this machine, add the chosen folder
    POST /api/library/upload?name=<file>[&dir=<subfolder>]
                                  body = one video file, streamed into <project>/footage/inbox/

Run notes (see runner.py):
    GET  /api/run[?version=v]     {state, version, elapsed, tail, new_version, changes, check}
    POST /api/run                 {"version": "ch2_v1"} -> start the agent on that version's saved notes
    POST /api/run/cancel          stop it
    POST /api/publish             {"timeline": "ch2"} -> cutvelvet publish (no agent) in the same job slot
    GET  /api/publish/estimate?timeline=ch2   shots that will re-render (the ones not cached), jobs, minutes
    GET  /changes?f=<version>     feedback/<version>_changes.md as text ("" when absent)
    *    /api/new...              handled by newcut.py (start a cut from a song + footage + brief)

Rules (see rules.py):
    GET  /api/rules               {rules, groups, defaults} -- seeds <project>/rules.json on first use
    POST /api/rules               {"rules": [...]} -> normalised and written atomically
    POST /api/rules/defaults      save the current rule set as the editor's defaults (~/.cutvelvet/rules.json)
    POST /api/rules/use-defaults  make this project's rules the editor's defaults (only ever on request)

Agent (see agents.py) -- which coding agent this machine starts:
    GET  /api/agent               {choice, model, known: [...], effective: {...}, project: {...}|null}
    POST /api/agent               {choice?, model?, command?, approve_project?} -> the same, updated
    POST /api/agent/test          one trivial, billed prompt through the configured agent (result: kind "agenttest" in /api/run)

    POST /api/agent/prompt        {"name": "run_prompt.md", "digest": d} -> approve this project's own agent instructions
                                  (.cutvelvet/run_prompt.md / newcut_prompt.md) on this machine; unapproved ones are not used

Every request must be addressed to 127.0.0.1/localhost on this port and must not come from another site; every POST
must also carry the X-Cutvelvet-Token header (the token is made at start-up and written into the served pages).

Machine (see settings.py):
    GET  /api/system              {cores, load1, busy, level, summary, lines, cutvelvet: {...}, performance: {...}}
    POST /api/system/performance  {"mode": "light"|"balanced"|"fast"} or {"mode": "custom", "jobs": N}
    GET  /api/run/estimate?version=v   shots likely to re-render, parallel jobs, rough minutes
"""
import argparse
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from . import agents as AGENTS
from . import library as L
from . import rules as RULES
from . import runner as R
from . import settings as SETTINGS

WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'web')
TYPES = {'.html': 'text/html; charset=utf-8', '.json': 'application/json', '.mp4': 'video/mp4', '.m4v': 'video/mp4',
         '.mov': 'video/quicktime', '.webm': 'video/webm', '.mkv': 'video/x-matroska', '.js': 'text/javascript',
         '.css': 'text/css', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png', '.svg': 'image/svg+xml',
         # every audio type New cut accepts as a song (it plays the file itself to preview it; cuts are mixed by ffmpeg)
         '.wav': 'audio/wav', '.mp3': 'audio/mpeg', '.m4a': 'audio/mp4', '.aac': 'audio/aac', '.flac': 'audio/flac',
         '.ogg': 'audio/ogg', '.opus': 'audio/ogg', '.aif': 'audio/aiff', '.aiff': 'audio/aiff'}
VERSION_RE = re.compile(r'^[A-Za-z0-9]+_v\d+\.mp4$')
MAX_BODY = 8 * 1024 * 1024
CHUNK = 256 * 1024
FAVICON = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="6" fill="#0c0c0d"/>'
           '<rect x="5" y="8" width="22" height="5" fill="#ece7dc"/><rect x="5" y="15" width="13" height="4" fill="#ece7dc"/>'
           '<rect x="20" y="15" width="7" height="4" fill="#8d8a84"/><rect x="5" y="21" width="22" height="3" fill="#8d8a84"/></svg>')
INBOX = 'footage/inbox'                 # where dropped files are copied
MAX_UPLOAD = 64 * 1024 ** 3             # per dropped file
PICK_PROMPT = 'Choose a footage folder to add to the library'
_TK_PICK = ('import tkinter as tk, tkinter.filedialog as fd, sys\n'
            'r = tk.Tk(); r.withdraw(); r.attributes("-topmost", True)\n'
            'sys.stdout.write(fd.askdirectory(title=sys.argv[1]) or "")\n')


def pick_folder():
    """Open the operating system's folder chooser. Returns (path or None when cancelled, error or None)."""
    try:
        if sys.platform == 'darwin':
            # "activate" brings the chooser (owned by osascript) in front of the browser.
            r = subprocess.run(['osascript', '-e', 'activate', '-e', 'POSIX path of (choose folder with prompt "%s")' % PICK_PROMPT],
                               capture_output=True, text=True, timeout=600)
            if r.returncode != 0:
                return None, None if ('-128' in r.stderr or r.returncode < 0) else (r.stderr.strip() or 'the folder chooser failed')
            return r.stdout.strip() or None, None
        r = subprocess.run([sys.executable, '-c', _TK_PICK, PICK_PROMPT], capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            return None, 'No folder chooser is available here — paste the folder path into the field instead.'
        return r.stdout.strip() or None, None
    except subprocess.TimeoutExpired:
        return None, None
    except OSError:
        return None, 'No folder chooser is available here — paste the folder path into the field instead.'


def _mmss(s):
    s = int(s)
    return '%dm %02ds' % (s // 60, s % 60) if s >= 60 else '%ds' % s


def system_info(app):
    """How busy the machine is, and what cutvelvet itself is doing right now, in plain words."""
    m, perf = SETTINGS.machine(), SETTINGS.performance()
    lines, roots = [], [os.getpid()]
    render = SETTINGS.render_status(app.project_dir)
    if render:
        roots.append(render.get('pid'))
        lines.append('rendering %s: %d of %d shots done, %d at a time' % (render.get('timeline'), render.get('done', 0), render.get('total', 0), render.get('jobs', 1)))
    with app.lib.lock:
        scan = dict(app.lib.status)
    thumbs = {'done': scan['done'], 'total': scan['total']} if scan.get('scanning') and scan.get('total') else None
    if thumbs:
        lines.append('making thumbnails: %d of %d clips' % (thumbs['done'], thumbs['total']))
    proxies = None
    mod = sys.modules.get(__package__ + '.api_timeline')  # only if the live-preview module is loaded; never started from here
    store = getattr(mod, '_stores', {}).get(app.project_dir) if mod else None
    if store is not None:
        try:
            ov = store.overview()
            active = [j for j in ov.get('jobs') or [] if j.get('state') in ('queued', 'running', 'working', 'making')]
            if ov.get('queue') or active:
                proxies = {'queue': ov.get('queue', 0), 'active': len(active)}
                lines.append('making preview proxies: %d to go' % max(len(active), ov.get('queue', 0)))
        except Exception:
            pass
    rs = app.run.status()
    run = None
    if rs['state'] == 'running':
        run = {'version': rs['version'], 'elapsed': rs['elapsed']}
        roots.append((app.run.job or {}).get('pid'))
        what = ('publishing %s' % rs['timeline'] if rs.get('kind') == 'publish' else
                'the agent is building the first cut of %s' % rs['timeline'] if rs.get('kind') == 'newcut' else
                'testing the agent' if rs.get('kind') == 'agenttest' else
                'the agent is working on the notes for %s' % rs['version'] if rs.get('version') else 'the agent is working on %s' % rs['timeline'])
        lines.append('%s (%s so far)' % (what, _mmss(rs['elapsed'])))
    cpu = SETTINGS.tree_cpu(roots)
    using = round(cpu / 100.0, 1)
    # "heavy" is about cutvelvet's own share: half the cores, or a quarter of them on a machine that is already full
    level = 'idle' if not lines else ('heavy' if using >= 0.5 * m['cores'] or (m['busy'] >= 85 and using >= 0.25 * m['cores']) else 'working')
    machine = 'This machine is %d%% busy (load %.1f on %d cores)' % (m['busy'], m['load1'], m['cores'])
    machine += '; cutvelvet is using about %.1f of them.' % using if lines else '; none of that is cutvelvet.' if m['busy'] >= 25 else '.'
    return dict(m, level=level, lines=lines, summary='; '.join(lines) if lines else 'cutvelvet is idle', machine=machine,
                cutvelvet={'render': render, 'thumbs': thumbs, 'proxies': proxies, 'run': run, 'cpu': cpu, 'cores_used': using}, performance=perf)


def clean_name(name):
    """A dropped file's name, safe to create inside the inbox ('' when nothing usable is left)."""
    name = os.path.basename((name or '').replace('\\', '/'))
    name = re.sub(r'[^A-Za-z0-9._ ()+-]', '_', name).strip(' .')
    return name[:150]


def safe(s):
    return re.sub(r'[^A-Za-z0-9_]', '', s or '')


def _natural(s):
    return [int(x) if x.isdigit() else x for x in re.split(r'(\d+)', s)]


_inside = L.inside

# ---------------------------------------------------------------- what keeps other web pages out
# The server has no accounts: whoever can reach it can start an agent and write files. So besides binding to
# 127.0.0.1 it only answers (1) requests addressed to 127.0.0.1/localhost on its own port (DNS rebinding),
# (2) that a browser does not mark as coming from another site (Origin / Sec-Fetch-Site), and (3) for anything that
# changes state (every POST), that carry the token made at start-up. The token is written into the pages this
# server serves (BOOTSTRAP adds it to their same-origin requests), so only those pages know it.
TOKEN_HEADER = 'X-Cutvelvet-Token'
BOOTSTRAP = ('<script nonce="%(nonce)s">(function(){var T="%(token)s",H="' + TOKEN_HEADER + '",same=function(u){try{return new URL(u,location.href).origin===location.origin}catch(e){return false}};'
             'var f=window.fetch;window.fetch=function(i,o){var u=(typeof i==="string"||i instanceof URL)?String(i):(i&&i.url)||"";'
             'if(same(u)){o=Object.assign({},o||{});var h=new Headers(o.headers||((i instanceof Request)?i.headers:undefined));h.set(H,T);o.headers=h}return f.call(this,i,o)};'
             'var X=XMLHttpRequest.prototype,op=X.open,sd=X.send;X.open=function(m,u){this.__cv=same(u);return op.apply(this,arguments)};'
             'X.send=function(){if(this.__cv){try{this.setRequestHeader(H,T)}catch(e){}}return sd.apply(this,arguments)};})();</script>')
# Pages: scripts only from this server and only the ones in the shipped files (each gets the response's nonce), so
# markup injected through a file name, a note or a log line cannot run; nothing is loaded from or sent to other hosts.
PAGE_CSP = ("default-src 'self'; script-src 'self' 'nonce-%s'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
            "media-src 'self' blob:; connect-src 'self' blob:; frame-src 'self'; frame-ancestors 'self'; base-uri 'none'; "
            "form-action 'none'; object-src 'none'")
DATA_CSP = "default-src 'none'; img-src 'self'; media-src 'self'; style-src 'unsafe-inline'; frame-ancestors 'self'"
REVIEW_FILE = re.compile(r'^[A-Za-z0-9_][A-Za-z0-9_.-]*\.(mp4|json)$')   # all the project's review folder is served as


class App:
    def __init__(self, project_dir, port):
        self.project_dir = os.path.abspath(project_dir)
        self.port = port
        proj = L.load_project(self.project_dir)
        self.review = L.abs_path(self.project_dir, proj.get('review') or 'review')
        self.feedback = L.abs_path(self.project_dir, proj.get('feedback') or 'feedback')
        for d in (self.review, self.feedback):  # e.g. a symlink that came with a downloaded project
            if os.path.lexists(d) and not _inside(self.project_dir, d):
                raise SystemExit('cutvelvet: %s leads outside the project folder; refusing to serve or write there' % d)
        self.token = secrets.token_urlsafe(32)  # new at every start; required on every POST (see TOKEN_HEADER)
        self.name = proj.get('name') or os.path.basename(self.project_dir)
        self.lib = L.Library(self.project_dir)
        self.save_lock = threading.Lock()
        self.pick_lock = threading.Lock()
        self.run = R.Runner(self.project_dir, self.feedback, self.review)
        self.runner = self.run  # the name other modules use


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'cutvelvet'
    sys_version = ''  # no Python version in the Server header
    app = None

    # ---------------------------------------------------------- plumbing
    def log_message(self, fmt, *args):
        if self.command != 'GET' or not str(args[1] if len(args) > 1 else '').startswith(('2', '3')):
            sys.stderr.write('%s %s\n' % (self.log_date_time_string(), fmt % args))

    def _send(self, code, body=b'', ctype='text/plain; charset=utf-8', extra=None):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        if 'Cache-Control' not in (extra or {}):
            self.send_header('Cache-Control', 'no-store')
        if self.close_connection:  # e.g. a refused POST whose body was not read: tell the client not to reuse the socket
            self.send_header('Connection', 'close')
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def _json(self, data, code=200):
        self._send(code, json.dumps(data), 'application/json')

    def end_headers(self):
        """Every response, whoever builds it: no sniffing, no embedding or reading from other sites, no referrer."""
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Cross-Origin-Resource-Policy', 'same-origin')
        self.send_header('X-Frame-Options', 'SAMEORIGIN')
        self.send_header('Content-Security-Policy', getattr(self, '_csp', None) or DATA_CSP)
        self._csp = None
        super().end_headers()

    def _local(self):
        """Refuse requests that did not come from a page this server served (DNS rebinding, cross-site requests,
        and any POST without this server's token). See the note above TOKEN_HEADER."""
        ok = {'127.0.0.1:%d' % self.app.port, 'localhost:%d' % self.app.port}
        if (self.headers.get('Host') or '') not in ok:
            return False
        origin = self.headers.get('Origin')
        if origin and urlsplit(origin).netloc not in ok:
            return False
        site = (self.headers.get('Sec-Fetch-Site') or '').lower()
        if site and site not in ('same-origin', 'none'):
            # Another site may link to the page itself (a plain top-level visit); it may not load or embed anything else.
            path = urlsplit(self.path).path
            if not (self.command in ('GET', 'HEAD') and self.headers.get('Sec-Fetch-Mode') == 'navigate'
                    and self.headers.get('Sec-Fetch-Dest') == 'document' and (path == '/' or path.endswith('.html'))):
                return False
        if self.command == 'POST':
            return hmac.compare_digest((self.headers.get(TOKEN_HEADER) or '').encode('utf-8', 'replace'), self.app.token.encode())
        return self.command in ('GET', 'HEAD')

    def _page(self, path):
        """One of the shipped pages, with this server's token and a script nonce written in (never cached)."""
        try:
            with open(path, encoding='utf-8') as f:
                html = f.read()
        except OSError:
            return self._send(404, 'not found')
        nonce = secrets.token_urlsafe(16)
        html = re.sub(r'<script(?=[\s>])', '<script nonce="%s"' % nonce, html)
        boot = BOOTSTRAP % {'nonce': nonce, 'token': self.app.token}
        html = html.replace('<head>', '<head>' + boot, 1) if '<head>' in html else boot + html
        self._csp = PAGE_CSP % nonce
        return self._send(200, html, 'text/html; charset=utf-8')

    def _file(self, path, cache='no-store'):
        try:
            st = os.stat(path)
            f = open(path, 'rb')
        except OSError:
            return self._send(404, 'not found')
        with f:
            size = st.st_size
            ctype = TYPES.get(os.path.splitext(path)[1].lower(), 'application/octet-stream')
            a, b, code = 0, size - 1, 200
            m = re.match(r'\s*bytes=(\d*)-(\d*)\s*$', self.headers.get('Range') or '')
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    a = int(m.group(1))
                    b = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
                else:  # suffix range: the last N bytes
                    a = max(0, size - int(m.group(2)))
                if a > b or a >= size:
                    return self._send(416, 'range not satisfiable', extra={'Content-Range': 'bytes */%d' % size})
                code = 206
            self.send_response(code)
            self.send_header('Content-Type', ctype)
            self.send_header('Accept-Ranges', 'bytes')
            self.send_header('Content-Length', str(b - a + 1))
            if code == 206:
                self.send_header('Content-Range', 'bytes %d-%d/%d' % (a, b, size))
            else:
                self.send_header('Cache-Control', cache)
            self.end_headers()
            if self.command == 'HEAD':
                return
            f.seek(a)
            left = b - a + 1
            try:
                while left > 0:
                    buf = f.read(min(CHUNK, left))
                    if not buf:
                        break
                    self.wfile.write(buf)
                    left -= len(buf)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True  # the browser seeks by dropping the connection; that is normal

    def _body(self):
        n = int(self.headers.get('Content-Length') or 0)
        if n < 0 or n > MAX_BODY:
            raise ValueError('body too large')
        return self.rfile.read(n) if n else b''

    def _newcut(self):
        """/api/new... (starting a cut from a song, footage and a brief) lives in newcut.py, which reads its own body."""
        try:
            from . import newcut
        except ImportError:
            if self.command == 'POST':
                self.close_connection = True  # body not read
            return self._json({'ok': False, 'error': 'the New cut module is not installed'}, 404)
        return newcut.handle(self)

    # ---------------------------------------------------------- routes
    def _guarded(self, handle):
        """Run a request handler so that a bug in it becomes a 500 with the error in the log, never a connection that is
        simply dropped (which a page sees as a request that hangs or fails for no stated reason)."""
        try:
            handle()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception:
            sys.stderr.write('%s error handling %s %s\n%s\n' % (self.log_date_time_string(), self.command, self.path.split('?')[0], traceback.format_exc()))
            self.close_connection = True
            try:
                self._json({'ok': False, 'error': 'the server hit an error handling this request; see its log'}, 500)
            except Exception:
                pass

    def do_HEAD(self):
        self._guarded(self._get)

    def do_GET(self):
        self._guarded(self._get)

    def do_POST(self):
        self._guarded(self._post)

    def _get(self):
        if not self._local():
            return self._send(403, 'forbidden')
        app, u = self.app, urlsplit(self.path)
        if u.path.startswith('/api/timeline'):  # live-preview / timeline editing API lives in its own module
            from . import api_timeline
            return api_timeline.handle(self)
        if u.path.startswith('/api/new'):
            return self._newcut()
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        p = unquote(u.path)
        if p == '/feedback':
            f = os.path.join(app.feedback, safe(q.get('f')) + '.json')
            try:
                with open(f, 'rb') as fh:
                    return self._send(200, fh.read(), 'application/json')
            except OSError:
                return self._send(200, '{}', 'application/json')
        if p == '/versions':
            try:
                vs = sorted((n[:-4] for n in os.listdir(app.review) if VERSION_RE.match(n)), key=_natural)
            except OSError:
                vs = []
            return self._json(vs)
        if p == '/api/library':
            d = app.lib.snapshot()
            d['project'] = {'name': app.name, 'dir': app.project_dir}
            return self._json(d)
        if p == '/api/rules':
            # Reading never writes: rules.json appears only once the user changes a rule (or asks for their defaults).
            return self._json({'rules': RULES.load(app.project_dir), 'groups': RULES.GROUP_ORDER,
                               'saved': os.path.isfile(RULES.path(app.project_dir)), 'defaults': RULES.defaults_info()})
        if p == '/api/system':
            return self._json(system_info(app))
        if p == '/api/agent':
            return self._json(AGENTS.describe(app.project_dir))
        if p == '/api/run/estimate':
            return self._json(app.run.estimate(q.get('version') or ''))
        if p == '/api/publish/estimate':
            return self._json(app.run.estimate(timeline=q.get('timeline') or '?'))
        if p == '/api/run':
            d = app.run.status(q.get('version'))
            d['cut'] = app.run.cut_changed(q.get('version'))
            pp = SETTINGS.project_prompt(app.project_dir, 'run_prompt.md')  # the project's own instructions, if it has any
            d['prompt'] = pp and {'name': pp['name'], 'digest': pp['digest'], 'approved': pp['approved'], 'preview': pp['text'][:1500]}
            return self._json(d)
        if p == '/favicon.ico':
            return self._send(200, FAVICON, 'image/svg+xml', extra={'Cache-Control': 'max-age=86400'})
        if p == '/changes':
            return self._send(200, app.run.changes(q.get('f') or '') or '', 'text/plain; charset=utf-8')
        if p == '/api/used':
            return self._json(L.used_ranges(app.project_dir))
        if p == '/media':
            f = app.lib.clip_path(q.get('src'))
            return self._file(f) if f else self._send(404, 'not in the library')
        if p.startswith('/thumbs/'):
            n = p[len('/thumbs/'):]
            if not re.match(r'^[0-9a-f]{16}_[ps]\.jpg$', n):
                return self._send(404, 'not found')
            return self._file(os.path.join(app.lib.thumbs, n), cache='max-age=31536000, immutable')
        # static: the page first, then the project's review folder (renders + *_shots.json)
        rel = os.path.normpath('/' + ('index.html' if p == '/' else p)).lstrip('/')
        f = os.path.join(WEB, rel)
        if rel and '\0' not in rel and _inside(WEB, f) and os.path.isfile(f):
            return self._page(f) if f.endswith('.html') else self._file(f)
        f = os.path.join(app.review, rel)  # project files: only renders and their shot lists, never as a page or script
        if REVIEW_FILE.match(rel) and _inside(app.review, f) and _inside(app.project_dir, f) and os.path.isfile(f):
            return self._file(f)
        return self._send(404, 'not found')

    def _post(self):
        if not self._local():
            self.close_connection = True  # the body is not read: do not let it be parsed as the next request
            return self._send(403, 'forbidden')
        app, u = self.app, urlsplit(self.path)
        if u.path.startswith('/api/timeline'):  # live-preview / timeline editing API lives in its own module
            from . import api_timeline
            return api_timeline.handle(self)
        if u.path.startswith('/api/new'):
            return self._newcut()
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == '/api/library/upload':  # streamed to disk; the JSON body limit does not apply
            return self._upload(q)
        try:
            body = self._body()
        except ValueError:
            self.close_connection = True
            return self._send(413, 'too large')
        if u.path == '/save':
            name = safe(q.get('f'))
            try:
                json.loads(body.decode('utf-8'))
                if not name:
                    raise ValueError('no name')
            except ValueError:
                return self._send(400, 'bad')
            os.makedirs(app.feedback, exist_ok=True)
            dst = os.path.join(app.feedback, name + '.json')
            with app.save_lock:
                tmp = dst + '.tmp'
                with open(tmp, 'wb') as f:
                    f.write(body)
                os.replace(tmp, dst)
            return self._send(200, 'ok')
        if u.path == '/api/library/rescan':
            started = app.lib.rescan()
            return self._json({'ok': True, 'started': started, 'status': app.lib.snapshot()['status']})
        if u.path == '/api/rules':
            try:
                rules = json.loads(body.decode('utf-8') or '{}').get('rules')
                if not isinstance(rules, list):
                    raise ValueError('expected {"rules": [...]}')
            except (ValueError, AttributeError) as e:
                return self._json({'ok': False, 'error': str(e)}, 400)
            with app.save_lock:
                return self._json({'ok': True, 'rules': RULES.save(app.project_dir, rules)})
        if u.path == '/api/agent':
            try:
                d = json.loads(body.decode('utf-8') or '{}')
                if not isinstance(d, dict):
                    raise ValueError('expected an object')
                return self._json(dict(AGENTS.save(app.project_dir, d), ok=True))
            except (ValueError, OSError) as e:
                return self._json({'ok': False, 'error': str(e)}, 400)
        if u.path == '/api/agent/prompt':  # approve this project's own agent instructions, exactly as they were shown
            try:
                d = json.loads(body.decode('utf-8') or '{}')
                ok = SETTINGS.approve_prompt(app.project_dir, d.get('name'), d.get('digest'))
            except (ValueError, AttributeError, OSError, TypeError):
                ok = False
            return self._json({'ok': ok, 'error': '' if ok else 'that file has changed or is gone; look at it again'}, 200 if ok else 409)
        if u.path == '/api/agent/test':
            ok, err = app.run.start_test()
            return self._json({'ok': ok, 'error': err, 'status': app.run.status()}, 200 if ok else 409)
        if u.path == '/api/system/performance':
            try:
                d = json.loads(body.decode('utf-8') or '{}')
                return self._json({'ok': True, 'performance': SETTINGS.set_performance(d.get('mode'), d.get('jobs'))})
            except (ValueError, AttributeError, OSError) as e:
                return self._json({'ok': False, 'error': str(e)}, 400)
        if u.path == '/api/rules/use-defaults':
            if not RULES.defaults_info()['exists']:
                return self._json({'ok': False, 'error': 'you have not saved any defaults yet'}, 400)
            with app.save_lock:
                return self._json({'ok': True, 'rules': RULES.start_from_defaults(app.project_dir)})
        if u.path == '/api/rules/defaults':
            try:
                return self._json({'ok': True, 'path': RULES.save_defaults(app.project_dir)})
            except OSError as e:
                return self._json({'ok': False, 'error': str(e)}, 500)
        if u.path == '/api/library/pick':
            if not app.pick_lock.acquire(blocking=False):
                return self._json({'ok': False, 'error': 'The folder chooser is already open — look for its window.'}, 409)
            try:
                path, err = pick_folder()
            finally:
                app.pick_lock.release()
            if err:
                return self._json({'ok': False, 'error': err}, 500)
            if not path:
                return self._json({'ok': False, 'cancelled': True})
            try:
                folders = L.set_folders(app.project_dir, add=path)
            except ValueError as e:
                return self._json({'ok': False, 'error': str(e)}, 400)
            app.lib.rescan()
            return self._json({'ok': True, 'path': path, 'folders': folders})
        if u.path == '/api/publish':
            try:
                tid = json.loads(body.decode('utf-8') or '{}').get('timeline')
            except (ValueError, AttributeError):
                tid = None
            ok, err = app.run.start_publish(tid if isinstance(tid, str) else '')
            return self._json({'ok': ok, 'error': err, 'status': app.run.status()}, 200 if ok else 409)
        if u.path == '/api/run' or u.path == '/api/run/cancel':
            if u.path.endswith('/cancel'):
                return self._json({'ok': app.run.cancel(), 'status': app.run.status()})
            try:
                version = json.loads(body.decode('utf-8') or '{}').get('version')
            except (ValueError, AttributeError):
                version = None
            ok, err = app.run.start(version if isinstance(version, str) else '')
            return self._json({'ok': ok, 'error': err, 'status': app.run.status()}, 200 if ok else 409)
        if u.path == '/api/library/folders':
            try:
                d = json.loads(body.decode('utf-8') or '{}')
                add, remove = d.get('add'), d.get('remove')
                if not (isinstance(add, str) and add.strip()) and not (isinstance(remove, str) and remove.strip()):
                    raise ValueError('expected {"add": path} or {"remove": path}')
                folders = L.set_folders(app.project_dir, add=add if isinstance(add, str) else None,
                                        remove=remove if isinstance(remove, str) else None)
            except (ValueError, AttributeError) as e:
                return self._json({'ok': False, 'error': str(e)}, 400)
            app.lib.rescan()
            return self._json({'ok': True, 'folders': folders})
        return self._send(404, 'not found')


def _upload(self, q):
    """One dropped video file -> <project>/footage/inbox/[dir/]<name>, streamed in chunks, never overwriting."""
    app = self.app

    def refuse(code, msg):
        self.close_connection = True  # the body was not read
        return self._json({'ok': False, 'error': msg}, code)

    name = clean_name(q.get('name'))
    parts = [clean_name(x) for x in (q.get('dir') or '').replace('\\', '/').split('/')]
    parts = [x for x in parts if x and x not in ('.', '..')][:3]
    if not name or os.path.splitext(name)[1].lower() not in L.VIDEO_EXT:
        return refuse(415, 'not a video file: %s' % (q.get('name') or '?'))
    try:
        size = int(self.headers.get('Content-Length') or '')
    except ValueError:
        return refuse(411, 'Content-Length required')
    if size <= 0 or size > MAX_UPLOAD:
        return refuse(413, '%s is empty or larger than %d GB' % (name, MAX_UPLOAD // 1024 ** 3))
    folder = os.path.join(L.abs_path(app.project_dir, INBOX), *parts)
    os.makedirs(folder, exist_ok=True)
    if not _inside(app.project_dir, folder):  # footage/inbox is a link to somewhere else
        return refuse(400, 'the inbox folder leads outside the project; nothing was copied')
    if shutil.disk_usage(folder).free < size + 1024 ** 3:
        return refuse(507, 'not enough free disk space for %s' % name)
    dst = os.path.join(folder, name)
    stem, ext = os.path.splitext(name)
    n = 1
    while os.path.exists(dst):
        if os.path.getsize(dst) == size:  # same name and size: assume the same clip was dropped again
            left = size
            while left > 0:  # read and discard, so the browser sees a clean answer
                buf = self.rfile.read(min(CHUNK, left))
                if not buf:
                    break
                left -= len(buf)
            return self._json({'ok': True, 'skipped': True, 'src': L.rel_src(app.project_dir, dst)})
        n += 1
        dst = os.path.join(folder, '%s-%d%s' % (stem, n, ext))
    part = '%s.%d.part' % (dst, threading.get_ident())
    left = size
    try:
        with open(part, 'wb') as f:
            while left > 0:
                buf = self.rfile.read(min(CHUNK, left))
                if not buf:
                    break
                f.write(buf)
                left -= len(buf)
        if left:
            raise OSError('upload interrupted')
        os.link(part, dst)  # fails rather than overwrites if the name was taken meanwhile
    except OSError as e:
        self.close_connection = True
        return self._json({'ok': False, 'error': 'could not store %s: %s' % (name, e)}, 500)
    finally:
        try:
            os.remove(part)
        except OSError:
            pass
    if L.abs_path(app.project_dir, INBOX) not in [L.abs_path(app.project_dir, x) for x in L.library_folders(app.project_dir)]:
        L.set_folders(app.project_dir, add=INBOX)
    app.lib.rescan()
    return self._json({'ok': True, 'src': L.rel_src(app.project_dir, dst), 'size': size})


Handler._upload = _upload


def make_server(project_dir, port=8771):
    project_dir = os.path.abspath(project_dir)
    if not os.path.isdir(project_dir):
        raise SystemExit('not a folder: %s' % project_dir)
    app = App(project_dir, port)
    handler = type('CutvelvetHandler', (Handler,), {'app': app})
    srv = ThreadingHTTPServer(('127.0.0.1', port), handler)
    srv.daemon_threads = True
    srv.app = app
    return srv


def main(project_dir, port=8771, scan=True):
    srv = make_server(project_dir, port)
    if scan:
        srv.app.lib.rescan()
    print('cutvelvet: %s at http://127.0.0.1:%d' % (srv.app.project_dir, port), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == '__main__':
    ap = argparse.ArgumentParser(prog='python -m cutvelvet.server', description='cutvelvet review + library server')
    ap.add_argument('project_dir')
    ap.add_argument('--port', type=int, default=8771)
    ap.add_argument('--no-scan', action='store_true', help='do not index the library on start')
    a = ap.parse_args()
    main(a.project_dir, a.port, scan=not a.no_scan)
