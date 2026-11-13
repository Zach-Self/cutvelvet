"""Timelines as data.

A project is a folder with a ``project.json``; each timeline is ``<project>/<timelines>/<id>.json``.

project.json
    {"name": "My Film", "canvas": [1920, 1080], "fps": 24, "library": ["footage"],
     "timelines": "timelines", "cache": ".cutvelvet/cache", "out": "renders"}

timelines/<id>.json
    {"id": "ch2", "title": "Chapter Two", "duration": 124.0,
     "audio": {"src": "music/song.wav", "start": 0, "fadeout": 6},
     "fx": {"fade": {"in": 1.2, "out": 1.2}},
     "shots": [{"id": "S1a", "label": "Phone goes face-down", "t0": 13.0, "t1": 19.6,
                "panels": [{"src": "footage/phone.mp4", "ss": 8.5, "t0": 13.0, "t1": 19.6,
                            "rect": [0, 138, 1180, 804], "region": [0.30, 0.60, 0.62],
                            "grade": "bw", "expo": 1.0, "speed": 0.4, ...}],
                "texts": [{"text": "...", "t0": 2.8, "t1": 12.5, "pos": [960, 569], "size": 150, ...}],
                "fx": {}}]}

Conventions
    * All times are absolute timeline seconds. ``src`` paths are project-relative. ``rect`` is
      [x, y, w, h] in canvas pixels; ``region`` is [cx, cy, width-fraction] of the source frame.
    * Panel / text dicts are the engine's dicts verbatim (tuples written as JSON arrays). Panel keys:
      src ss t0 t1 rect region grade expo speed zoom pan tin tout ghost blur blend op z vig q flipx.
      Text keys: text t0 t1 pos size font anchor fade fadeout track spread stagger rise color.
      Unknown keys are preserved and ignored by the engine.
    * Shot t0/t1 are stored as authored and snapped to the frame grid at render time
      (round(t * fps)). Shots must be in order and contiguous.

Schema extensions beyond the original contract (needed by the first film cut with the tool)
    1. Timeline-level ``fx`` (object, optional): effects that apply to every shot. A shot's own ``fx``
       is merged over it (same keys win) and is only evaluated while that shot's frames render.
       Effects are declarative because JSON cannot hold functions:
         "fade":  {"in": 1.2, "out": 1.2}  -> global brightness smooth(t/in) * smooth((duration-t)/out)
         "flash": {"t": 68.38, "amp": 0.9, "decay": 9, "dur": 0.6}  -> amp*exp(-(t-t0)*decay) for 0 <= t-t0 < dur
         "shred": {"t": 68.38, "amp": 70, "decay": 5, "dur": 0.9}   -> same pulse shape, drives row displacement
       "flash" / "shred" may also be a list of such pulses (summed).
    2. ``audio`` may be a single track object or a LIST of track objects (mixed, unnormalised).
       Track fields: ``src``; ``start`` = timeline second at which the track begins (default 0);
       ``ss`` = offset into the source file (default 0); ``dur`` = max seconds of source used (optional);
       ``fadein`` seconds (default 0); ``fadeout`` seconds (default 0); ``fadeout_at`` = timeline second
       the fade-out starts (default duration - fadeout); ``gain`` linear (default 1).
       Use ``audio_tracks(tl)`` to always get a list.
    3. Panels that are visible outside their owning shot. A panel lives in exactly one shot (the one
       containing its t0) for editing purposes, but the renderer composes every panel/text in the
       timeline whose [t0, t1) covers a frame, whichever shot the frame belongs to. This matters because
       cuts are frame-snapped while panel times are not (a panel ending at 19.6 still shows on the frame
       at 19.5833 that opens the next shot), and it lets a panel deliberately run across a cut.
"""
import hashlib
import json
import math
import os

from . import engine

DEFAULT_PROJECT = {'name': '', 'canvas': [1920, 1080], 'fps': 24, 'library': [], 'timelines': 'timelines',
                   'cache': '.cutvelvet/cache', 'out': 'renders'}
TRANSITIONS = ('lid', 'wipeL', 'wipeR', 'fade', 'cut')


class TimelineError(ValueError):
    pass


# ───────────── project ─────────────
def load_project(project_dir):
    """Returns project.json merged over defaults, plus 'root' (absolute project dir)."""
    root = os.path.abspath(project_dir)
    path = os.path.join(root, 'project.json')
    if not os.path.exists(path): raise TimelineError(f'no project.json in {root}')
    try:
        with open(path) as f: d = json.load(f)
    except ValueError as e: raise TimelineError(f'{path}: {e}')
    if not isinstance(d, dict): raise TimelineError(f'{path}: not a JSON object')
    from . import library                      # stdlib only; keeps folder keys inside the project (see library.confine)
    proj = dict(DEFAULT_PROJECT); proj.update(library.confine(root, d)); proj['root'] = root
    c, fps = proj['canvas'], proj['fps']
    if not (_nums(c, 2) and all(16 <= v <= 16384 for v in c)): raise TimelineError(f'{path}: "canvas" must be [width, height], each 16 to 16384')
    if not (_num(fps) and 1 <= fps <= 240): raise TimelineError(f'{path}: "fps" must be a number from 1 to 240')
    return proj

def _proj(project):
    return project if isinstance(project, dict) else load_project(project)

def timelines_dir(project):
    proj = _proj(project); return os.path.join(proj['root'], proj['timelines'])

def timeline_path(project, tid):
    return os.path.join(timelines_dir(project), tid + '.json')

def list_timelines(project):
    d = timelines_dir(project)
    return sorted(f[:-5] for f in os.listdir(d) if f.endswith('.json')) if os.path.isdir(d) else []

def resolve(project, src):
    """project-relative path -> absolute"""
    return os.path.join(_proj(project)['root'], src)


# ───────────── load / validate ─────────────
def _num(v): return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
def _nums(v, n): return isinstance(v, (list, tuple)) and len(v) == n and all(_num(x) for x in v)

def audio_tracks(tl):
    a = tl.get('audio')
    if not a: return []
    return [a] if isinstance(a, dict) else list(a)

def validate(tl, project=None):
    """Returns (errors, warnings) as lists of strings. Pass a project (dir or dict) to also check that sources exist."""
    E, Wn = [], []
    if not isinstance(tl, dict): return ['timeline is not an object'], []
    if not isinstance(tl.get('id'), str) or not tl.get('id'): E.append('missing "id"')
    dur = tl.get('duration')
    if not _num(dur) or dur <= 0: E.append('"duration" must be a positive number'); dur = None
    shots = tl.get('shots')
    if not isinstance(shots, list) or not shots: return E + ['"shots" must be a non-empty list'], Wn
    proj = _proj(project) if project is not None else None
    def src_ok(where, src):
        if not isinstance(src, str) or not src: E.append(f'{where}: missing "src"'); return
        if os.path.isabs(src): Wn.append(f'{where}: src should be project-relative: {src}')
        if proj and not os.path.exists(resolve(proj, src)): Wn.append(f'{where}: source not found: {src}')
    def fx_ok(where, fx):
        if fx is None: return
        if not isinstance(fx, dict): E.append(f'{where}: "fx" must be an object'); return
        for k, v in fx.items():
            if k == 'fade':
                if not (isinstance(v, dict) and all(_num(v.get(q, 0)) for q in ('in', 'out'))): E.append(f'{where}: fx.fade must be {{"in": s, "out": s}}')
            elif k in ('flash', 'shred'):
                for pulse in (v if isinstance(v, list) else [v]):
                    if not (isinstance(pulse, dict) and all(_num(pulse.get(q)) for q in ('t', 'amp', 'decay', 'dur'))): E.append(f'{where}: fx.{k} pulse needs numeric t, amp, decay, dur')
            else: E.append(f'{where}: unknown fx "{k}"')
    fx_ok('timeline', tl.get('fx'))
    for i, a in enumerate(audio_tracks(tl)):
        if not isinstance(a, dict): E.append(f'audio[{i}] is not an object'); continue
        src_ok(f'audio[{i}]', a.get('src'))
        for k in ('start', 'ss', 'dur', 'fadein', 'fadeout', 'fadeout_at', 'gain'):
            if k in a and not _num(a[k]): E.append(f'audio[{i}].{k} must be a number')
    seen = set(); prev = None
    for si, s in enumerate(shots):
        if not isinstance(s, dict): E.append(f'shots[{si}] is not an object'); continue
        sid = s.get('id'); w = f'shot {sid!r}'
        if not isinstance(sid, str) or not sid: E.append(f'shots[{si}]: missing "id"')
        elif sid in seen: E.append(f'{w}: duplicate id')
        seen.add(sid)
        if not (_num(s.get('t0')) and _num(s.get('t1'))): E.append(f'{w}: t0/t1 must be numbers'); continue
        if s['t1'] <= s['t0']: E.append(f'{w}: t1 <= t0')
        if prev is None and abs(s['t0']) > 1e-6: E.append(f'{w}: first shot must start at 0 (starts at {s["t0"]})')
        if prev is not None and abs(s['t0'] - prev['t1']) > 1e-6: E.append(f'{w}: t0 {s["t0"]} does not meet previous shot t1 {prev["t1"]} (shots must be ordered and contiguous)')
        prev = s
        fx_ok(w, s.get('fx'))
        for pi, p in enumerate(s.get('panels') or []):
            pw = f'{w} panel[{pi}]'
            if not isinstance(p, dict): E.append(f'{pw}: not an object'); continue
            src_ok(pw, p.get('src'))
            if not (_num(p.get('t0')) and _num(p.get('t1'))): E.append(f'{pw}: t0/t1 must be numbers'); continue
            if p['t1'] <= p['t0']: E.append(f'{pw}: t1 <= t0')
            if not (s['t0'] - 1e-6 <= p['t0'] < s['t1']): Wn.append(f'{pw}: t0 {p["t0"]} is outside its shot [{s["t0"]}, {s["t1"]})')
            if not _nums(p.get('rect'), 4) or p['rect'][2] <= 0 or p['rect'][3] <= 0: E.append(f'{pw}: rect must be [x, y, w, h] with w, h > 0')
            if not _nums(p.get('region'), 3) or p['region'][2] <= 0: E.append(f'{pw}: region must be [cx, cy, width-fraction]')
            if p.get('grade', 'bw') not in engine.GR: E.append(f'{pw}: unknown grade {p.get("grade")!r} (have {sorted(engine.GR)})')
            for k in ('ss', 'expo', 'speed', 'op', 'z', 'vig', 'q', 'blur'):
                if k in p and not _num(p[k]): E.append(f'{pw}: {k} must be a number')
            if 'speed' in p and _num(p['speed']) and p['speed'] <= 0: E.append(f'{pw}: speed must be > 0')
            if 'zoom' in p and not _nums(p['zoom'], 2): E.append(f'{pw}: zoom must be [z0, z1]')
            if 'pan' in p and not (isinstance(p['pan'], (list, tuple)) and len(p['pan']) == 2 and all(_nums(q, 2) for q in p['pan'])): E.append(f'{pw}: pan must be [[x0, y0], [x1, y1]]')
            for k in ('tin', 'tout'):
                if k in p and not (isinstance(p[k], (list, tuple)) and len(p[k]) == 2 and p[k][0] in TRANSITIONS and _num(p[k][1])): E.append(f'{pw}: {k} must be [kind, seconds], kind in {TRANSITIONS}')
            if p.get('blend') not in (None, 'screen'): E.append(f'{pw}: blend must be "screen" or absent')
        for xi, x in enumerate(s.get('texts') or []):
            xw = f'{w} text[{xi}]'
            if not isinstance(x, dict): E.append(f'{xw}: not an object'); continue
            if not isinstance(x.get('text'), str): E.append(f'{xw}: missing "text"')
            if not (_num(x.get('t0')) and _num(x.get('t1'))): E.append(f'{xw}: t0/t1 must be numbers')
            if not _nums(x.get('pos'), 2): E.append(f'{xw}: pos must be [x, y]')
            if not _num(x.get('size')): E.append(f'{xw}: size must be a number')
            if x.get('anchor', 'l') not in ('l', 'c', 'r'): E.append(f'{xw}: anchor must be l, c or r')
            if 'color' in x and not _nums(x['color'], 3): E.append(f'{xw}: color must be [r, g, b]')
            if 'fade' in x and not (_num(x['fade']) and x['fade'] > 0): E.append(f'{xw}: fade must be > 0')
    if dur is not None and prev is not None and _num(prev.get('t1')) and abs(prev['t1'] - dur) > 1e-6:
        E.append(f'last shot ends at {prev["t1"]} but duration is {dur}')
    return E, Wn

def load_timeline(project, tid, strict=True):
    """Load <project>/<timelines>/<tid>.json (or a direct path to a .json). Raises TimelineError when invalid and strict."""
    proj = _proj(project)
    path = tid if tid.endswith('.json') and os.path.exists(tid) else timeline_path(proj, tid)
    if not os.path.exists(path): raise TimelineError(f'no timeline {tid!r} (looked for {path}); have: {", ".join(list_timelines(proj)) or "none"}')
    try: tl = json.load(open(path))
    except json.JSONDecodeError as e: raise TimelineError(f'{path}: {e}')
    if strict:
        errs, _ = validate(tl, proj)
        if errs: raise TimelineError(f'{path}:\n  ' + '\n  '.join(errs))
    return tl

def save_timeline(project, tl):
    path = timeline_path(project, tl['id']); os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w') as f: f.write(dumps(tl))
    os.replace(tmp, path); return path

def dumps(tl):
    """Readable JSON: one panel / text per line."""
    def one(o): return json.dumps(o, ensure_ascii=False)
    head = {k: v for k, v in tl.items() if k != 'shots'}
    out = ['{'] + [f' {one(k)}: {one(v)},' for k, v in head.items()] + [' "shots": [']
    shots = tl.get('shots', [])
    for si, s in enumerate(shots):
        meta = {k: v for k, v in s.items() if k not in ('panels', 'texts', 'fx')}
        out.append('  {' + one(meta)[1:-1] + ',')
        for key in ('panels', 'texts'):
            items = s.get(key) or []
            if items: out += [f'   "{key}": ['] + ['    ' + one(it) + (',' if j < len(items) - 1 else '') for j, it in enumerate(items)] + ['   ],']
            else: out.append(f'   "{key}": [],')
        out.append(f'   "fx": {one(s.get("fx") or {})}')
        out.append('  }' + (',' if si < len(shots) - 1 else ''))
    return '\n'.join(out + [' ]', '}']) + '\n'


# ───────────── render helpers ─────────────
def flatten(tl):
    """All panels and all texts of the timeline, in shot order (this is the engine's draw order within a z level)."""
    P = [p for s in tl['shots'] for p in (s.get('panels') or [])]
    X = [x for s in tl['shots'] for x in (s.get('texts') or [])]
    return P, X

def overlapping(tl, t0, t1):
    P, X = flatten(tl)
    return [p for p in P if p['t0'] < t1 and p['t1'] > t0], [x for x in X if x['t0'] < t1 and x['t1'] > t0]

def on_frames(item, n0, n1, fps):
    """Is a panel or text drawn on any frame n0 <= n < n1? The renderer draws it on frame n when t0 <= n / fps < t1,
    so this is decided on the frame grid: a panel that starts at 21.87 is NOT on the frame at 21.8333."""
    a = max(n0, int(math.ceil(item['t0'] * fps - 1e-9)) - 1)
    while a < n1 and not (item['t0'] <= a / fps): a += 1          # first frame at or after t0, by the renderer's own comparison
    return a < n1 and a / fps < item['t1']

def visible(tl, n0, n1, fps):
    """The panels and texts that appear on frames n0..n1-1 -- exactly what those frames' pixels depend on."""
    P, X = flatten(tl)
    return [p for p in P if on_frames(p, n0, n1, fps)], [x for x in X if on_frames(x, n0, n1, fps)]

def shot_frames(shot, fps):
    """(first frame, end frame) of a shot on the frame grid."""
    return int(round(shot['t0'] * fps)), int(round(shot['t1'] * fps))

def shot_at(tl, t, fps):
    """The shot whose frame range contains time t (falls back to the last shot)."""
    n = int(round(t * fps))
    for s in tl['shots']:
        a, b = shot_frames(s, fps)
        if a <= n < b: return s
    return tl['shots'][-1]

def get_shot(tl, sid):
    for s in tl['shots']:
        if s['id'] == sid: return s
    raise TimelineError(f'no shot {sid!r} in {tl.get("id")}; have: {", ".join(s["id"] for s in tl["shots"])}')

def fx_spec(tl, shot=None):
    spec = dict(tl.get('fx') or {})
    if shot: spec.update(shot.get('fx') or {})
    return spec

def _pulse(v):
    pulses = v if isinstance(v, list) else [v]
    def f(t):
        tot = 0
        for q in pulses:
            d = t - q['t']
            if 0 <= d < q['dur']: tot += q['amp'] * math.exp(-d * q['decay'])
        return tot
    return f

def build_fx(tl, shot=None):
    """Declarative fx spec -> the dict of callables t -> value that engine.render wants."""
    spec = fx_spec(tl, shot); F = {}; dur = tl['duration']
    if 'fade' in spec:
        fi, fo = spec['fade'].get('in', 0), spec['fade'].get('out', 0)
        F['fade'] = lambda t: (engine.smooth(t / fi) if fi > 0 else 1.0) * (engine.smooth((dur - t) / fo) if fo > 0 else 1.0)
    for k in ('flash', 'shred'):
        if k in spec: F[k] = _pulse(spec[k])
    return F

_engine_sig = []
def engine_signature():
    if not _engine_sig: _engine_sig.append(hashlib.md5(open(engine.__file__.replace('.pyc', '.py'), 'rb').read()).hexdigest()[:8])
    return _engine_sig[0]

def shot_key(tl, shot, proj, scale):
    """Cache key: everything that can change the pixels of this shot."""
    fps = proj['fps']; n0, n1 = shot_frames(shot, fps)
    P, X = visible(tl, n0, n1, fps)   # frame-exact: a neighbour's off-grid panel that never reaches these frames is not part of the key
    blob = json.dumps([P, X, fx_spec(tl, shot), tl['duration'], float(scale), n0, n1, list(proj['canvas']), fps, engine_signature()], sort_keys=True)
    return hashlib.md5(blob.encode()).hexdigest()[:10]


# ───────────── footage bookkeeping ─────────────
def panel_range(p):
    """Source seconds a panel plays: [ss, ss + (t1 - t0) * speed]."""
    a = p.get('ss', 0); return a, a + (p['t1'] - p['t0']) * p.get('speed', 1)

def used_ranges(project_dir):
    """{src: [{"timeline", "shot", "a", "b"}]} over every timeline in the project (a, b = source seconds)."""
    proj = _proj(project_dir); used = {}
    for tid in list_timelines(proj):
        try: tl = load_timeline(proj, tid, strict=False)
        except TimelineError: continue
        for s in tl.get('shots') or []:
            for p in s.get('panels') or []:
                try: a, b = panel_range(p)
                except (KeyError, TypeError): continue
                used.setdefault(p.get('src'), []).append({'timeline': tl.get('id', tid), 'shot': s.get('id'), 'a': a, 'b': b})
    return used

def audit(project_dir, timeline=None):
    """Repeated footage: the same src with overlapping source ranges in two different shots.

    With a timeline id: repeats within that timeline. Without: every timeline, plus repeats across timelines.
    Returns a list of (a, b) pairs of {"timeline", "shot", "src", "a", "b"}; empty means no repeated cuts.
    """
    used = used_ranges(project_dir); bad = []
    for src, uses in used.items():
        if timeline: uses = [u for u in uses if u['timeline'] == timeline]
        for i, a in enumerate(uses):
            for b in uses[i + 1:]:
                if (a['timeline'], a['shot']) != (b['timeline'], b['shot']) and a['a'] < b['b'] and b['a'] < a['b']:
                    bad.append((dict(a, src=src), dict(b, src=src)))
    return bad
