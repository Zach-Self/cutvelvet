"""Asset library: scan folders of video clips, probe them, cache posters + scrub sprites.

Stdlib only. Everything generated lives under ``<project>/.cutvelvet/``:

    .cutvelvet/library.json            the index (see ``Library.snapshot``)
    .cutvelvet/thumbs/<key>_p.jpg      poster (480 px wide, frame at the middle of the clip)
    .cutvelvet/thumbs/<key>_s.jpg      scrub sprite (SPRITE_FRAMES frames of SPRITE_W px, tiled in one row)

Thumbnail jobs run at low priority, 1-3 at a time depending on the machine's performance setting.

``key`` is a hash of absolute path + mtime + size, so rescans only touch new or changed files.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import settings as SETTINGS

VIDEO_EXT = {'.mp4', '.mov', '.m4v', '.webm', '.mkv', '.avi', '.mxf'}
SPRITE_FRAMES = 10
SPRITE_W = 160
POSTER_W = 480
MAX_DEPTH = 4         # how deep to walk below a library folder
MAX_FILES = 5000      # per folder, a guard against someone adding "/"
INDEX_VERSION = 1
DEFAULT_LIBRARY = ['footage/px']


def _bin(name):
    """ffmpeg/ffprobe: $CUTVELVET_FFMPEG / $CUTVELVET_FFPROBE if set, else whatever is on PATH."""
    return os.environ.get('CUTVELVET_' + name.upper()) or shutil.which(name) or name


# ---------------------------------------------------------------- project.json

def project_path(project_dir):
    return os.path.join(project_dir, 'project.json')


def inside(root, path):
    """Is `path` (after following symlinks) strictly inside `root`? The one containment test used everywhere."""
    root, path = os.path.realpath(root), os.path.realpath(path)
    return path.startswith(root.rstrip(os.sep) + os.sep)


# project.json keys that name a folder cutvelvet reads from, writes to or serves. A project can come from anyone,
# so these may only point inside the project; anything else is ignored and the built-in default is used.
PATH_KEYS = ('timelines', 'review', 'feedback', 'music', 'cache', 'out')
_warned = set()


def confine(project_dir, proj):
    """Drop PATH_KEYS entries of a project dict that are not plain folders inside the project. Returns the dict."""
    for k in PATH_KEYS:
        v = proj.get(k)
        if v is None:
            continue
        if not (isinstance(v, str) and v.strip() and '\0' not in v and inside(project_dir, abs_path(project_dir, v))):
            if (project_dir, k, repr(v)) not in _warned:  # say it once, not on every request
                _warned.add((project_dir, k, repr(v)))
                sys.stderr.write('cutvelvet: ignoring "%s": %r in project.json (it must be a folder inside the project)\n' % (k, v))
            del proj[k]
    return proj


def load_project(project_dir):
    try:
        with open(project_path(project_dir)) as f:
            d = json.load(f)
        return confine(project_dir, d) if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json(path, data, indent=1):
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    tmp = '%s.%d.%d.tmp' % (path, os.getpid(), threading.get_ident())
    with open(tmp, 'w') as f:
        json.dump(data, f, indent=indent)
        f.write('\n')
    os.replace(tmp, path)


def listed_folders(project_dir):
    """Every library folder project.json lists, allowed on this machine or not."""
    lib = load_project(project_dir).get('library')
    if isinstance(lib, str):
        lib = [lib]
    if not isinstance(lib, list):
        lib = list(DEFAULT_LIBRARY)
    return [x for x in lib if isinstance(x, str) and x.strip() and '\0' not in x][:200]


def folder_ok(project_dir, folder):
    """A listed folder is used when it lies inside the project, or the user added it on this machine (the page's
    Add folder). A folder outside the project that merely arrived in someone else's project.json is not scanned
    or served until then."""
    a = abs_path(project_dir, folder)
    return inside(project_dir, a) or os.path.realpath(a) == os.path.realpath(project_dir) or SETTINGS.folder_allowed(project_dir, a)


def library_folders(project_dir):
    """The library folders in use: the listed ones that pass ``folder_ok``."""
    return [x for x in listed_folders(project_dir) if folder_ok(project_dir, x)]


def abs_path(project_dir, p):
    p = os.path.expanduser(p)
    return os.path.normpath(p if os.path.isabs(p) else os.path.join(project_dir, p))


def rel_src(project_dir, p):
    """Canonical ``src``: project-relative with forward slashes when inside the project, else absolute."""
    a = abs_path(project_dir, p)
    root = os.path.normpath(project_dir)
    if a == root or a.startswith(root + os.sep):
        return os.path.relpath(a, root).replace(os.sep, '/')
    return a


def set_folders(project_dir, add=None, remove=None):
    """Add/remove a library folder in project.json (created if absent, other keys preserved)."""
    proj = load_project(project_dir)
    lib = listed_folders(project_dir)
    if add:
        a = abs_path(project_dir, add.strip())
        if '\0' in a or not os.path.isdir(a):
            raise ValueError('not a folder: %s' % a)
        if os.path.realpath(a) in ('/', os.path.realpath(os.path.expanduser('~'))):
            raise ValueError('pick a footage folder, not %s' % a)
        if not inside(project_dir, a):
            SETTINGS.allow_folder(project_dir, a)  # the user chose it here: remember that on this machine
        if a not in [abs_path(project_dir, x) for x in lib]:
            lib.append(rel_src(project_dir, a))
    if remove:
        r = abs_path(project_dir, remove.strip())
        lib = [x for x in lib if abs_path(project_dir, x) != r]
    proj['library'] = lib
    write_json(project_path(project_dir), proj, indent=1)
    return lib


# ---------------------------------------------------------------- used ranges

def _fallback_used_ranges(project_dir):
    proj = load_project(project_dir)
    tdir = abs_path(project_dir, proj.get('timelines') or 'timelines')
    out = {}
    try:
        names = sorted(n for n in os.listdir(tdir) if n.endswith('.json'))
    except OSError:
        return out
    for n in names:
        try:
            with open(os.path.join(tdir, n)) as f:
                tl = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(tl, dict):
            continue
        tid = tl.get('id') or n[:-5]
        for shot in tl.get('shots') or []:
            for p in (shot.get('panels') or []) if isinstance(shot, dict) else []:
                try:
                    src = p['src']
                    a = float(p.get('ss') or 0)
                    b = a + (float(p['t1']) - float(p['t0'])) * float(p.get('speed') or 1)
                except (KeyError, TypeError, ValueError):
                    continue
                if not isinstance(src, str):
                    continue
                out.setdefault(src, []).append({'timeline': tid, 'shot': shot.get('id'), 'a': a, 'b': b})
    return out


def used_ranges(project_dir):
    """``{src: [{timeline, shot, a, b}]}`` -- footage seconds each timeline panel reads.

    Uses ``cutvelvet.timeline.used_ranges`` when available, else computes the same from ``timelines/*.json``
    (range = ``[ss, ss + (t1 - t0) * speed]``). Keys are normalised with ``rel_src``.
    """
    raw = None
    try:
        from . import timeline as _tl
        raw = _tl.used_ranges(project_dir)
        if not isinstance(raw, dict):
            raw = None
    except Exception:
        raw = None
    if raw is None:
        raw = _fallback_used_ranges(project_dir)
    out = {}
    for src, ranges in raw.items():
        key = rel_src(project_dir, src)
        for r in ranges or []:
            try:
                a, b = float(r['a']), float(r['b'])
            except (KeyError, TypeError, ValueError):
                continue
            out.setdefault(key, []).append({'timeline': r.get('timeline'), 'shot': r.get('shot'),
                                            'a': round(min(a, b), 4), 'b': round(max(a, b), 4)})
    for v in out.values():
        v.sort(key=lambda r: (r['a'], r['b']))
    return out


# ---------------------------------------------------------------- probing + thumbnails

def _rate(s):
    try:
        n, d = (s or '0/1').split('/')
        return float(n) / float(d) if float(d) else 0.0
    except (ValueError, ZeroDivisionError):
        return 0.0


def probe(path):
    """duration / width / height / fps / codec via ffprobe; ``None`` if it is not a readable video."""
    try:
        o = subprocess.run([_bin('ffprobe'), '-v', 'error', '-select_streams', 'v:0', '-show_entries',
                            'stream=width,height,r_frame_rate,avg_frame_rate,duration,codec_name:'
                            'stream_side_data=rotation:stream_tags=rotate:format=duration', '-of', 'json', path],
                           capture_output=True, text=True, timeout=60)
        d = json.loads(o.stdout or '{}')
        st = (d.get('streams') or [None])[0]
        if not st:
            return None
        w, h = int(st['width']), int(st['height'])
        rot = 0
        for sd in st.get('side_data_list') or []:
            if 'rotation' in sd:
                rot = int(float(sd['rotation']))
        rot = rot or int(float((st.get('tags') or {}).get('rotate') or 0))
        if abs(rot) % 180 == 90:
            w, h = h, w
        dur = float(st.get('duration') or 0) or float((d.get('format') or {}).get('duration') or 0)
        fps = _rate(st.get('avg_frame_rate')) or _rate(st.get('r_frame_rate'))
        return {'duration': round(dur, 3), 'width': w, 'height': h, 'fps': round(fps, 3), 'codec': st.get('codec_name')}
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        return None


def sprite_times(duration, n=SPRITE_FRAMES):
    return [round(duration * (i + 0.5) / n, 3) for i in range(n)]


def make_thumbs(path, info, poster, sprite):
    """One ffmpeg process: N seeks into the clip -> a 1-row sprite, plus a poster from the middle."""
    dur = max(info['duration'], 0.0)
    fh = max(2, int(round(SPRITE_W * info['height'] / float(info['width']) / 2)) * 2)
    times = sprite_times(dur)
    cmd = [_bin('ffmpeg'), '-v', 'error', '-y', '-nostdin']
    for t in times + [round(dur / 2, 3)]:
        cmd += ['-threads', '2', '-ss', '%.3f' % max(0.0, min(t, max(0.0, dur - 0.1))), '-i', path]
    n = len(times)
    fc = ''.join('[%d:v]scale=%d:%d,setsar=1[a%d];' % (i, SPRITE_W, fh, i) for i in range(n))
    fc += ''.join('[a%d]' % i for i in range(n)) + 'hstack=inputs=%d[s];[%d:v]scale=%d:-2[p]' % (n, n, POSTER_W)
    ptmp, stmp = poster[:-4] + '.tmp.jpg', sprite[:-4] + '.tmp.jpg'
    cmd += ['-filter_complex', fc, '-an',
            '-map', '[s]', '-frames:v', '1', '-q:v', '5', '-update', '1', stmp,
            '-map', '[p]', '-frames:v', '1', '-q:v', '4', '-update', '1', ptmp]
    if shutil.which('nice'):
        cmd = ['nice', '-n', '15'] + cmd
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        ok = r.returncode == 0 and os.path.exists(ptmp) and os.path.exists(stmp)
        err = (r.stderr or '').strip()[-300:]
    except (OSError, subprocess.SubprocessError) as e:
        ok, err = False, str(e)
    if ok:
        os.replace(ptmp, poster)
        os.replace(stmp, sprite)
        return fh, None
    for p in (ptmp, stmp):
        try:
            os.remove(p)
        except OSError:
            pass
    return fh, err or 'ffmpeg failed'


# ---------------------------------------------------------------- sidecar metadata

def _sidecar_meta(folder):
    """Metadata dicts keyed by clip id (file stem), from JSON files in the folder or its parent.

    Recognises any JSON object whose values are objects, e.g. ``footage/pexels.json``:
    ``{"8078692": {"link", "w", "h", "dur", "q", "url"}}``. Only entries matching a clip are kept by the caller.
    """
    meta = {}
    for d in (os.path.dirname(folder), folder):
        try:
            names = sorted(n for n in os.listdir(d) if n.endswith('.json'))
        except OSError:
            continue
        for n in names:
            p = os.path.join(d, n)
            try:
                if os.path.getsize(p) > 20 * 1024 * 1024:
                    continue
                with open(p) as f:
                    data = json.load(f)
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            for k, v in data.items():
                if isinstance(v, dict):
                    meta.setdefault(str(k), {}).update(
                        {a: b for a, b in v.items() if isinstance(b, (str, int, float, bool))})
                elif isinstance(v, str) and v.strip():  # e.g. {"2186": "water-falling-over-stones"}
                    meta.setdefault(str(k), {}).setdefault('title', v)
    return meta


# ---------------------------------------------------------------- the index

def _walk(folder):
    out = []
    base = folder.rstrip(os.sep).count(os.sep)
    for root, dirs, files in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if not d.startswith('.'))
        if root.count(os.sep) - base >= MAX_DEPTH:
            dirs[:] = []
        for n in sorted(files):
            if not n.startswith('.') and not n.endswith('.part') and os.path.splitext(n)[1].lower() in VIDEO_EXT:
                p = os.path.join(root, n)
                if os.path.islink(p) and not inside(folder, p):  # a link that leaves the folder is never read or served
                    continue
                out.append(p)
                if len(out) >= MAX_FILES:
                    return out
    return out


class Library:
    def __init__(self, project_dir):
        self.project_dir = os.path.abspath(project_dir)
        self.dir = os.path.join(self.project_dir, '.cutvelvet')
        self.thumbs = os.path.join(self.dir, 'thumbs')
        self.index_path = os.path.join(self.dir, 'library.json')
        self.lock = threading.Lock()
        self.again = False
        self.status = {'scanning': False, 'done': 0, 'total': 0, 'error': None}
        self.index = {'version': INDEX_VERSION, 'folders': [], 'clips': [], 'scanned_at': None}
        try:
            with open(self.index_path) as f:
                d = json.load(f)
            if isinstance(d, dict) and d.get('version') == INDEX_VERSION and isinstance(d.get('clips'), list):
                self.index = d
        except (OSError, ValueError):
            pass

    # -- read side
    def snapshot(self):
        with self.lock:
            d = dict(self.index)
            d['clips'] = list(self.index['clips'])
            d['status'] = dict(self.status)
        d['sprite'] = {'frames': SPRITE_FRAMES, 'w': SPRITE_W}
        return d

    def clip_path(self, src):
        """Absolute path for a ``src``, only if it is a video file inside a registered library folder."""
        if not isinstance(src, str) or not src or '\0' in src:
            return None
        a = abs_path(self.project_dir, src)
        if os.path.splitext(a)[1].lower() not in VIDEO_EXT or not os.path.isfile(a):
            return None
        real = os.path.realpath(a)
        for folder in library_folders(self.project_dir):
            root = os.path.realpath(abs_path(self.project_dir, folder))
            if real.startswith(root.rstrip(os.sep) + os.sep):
                return real
        return None

    # -- scan
    def rescan(self, wait=False):
        with self.lock:
            if self.status['scanning']:
                self.again = True  # something changed mid-scan: go round once more when it finishes
                return False
            self.status = {'scanning': True, 'done': 0, 'total': 0, 'error': None}
        t = threading.Thread(target=self._scan, name='cutvelvet-scan', daemon=True)
        t.start()
        if wait:
            t.join()
        return True

    def _scan(self):
        try:
            self._scan_inner()
        except Exception as e:  # keep the server alive; surface the error in /api/library
            with self.lock:
                self.status['error'] = '%s: %s' % (type(e).__name__, e)
        finally:
            with self.lock:
                self.status['scanning'] = False
                again, self.again = self.again, False
            if again:
                self.rescan()

    def _scan_inner(self):
        os.makedirs(self.thumbs, exist_ok=True)
        with self.lock:
            old = {c.get('key'): c for c in self.index['clips']}
        folders, clips, todo, seen_ids = [], [], [], {}
        for folder in listed_folders(self.project_dir):
            fabs = abs_path(self.project_dir, folder)
            if not folder_ok(self.project_dir, folder):  # outside the project and not added on this machine: not read
                folders.append({'path': folder, 'abs': fabs, 'exists': os.path.isdir(fabs), 'count': 0, 'blocked': True})
                continue
            files = _walk(fabs) if os.path.isdir(fabs) else []
            folders.append({'path': folder, 'abs': fabs, 'exists': os.path.isdir(fabs), 'count': len(files)})
            meta = _sidecar_meta(fabs) if files else {}
            for p in files:
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                key = hashlib.sha1(('%s|%d|%d' % (p, st.st_mtime_ns, st.st_size)).encode()).hexdigest()[:16]
                stem = os.path.splitext(os.path.basename(p))[0]
                cid = stem
                if cid in seen_ids:  # same file name in two folders
                    cid = '%s~%s' % (stem, key[:6])
                seen_ids[cid] = 1
                c = dict(old.get(key) or {})
                c.update({'id': cid, 'name': os.path.basename(p), 'src': rel_src(self.project_dir, p), 'folder': folder,
                          'size': st.st_size, 'mtime': int(st.st_mtime), 'key': key, 'meta': meta.get(stem) or {}})
                clips.append(c)
                poster, sprite = self._thumb_paths(key)
                if 'duration' not in c or not (c.get('error') or (os.path.exists(poster) and os.path.exists(sprite))):
                    todo.append((c, p))
        with self.lock:
            self.index = {'version': INDEX_VERSION, 'folders': folders, 'clips': clips, 'scanned_at': self.index.get('scanned_at')}
            self.status.update(done=0, total=len(todo))
        if todo:
            with ThreadPoolExecutor(max_workers=SETTINGS.performance()['thumb_jobs']) as ex:  # 1-3, from the performance setting
                for i, _ in enumerate(ex.map(lambda cp: self._build(*cp), todo)):
                    with self.lock:
                        self.status['done'] = i + 1
                    if (i + 1) % 10 == 0:
                        self._write()
        self._prune({c['key'] for c in clips})
        with self.lock:
            self.index['scanned_at'] = time.strftime('%Y-%m-%dT%H:%M:%S')
        self._write()

    def _thumb_paths(self, key):
        return os.path.join(self.thumbs, key + '_p.jpg'), os.path.join(self.thumbs, key + '_s.jpg')

    def _build(self, c, path):
        info = probe(path)
        upd = {}
        if not info or not info['width'] or not info['height']:
            upd = {'error': 'unreadable', 'duration': 0, 'width': 0, 'height': 0, 'fps': 0}
        else:
            poster, sprite = self._thumb_paths(c['key'])
            fh, err = make_thumbs(path, info, poster, sprite)
            upd = dict(info)
            upd['error'] = err
            if not err:
                upd['poster'] = '/thumbs/' + os.path.basename(poster)
                upd['sprite'] = {'url': '/thumbs/' + os.path.basename(sprite), 'frames': SPRITE_FRAMES,
                                 'w': SPRITE_W, 'h': fh, 'times': sprite_times(info['duration'])}
        with self.lock:
            c.update(upd)
            if not c.get('error'):
                c.pop('error', None)

    def _prune(self, keys):
        try:
            for n in os.listdir(self.thumbs):
                if n.endswith('.jpg') and n.split('_')[0] not in keys:
                    os.remove(os.path.join(self.thumbs, n))
        except OSError:
            pass

    def _write(self):
        with self.lock:
            data = json.loads(json.dumps(self.index))
        write_json(self.index_path, data)


if __name__ == '__main__':
    import sys
    lib = Library(sys.argv[1] if len(sys.argv) > 1 else '.')
    lib.rescan(wait=True)
    s = lib.snapshot()
    print('%d clips, %d with errors -> %s' % (len(s['clips']), sum(1 for c in s['clips'] if c.get('error')), lib.index_path))
