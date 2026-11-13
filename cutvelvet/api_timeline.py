"""Timeline API for the live preview (web/live.html). Mounted by server.py for every path under /api/timeline.

    GET  /api/timeline                      {project, timelines: [{id, title, duration, shots, ...}], grades, margin, ...}
    GET  /api/timeline/<id>                 {timeline, rev, errors, warnings, history}
    POST /api/timeline/<id>                 {"base_rev": rev, "edits": [{"shot": id, "panel": index, "set": {...}}]}
                                            applies the edits to the file on disk, validates, keeps an undo copy, saves.
                                            Only src / ss / region / grade / expo / speed may be set. 409 when the file
                                            changed since base_rev, 400 with {"errors": [...]} when the result is invalid.
    POST /api/timeline/<id>/undo            restore the newest undo copy (the replaced file is kept as *.undone.json)
    GET  /api/timeline/<id>/history         [{name, time, rev, size}] newest first
    GET  /api/timeline/<id>/audio           the timeline's audio tracks mixed to one wav (Range), built once and cached
    POST /api/timeline/proxy                {"srcs": [...], "ensure": true} -> {"clips": {src: status}}; queues missing proxies
    GET  /api/timeline/proxy/status         every proxy job this server knows about, plus the queue length
    GET  /api/timeline/proxy/file?src=<src> the proxy mp4 (Range); src must be inside a registered library folder

Proxies: <project>/.cutvelvet/proxy/<key>.mp4 (+ <key>.json with the source's size / duration), key = sha1 of the
source's real path, mtime, size and the encode settings. H.264, PROXY_H lines high, a keyframe every PROXY_GOP frames
and no B-frames so the browser can seek to any frame at once; no audio; tagged sRGB so browser colour matches ffmpeg's. One ffmpeg at a time (two when the machine's Performance setting is Fast), single-threaded stages (about two cores per job in practice), nice 15.

Undo copies: <project>/<timelines>/.history/<id>.<utc stamp>.json, the newest HISTORY_KEEP are kept.
"""
import hashlib
import json
import os
import re
import subprocess
import threading
import time
from urllib.parse import parse_qs, quote, unquote, urlsplit

from . import engine, timeline as TL

PROXY_H = int(os.environ.get('CUTVELVET_PROXY_H', '720'))
PROXY_GOP = 6
PROXY_CRF = 23
PROXY_JOBS = 2                 # ceiling; the per-machine Performance setting decides (see proxy_jobs)
# ffmpeg decode / filter / encode threads per proxy job. Measured on this machine (x86 ffmpeg under Rosetta, 2.7K source):
# "-threads 2" on both sides still burns ~3.7 cores, because ffmpeg 8 runs demux, decode, filter and encode as separate
# pipelines; 1 / 1 / 1 holds a job to ~1.9 cores at about 3x real time.
PROXY_THREADS = 1
PROXY_SIG = 'v3 h%d g%d crf%d srgb' % (PROXY_H, PROXY_GOP, PROXY_CRF)
HISTORY_KEEP = 60
EDITABLE = ('src', 'ss', 'region', 'grade', 'expo', 'speed')
ID_RE = re.compile(r'^[A-Za-z0-9_][A-Za-z0-9_.-]*$')

def proxy_jobs():
    """Proxy encodes at once: Light / Balanced -> 1, Fast -> 2 (custom: 2 only when it allows at least Fast's jobs)."""
    try:
        from . import settings
        perf = settings.performance()
        fast = perf['mode'] == 'fast' or (perf['mode'] == 'custom' and perf['jobs'] >= perf['options']['fast'])
        return min(PROXY_JOBS, 2 if fast else 1)
    except Exception:  # noqa: BLE001 - no settings module / unreadable file: be gentle
        return 1


_stores = {}
_stores_lock = threading.Lock()
_save_lock = threading.Lock()
_audio_lock = threading.Lock()


# ───────────── proxies ─────────────
class Proxies:
    def __init__(self, app):
        self.app = app
        self.dir = os.path.join(app.project_dir, '.cutvelvet', 'proxy')
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)
        self.jobs = {}       # key -> {src, path, state, progress, error, ...}
        self.queue = []      # keys, in the order they should be made
        self.workers = 0
        self._prune()

    def _prune(self):
        """Drop proxies made with other encode settings, and half-written files left by a killed encode."""
        try:
            names = os.listdir(self.dir)
        except OSError:
            return
        for n in names:
            p = os.path.join(self.dir, n)
            try:
                if n.endswith('.tmp.mp4') or n.endswith('.json.tmp'):
                    os.remove(p)
                elif n.endswith('.json'):
                    with open(p) as f:
                        stale = json.load(f).get('settings') != PROXY_SIG
                    if stale:
                        os.remove(p)
                        os.remove(p[:-5] + '.mp4')
                elif n.endswith('.mp4') and not os.path.exists(p[:-4] + '.json'):
                    os.remove(p)
            except (OSError, ValueError):
                pass

    def key(self, real):
        st = os.stat(real)
        return hashlib.sha1(('%s|%d|%d|%s' % (real, st.st_mtime_ns, st.st_size, PROXY_SIG)).encode()).hexdigest()[:16]

    def _paths(self, key):
        return os.path.join(self.dir, key + '.mp4'), os.path.join(self.dir, key + '.json')

    def status(self, src, ensure=False, front=False):
        real = self.app.lib.clip_path(src)
        if not real:
            return {'state': 'missing', 'error': 'not a video inside a library folder'}
        try:
            key = self.key(real)
        except OSError as e:
            return {'state': 'missing', 'error': str(e)}
        mp4, meta = self._paths(key)
        with self.lock:
            job = self.jobs.get(key)
            if job is None or job['state'] == 'ready':
                if os.path.exists(mp4) and os.path.exists(meta):
                    try:
                        with open(meta) as f:
                            info = json.load(f)
                        info.update(state='ready', url='/api/timeline/proxy/file?src=%s&k=%s' % (quote(src, safe=''), key))
                        return info
                    except (OSError, ValueError):
                        pass
                job = None
            if job is None or (job['state'] == 'error' and ensure and time.time() - job.get('at', 0) > 30):
                if not ensure:
                    return {'state': 'none'}
                job = self.jobs[key] = {'src': src, 'real': real, 'key': key, 'state': 'queued', 'progress': 0.0, 'error': None}
                self.queue.append(key)
                self._spawn()
            if front and job['state'] == 'queued' and key in self.queue:
                self.queue.remove(key)
                self.queue.insert(0, key)
            self.cond.notify_all()
            return {'state': job['state'], 'progress': round(job['progress'], 3), 'error': job['error']}

    def _spawn(self):  # lock held
        while self.workers < proxy_jobs() and self.workers < len(self.queue):
            self.workers += 1
            threading.Thread(target=self._work, daemon=True, name='cutvelvet-proxy').start()

    def _work(self):
        while True:
            with self.lock:
                if not self.queue or self.workers > proxy_jobs():      # also steps down when the setting is lowered
                    self.workers -= 1
                    return
                job = self.jobs[self.queue.pop(0)]
                job['state'] = 'running'
            try:
                self._make(job)
                job['state'] = 'ready'
            except Exception as e:  # noqa: BLE001 - reported to the page
                job['error'] = str(e)[-400:]
                job['state'] = 'error'
                job['at'] = time.time()

    def _probe(self, real):
        o = subprocess.run([engine.FFPROBE, '-v', 'error', '-select_streams', 'v:0', '-show_entries',
                            'stream=width,height,r_frame_rate,avg_frame_rate:format=duration', '-of', 'json', real],
                           capture_output=True, text=True, timeout=60).stdout
        d = json.loads(o or '{}')
        st = (d.get('streams') or [{}])[0]

        def rate(s):
            try:
                a, b = (s or '0/1').split('/')
                return float(a) / float(b) if float(b) else 0.0
            except ValueError:
                return 0.0
        return {'sw': int(st['width']), 'sh': int(st['height']), 'duration': float(d.get('format', {}).get('duration') or 0),
                'fps': round(rate(st.get('avg_frame_rate')) or rate(st.get('r_frame_rate')), 3)}

    def _make(self, job):
        os.makedirs(self.dir, exist_ok=True)
        real = job['real']
        mp4, meta = self._paths(job['key'])
        info = self._probe(real)
        info['sw'], info['sh'] = engine.probe(real)          # exactly the size the renderer crops against
        tmp = mp4[:-4] + '.tmp.mp4'
        # setparams tags (does not convert) the frames as sRGB-transfer BT.709. A browser converts BT.709-transfer video
        # to the display's gamma, which lifts the mid-tones about 10%; tagged sRGB it shows the pixel values ffmpeg hands
        # the renderer, so the preview's grade starts from the same picture.
        vf = ("scale=-2:'trunc(min(%d,ih)/2)*2':flags=bilinear,"
              "setparams=color_primaries=bt709:color_trc=iec61966-2-1:colorspace=bt709:range=tv") % PROXY_H
        cmd = ['nice', '-n', '15', engine.FFMPEG, '-v', 'error', '-nostdin', '-y', '-threads', str(PROXY_THREADS), '-filter_threads', '1', '-i', real, '-map', '0:v:0', '-vf', vf, '-threads', str(PROXY_THREADS),
               '-c:v', 'libx264', '-preset', 'veryfast', '-crf', str(PROXY_CRF), '-g', str(PROXY_GOP), '-bf', '0',
               '-pix_fmt', 'yuv420p', '-an', '-movflags', '+faststart', '-progress', 'pipe:1', tmp]
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        job['pid'] = p.pid
        err = []
        threading.Thread(target=lambda: err.append(p.stderr.read()), daemon=True).start()
        for line in p.stdout:
            if line.startswith('out_time_us=') and info['duration'] > 0:
                try:
                    job['progress'] = max(0.0, min(1.0, int(line.split('=')[1]) / 1e6 / info['duration']))
                except ValueError:
                    pass
        if p.wait() != 0 or not os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise RuntimeError((''.join(err) or 'ffmpeg failed').strip())
        o = subprocess.run([engine.FFPROBE, '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=width,height',
                            '-of', 'csv=p=0', tmp], capture_output=True, text=True, timeout=60).stdout.strip().split(',')
        info.update(pw=int(o[0]), ph=int(o[1]), size=os.path.getsize(tmp), src=job['src'], settings=PROXY_SIG)
        os.replace(tmp, mp4)
        with open(meta + '.tmp', 'w') as f:
            json.dump(info, f)
        os.replace(meta + '.tmp', meta)
        job['progress'] = 1.0

    def overview(self):
        with self.lock:
            return {'queue': len(self.queue), 'workers': self.workers, 'settings': PROXY_SIG, 'dir': self.dir,
                    'jobs': [{k: j.get(k) for k in ('src', 'state', 'progress', 'error')} for j in self.jobs.values()]}


def _proxies(app):
    with _stores_lock:
        if app.project_dir not in _stores:
            _stores[app.project_dir] = Proxies(app)
        return _stores[app.project_dir]


# ───────────── timelines on disk ─────────────
def _rev(data):
    return hashlib.sha1(data).hexdigest()[:12]


def _read(proj, tid):
    path = TL.timeline_path(proj, tid)
    with open(path, 'rb') as f:
        data = f.read()
    return path, data, json.loads(data.decode('utf-8'))


def _hist_dir(proj):
    return os.path.join(TL.timelines_dir(proj), '.history')


def _history(proj, tid):
    d = _hist_dir(proj)
    out = []
    try:
        names = os.listdir(d)
    except OSError:
        return out
    for n in names:
        m = re.match(r'^' + re.escape(tid) + r'\.(\d{8}T\d{6}_\d{6})\.json$', n)
        if m:
            p = os.path.join(d, n)
            out.append({'name': n, 'stamp': m.group(1), 'time': os.path.getmtime(p), 'size': os.path.getsize(p)})
    out.sort(key=lambda h: h['stamp'], reverse=True)
    return out


def _stamp():
    t = time.time()
    return time.strftime('%Y%m%dT%H%M%S', time.gmtime(t)) + '_%06d' % int((t % 1) * 1e6)


def _write_atomic(path, data):
    tmp = path + '.tmp'
    with open(tmp, 'wb') as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _keep_copy(proj, tid, data, suffix='.json'):
    d = _hist_dir(proj)
    os.makedirs(d, exist_ok=True)
    name = '%s.%s%s' % (tid, _stamp(), suffix)
    _write_atomic(os.path.join(d, name), data)
    if suffix == '.json':
        for h in _history(proj, tid)[HISTORY_KEEP:]:
            try:
                os.remove(os.path.join(d, h['name']))
            except OSError:
                pass
    return name


def _payload(proj, tid):
    path, data, tl = _read(proj, tid)
    errs, warns = TL.validate(tl, proj)
    return {'timeline': tl, 'rev': _rev(data), 'mtime': os.path.getmtime(path), 'errors': errs, 'warnings': warns,
            'history': len(_history(proj, tid))}


def _apply(app, tl, edits):
    """Apply [{shot, panel, set}] in place. Returns a list of problems (empty when every edit applied)."""
    bad = []
    if not isinstance(edits, list) or not edits:
        return ['"edits" must be a non-empty list']
    shots = {s.get('id'): s for s in tl.get('shots') or [] if isinstance(s, dict)}
    for i, e in enumerate(edits):
        w = 'edit[%d]' % i
        if not isinstance(e, dict) or not isinstance(e.get('set'), dict):
            bad.append(w + ': expected {"shot", "panel", "set": {...}}')
            continue
        s = shots.get(e.get('shot'))
        panels = (s or {}).get('panels') or []
        k = e.get('panel')
        if s is None or not isinstance(k, int) or isinstance(k, bool) or not 0 <= k < len(panels):
            bad.append('%s: no panel %r in shot %r' % (w, k, e.get('shot')))
            continue
        for key, val in e['set'].items():
            if key not in EDITABLE:
                bad.append('%s: "%s" cannot be edited here (only %s)' % (w, key, ', '.join(EDITABLE)))
            elif key == 'src' and not app.lib.clip_path(val):
                bad.append('%s: src %r is not a clip in the library' % (w, val))
            else:
                panels[k][key] = val
    return bad


def _save(h, proj, tid, raw):
    try:
        body = json.loads(raw.decode('utf-8') or '{}')
    except ValueError:
        return h._json({'ok': False, 'error': 'bad request body'}, 400)
    if not isinstance(body, dict):
        return h._json({'ok': False, 'error': 'expected an object'}, 400)
    with _save_lock:
        try:
            path, data, tl = _read(proj, tid)
        except (OSError, ValueError) as e:
            return h._json({'ok': False, 'error': 'cannot read timeline: %s' % e}, 404)
        if body.get('base_rev') != _rev(data):
            return h._json({'ok': False, 'error': 'the timeline file changed on disk since it was opened', 'rev': _rev(data)}, 409)
        if tl.get('id') != tid:
            return h._json({'ok': False, 'error': 'file id %r does not match %r' % (tl.get('id'), tid)}, 400)
        errs = _apply(h.app, tl, body.get('edits'))
        if not errs:
            errs, _ = TL.validate(tl, proj)
        if errs:
            return h._json({'ok': False, 'error': 'not saved: the edited timeline is invalid', 'errors': errs}, 400)
        new = TL.dumps(tl).encode('utf-8')
        if new != data:
            _keep_copy(proj, tid, data)
            _write_atomic(path, new)
    out = _payload(proj, tid)
    out['ok'] = True
    return h._json(out)


def _undo(h, proj, tid):
    with _save_lock:
        hist = _history(proj, tid)
        if not hist:
            return h._json({'ok': False, 'error': 'nothing to undo'}, 409)
        src = os.path.join(_hist_dir(proj), hist[0]['name'])
        try:
            with open(src, 'rb') as f:
                old = f.read()
            errs, _ = TL.validate(json.loads(old.decode('utf-8')), proj)
        except (OSError, ValueError) as e:
            return h._json({'ok': False, 'error': 'undo copy unreadable: %s' % e}, 500)
        if errs:
            return h._json({'ok': False, 'error': 'undo copy is not a valid timeline', 'errors': errs}, 500)
        path = TL.timeline_path(proj, tid)
        try:
            with open(path, 'rb') as f:
                _keep_copy(proj, tid, f.read(), '.undone.json')
        except OSError:
            pass
        _write_atomic(path, old)
        os.remove(src)
    out = _payload(proj, tid)
    out['ok'] = True
    return h._json(out)


def _audio(h, proj, tid):
    try:
        tl = TL.load_timeline(proj, tid, strict=False)
        if not TL.audio_tracks(tl):
            return h._send(404, 'no audio')
        from . import cli
        with _audio_lock:
            path = cli.build_audio(proj, tl)
    except Exception as e:  # noqa: BLE001 - the page falls back to a silent clock
        return h._send(404, 'no audio: %s' % e)
    return h._file(path) if path else h._send(404, 'no audio')


def _index(h, proj):
    items = []
    for tid in TL.list_timelines(proj):
        it = {'id': tid}
        try:
            _, data, tl = _read(proj, tid)
            errs, _w = TL.validate(tl, proj)
            it.update(title=tl.get('title') or '', duration=tl.get('duration'), shots=len(tl.get('shots') or []),
                      rev=_rev(data), errors=errs)
        except (OSError, ValueError, AttributeError, TypeError) as e:   # e.g. a timeline file that is not an object
            it['errors'] = [str(e)]
        items.append(it)
    return h._json({'project': {'name': proj.get('name') or os.path.basename(proj['root']), 'canvas': proj['canvas'], 'fps': proj['fps']},
                    'timelines': items, 'grades': {k: [list(v[0]), list(v[1]), v[2], v[3]] for k, v in engine.GR.items()},
                    'margin': engine.MARGIN, 'transitions': list(TL.TRANSITIONS), 'editable': list(EDITABLE),
                    'bone': list(engine.BONE), 'proxy': PROXY_SIG})


# ───────────── router ─────────────
def handle(h):
    app, u = h.app, urlsplit(h.path)
    parts = [unquote(x) for x in u.path.split('/') if x][2:]      # after "api", "timeline"
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    post = h.command == 'POST'
    raw = b''
    if post:                                   # always drain the body, or it would be read as the next request on this connection
        try:
            raw = h._body()
        except ValueError:
            h.close_connection = True
            return h._json({'ok': False, 'error': 'body too large'}, 413)
    try:
        proj = TL.load_project(app.project_dir)
    except (TL.TimelineError, OSError, ValueError) as e:
        return h._json({'ok': False, 'error': str(e)}, 500)

    if not parts:
        return _index(h, proj) if not post else h._send(405, 'method not allowed')

    if parts[0] == 'proxy':
        px = _proxies(app)
        if post and len(parts) == 1:
            try:
                body = json.loads(raw.decode('utf-8') or '{}')
                srcs = [s for s in body.get('srcs') or [] if isinstance(s, str)][:500]
            except (ValueError, AttributeError):
                return h._json({'ok': False, 'error': 'bad request body'}, 400)
            ensure = bool(body.get('ensure'))
            first = body.get('first')
            clips = {s: px.status(s, ensure) for s in srcs}
            if isinstance(first, str) and first in clips:
                clips[first] = px.status(first, ensure, front=True)
            ov = px.overview()
            return h._json({'clips': clips, 'queue': ov['queue'], 'settings': PROXY_SIG})
        if not post and parts[1:] == ['status']:
            return h._json(px.overview())
        if not post and parts[1:] == ['file']:
            st = px.status(q.get('src'))
            if st.get('state') != 'ready':
                return h._send(404, 'no proxy yet')
            real = app.lib.clip_path(q.get('src'))
            return h._file(px._paths(px.key(real))[0])
        return h._send(404, 'not found')

    tid = parts[0]
    if not ID_RE.match(tid) or not os.path.isfile(TL.timeline_path(proj, tid)):
        return h._json({'ok': False, 'error': 'no timeline %r' % tid}, 404)
    rest = parts[1:]
    if not rest:
        if post:
            return _save(h, proj, tid, raw)
        try:
            return h._json(_payload(proj, tid))
        except (OSError, ValueError) as e:
            return h._json({'ok': False, 'error': str(e)}, 500)
    if rest == ['undo'] and post:
        return _undo(h, proj, tid)
    if rest == ['history'] and not post:
        return h._json(_history(proj, tid))
    if rest == ['audio'] and not post:
        return _audio(h, proj, tid)
    return h._send(404, 'not found')
