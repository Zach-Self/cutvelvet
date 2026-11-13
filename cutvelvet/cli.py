"""cutvelvet command line.

    cutvelvet render <project> <timeline> [--scale 0.5] [--jobs N] [--shots S1a,S2b] [--out path]
    cutvelvet stills <project> <timeline> t1,t2,... [--scale 0.5] [--out dir]
    cutvelvet audit  <project> [timeline] [--strict | --allow-repeats]
    cutvelvet publish <project> <timeline> [--scale 0.5] [--jobs N] [--strict | --allow-repeats]
    cutvelvet grid   <project> <src> t1[,t2,...] [--out dir]    frames of a library clip with a 10x10 grid, for placing crops
    cutvelvet probe  <project> <src>                            duration, size and fps of a library clip, as one JSON line
    cutvelvet schema                                            the timeline file format

grid and probe only read clips inside the project's library folders, and grid only writes inside the project.
During an agent run (the runner sets $CUTVELVET_PROJECT) every command is confined to that project: another
project folder, an --out path outside it, `serve` and `new` are refused. That is what lets the agent's shell
permission be just `cutvelvet ...`, with no direct ffmpeg.

Repeated footage only fails audit / publish while the project's "Never use the same moment of footage
twice" rule is switched on (rules.json, see rules.py); otherwise repeats are listed as warnings.
--strict / --allow-repeats override the rule for one command.

Without --jobs (or $JOBS) the number of shots rendered in parallel, and whether the work runs at low
priority, come from this machine's performance setting (~/.cutvelvet/settings.json, see settings.py); a
render in progress picks up a changed setting for the shots it has not started yet.
    cutvelvet serve  <project> [--port 8771]
    cutvelvet new    <dir> [--name "My film"]        start a project folder (newcut.py)
    cutvelvet analyze <project> <audio> [--lyrics] [--force]   analyse a song for cutting (newcut.py)

The render functions (render_timeline, render_stills, build_audio) are importable for other front ends.
"""
import argparse
import hashlib
import json
import multiprocessing as mp
import os
import re
import shutil
import subprocess
import sys
import time
from multiprocessing.connection import wait as _wait

from . import engine, rules as RULES, settings as SETTINGS, timeline as TL


def _setup(project_dir, scale):
    proj = TL.load_project(project_dir)
    # One thread per ffmpeg: parallelism comes from rendering several shots at once (--jobs / the performance
    # setting), so "N at a time" bounds the CPU used instead of each encoder also fanning out over every core.
    engine.configure(root=proj['root'], scale=scale, canvas=proj['canvas'], fps=proj['fps'], threads=1)
    return proj

def _tag(scale): return 'draft' if float(scale) < 1 else 'full'
def _safe(s): return re.sub(r'[^A-Za-z0-9_.-]', '_', s)
def _cache_dir(proj):
    d = os.path.join(proj['root'], proj['cache']); os.makedirs(d, exist_ok=True); return d


# ───────────── audio ─────────────
def build_audio(proj, tl):
    """Mix the timeline's audio tracks to a cached 48k wav covering [0, duration]. Returns its path, or None."""
    tracks = TL.audio_tracks(tl)
    if not tracks: return None
    dur = tl['duration']
    # These values are written into an ffmpeg filter graph, and this is also reached for timelines that were not
    # validated (the live preview's audio): anything that is not a plain number stops here.
    for i, a in enumerate(tracks):
        if not isinstance(a, dict) or not isinstance(a.get('src'), str): raise TL.TimelineError(f'audio[{i}]: not a track with a "src"')
        for k in ('start', 'ss', 'dur', 'fadein', 'fadeout', 'fadeout_at', 'gain'):
            if k in a and not TL._num(a[k]): raise TL.TimelineError(f'audio[{i}].{k} must be a number')
    if not TL._num(dur) or dur <= 0: raise TL.TimelineError('"duration" must be a positive number')
    srcs = [TL.resolve(proj, a['src']) for a in tracks]
    for s in srcs:
        if not os.path.exists(s): raise TL.TimelineError(f'audio source not found: {s}')
    sig = hashlib.md5(json.dumps([tracks, dur, [os.path.getmtime(s) for s in srcs]], sort_keys=True).encode()).hexdigest()[:10]
    out = os.path.join(_cache_dir(proj), f'{_safe(tl["id"])}_audio_{sig}.wav')
    if os.path.exists(out): return out
    cmd = [engine.FFMPEG, '-v', 'error', '-y']; chains = []
    for i, (a, src) in enumerate(zip(tracks, srcs)):
        start = a.get('start', 0)
        if a.get('ss'): cmd += ['-ss', str(a['ss'])]
        cmd += ['-t', str(a.get('dur', max(0.1, dur - start))), '-i', src]
        f = []
        if start > 0: f.append(f'adelay={int(round(start * 1000))}:all=1')
        if a.get('fadein', 0) > 0: f.append(f'afade=t=in:st={start}:d={a["fadein"]}')
        if a.get('fadeout', 0) > 0: f.append(f'afade=t=out:st={a.get("fadeout_at", dur - a["fadeout"])}:d={a["fadeout"]}')
        if a.get('gain', 1) != 1: f.append(f'volume={a["gain"]}')
        chains.append(f'[{i}:a]{",".join(f) or "anull"}[a{i}]')
    ins = ''.join(f'[a{i}]' for i in range(len(tracks)))
    mix = f'amix=inputs={len(tracks)}:normalize=0,' if len(tracks) > 1 else ''
    chains.append(f'{ins}{mix}atrim=0:{dur}[o]')
    tmp = out + '.tmp.wav'
    subprocess.run(cmd + ['-filter_complex', ';'.join(chains), '-map', '[o]', '-ar', '48000', tmp], check=True)
    os.replace(tmp, out); return out


# ───────────── video ─────────────
_JOB = {}      # set in the parent before the pool forks
def _render_shot(proj, tl, shot, path):
    fps = proj['fps']; n0, n1 = TL.shot_frames(shot, fps)
    P, X = TL.visible(tl, n0, n1, fps)
    engine.render(P, X, TL.build_fx(tl, shot), n1 / fps, path, t_start=n0 / fps, quiet=True)

def _seg(a):
    sid, path = a
    _render_shot(_JOB['proj'], _JOB['tl'], TL.get_shot(_JOB['tl'], sid), path); return sid

def _run_shots(proj, tl, todo, jobs, scale, log):
    """Render the (shot id, path) pairs, a few at a time. jobs=None follows the machine's performance setting,
    re-read whenever a slot frees up, so changing it mid-render takes effect for shots not yet started."""
    limit = SETTINGS.jobs if jobs is None else (lambda: max(1, int(jobs)))
    root, tid, fps = proj['root'], tl['id'], proj['fps']
    frames = {}
    for sid, _ in todo:
        n0, n1 = TL.shot_frames(TL.get_shot(tl, sid), fps); frames[sid] = n1 - n0
    secs, queue, total = {}, list(todo), len(todo)
    def status(running): SETTINGS.write_render_status(root, {'timeline': tid, 'scale': scale, 'total': total, 'done': len(secs), 'running': running, 'jobs': limit()})
    _JOB.update(proj=proj, tl=tl)
    try:
        if total == 1 or 'fork' not in mp.get_all_start_methods():
            for a in queue:
                status(1); t0 = time.time(); _seg(a); secs[a[0]] = time.time() - t0; log('done', a[0], '%.1fs' % secs[a[0]])
            return
        ctx, running = mp.get_context('fork'), {}
        try:
            while queue or running:
                while queue and len(running) < limit():
                    a = queue.pop(0); p = ctx.Process(target=_seg, args=(a,)); p.start(); running[p.sentinel] = (p, a, time.time())
                status(len(running))
                for sentinel in _wait(list(running), timeout=2):
                    p, a, t0 = running.pop(sentinel); p.join()
                    if p.exitcode != 0:
                        if os.path.exists(a[1]): os.remove(a[1])     # never leave a half-written shot in the cache
                        raise TL.TimelineError(f'rendering shot {a[0]} failed (exit {p.exitcode})')
                    secs[a[0]] = time.time() - t0; log('done', a[0], '%.1fs' % secs[a[0]])
        finally:
            for p, a, _ in running.values():
                p.terminate(); p.join()
                if os.path.exists(a[1]): os.remove(a[1])
    finally:
        SETTINGS.write_render_status(root, None)
        SETTINGS.record_timings(root, tid, _tag(scale), secs, frames)

def shot_cache_path(proj, tl, shot, scale):
    return os.path.join(_cache_dir(proj), f'{_safe(tl["id"])}_{_tag(scale)}_{_safe(shot["id"])}_{TL.shot_key(tl, shot, proj, scale)}.mp4')

def render_timeline(project_dir, tid, scale=1.0, jobs=None, shots=None, out=None, log=print):
    """Render every shot (or just `shots`) to cached clips in parallel, concatenate, mux audio. Returns the output path."""
    proj = _setup(project_dir, scale); tl = TL.load_timeline(proj, tid)
    _, warns = TL.validate(tl, proj)
    for w in warns: log('warning:', w)
    bad = TL.audit(proj, tl['id'])
    for a, b in bad: log('REPEAT', a, b)
    log('no repeated cuts' if not bad else 'REPEATS FOUND')
    sel = [TL.get_shot(tl, s) for s in shots] if shots else tl['shots']
    order = {s['id']: i for i, s in enumerate(tl['shots'])}
    sel = sorted(sel, key=lambda s: order[s['id']])
    audio = build_audio(proj, tl)
    segs, todo = [], []
    for s in sel:
        path = shot_cache_path(proj, tl, s, scale); segs.append(path)
        if not os.path.exists(path): todo.append((s['id'], path))
    log('re-rendering:', [a[0] for a in todo] or 'nothing (all cached)')
    if todo:
        log('rendering', len(todo), 'shot(s),', (str(jobs) if jobs is not None else '%d (%s setting)' % (SETTINGS.jobs(), SETTINGS.performance()['mode'])), 'at a time')
        _run_shots(proj, tl, todo, jobs, scale, log)
    partial = len(sel) != len(tl['shots'])
    if out is None:
        name = f'{_safe(tl["id"])}_{_tag(scale)}' + ('_' + '+'.join(_safe(s['id']) for s in sel) if partial else '') + '.mp4'
        out = os.path.join(proj['root'], proj['out'], name)
    out = os.path.abspath(out); os.makedirs(os.path.dirname(out), exist_ok=True)
    lst = os.path.join(_cache_dir(proj), f'{_safe(tl["id"])}_list_{os.getpid()}.txt')
    with open(lst, 'w') as f: f.write(''.join("file '%s'\n" % s.replace("'", "'\\''") for s in segs))
    fps = proj['fps']
    contiguous = all(TL.shot_frames(a, fps)[1] == TL.shot_frames(b, fps)[0] for a, b in zip(sel, sel[1:]))
    cmd = [engine.FFMPEG, '-v', 'error', '-y', '-f', 'concat', '-safe', '0', '-i', lst]
    if audio and contiguous:
        t0 = TL.shot_frames(sel[0], fps)[0] / fps
        cmd += (['-ss', str(t0)] if t0 > 0 else []) + ['-i', audio, '-c:v', 'copy', '-c:a', 'aac', '-b:a', '256k', '-shortest']
    else:
        if audio: log('selected shots are not contiguous: writing without audio')
        cmd += ['-c:v', 'copy']
    try: subprocess.run(cmd + ['-movflags', '+faststart', out], check=True)
    finally: os.remove(lst)
    log('wrote', out); return out

def render_stills(project_dir, tid, times, scale=1.0, out_dir=None, log=print):
    """One jpg per time (seconds). Returns the list of paths."""
    proj = _setup(project_dir, scale); tl = TL.load_timeline(proj, tid); fps = proj['fps']
    if out_dir is None: out_dir = os.path.join(os.path.dirname(_cache_dir(proj)), 'stills', _safe(tl['id']))
    os.makedirs(out_dir, exist_ok=True)
    tmp = os.path.join(_cache_dir(proj), f'_still_{os.getpid()}.mp4'); outs = []
    try:
        for tt in times:
            n = int(round(tt * fps)); path = os.path.join(out_dir, f't{tt:06.2f}.jpg')
            P, X = TL.visible(tl, n, n + 1, fps)
            engine.render(P, X, TL.build_fx(tl, TL.shot_at(tl, tt, fps)), (n + 1) / fps, tmp, t_start=n / fps, preview={n: path}, quiet=True)
            outs.append(path); log('wrote', path)
    finally:
        for f in (tmp, tmp + '.v.mp4'):
            if os.path.exists(f): os.remove(f)
    return outs


# ───────────── commands ─────────────
def repeats_fail(project_dir, strict=None):
    """Do repeats fail the audit? An explicit strict=True/False wins; otherwise the project's no-repeats rule decides."""
    return RULES.is_on(os.path.abspath(project_dir), RULES.REPEATS_RULE) if strict is None else bool(strict)

def cmd_audit(project_dir, tid=None, log=print, strict=None):
    fail = repeats_fail(project_dir, strict)
    proj = TL.load_project(project_dir); tids = [tid] if tid else TL.list_timelines(proj); ok = True
    for t in tids:
        tl = TL.load_timeline(proj, t, strict=False); errs, warns = TL.validate(tl, proj)
        for e in errs: log(f'{t}: ERROR {e}')
        for w in warns: log(f'{t}: warning {w}')
        ok = ok and not errs
    bad = TL.audit(proj, TL.load_timeline(proj, tid, strict=False).get('id', tid) if tid else None)
    for a, b in bad:
        log(('REPEAT' if fail else 'warning: repeat') + f' {a["src"]}: {a["timeline"]}/{a["shot"]} [{a["a"]:.2f}-{a["b"]:.2f}s]  x  {b["timeline"]}/{b["shot"]} [{b["a"]:.2f}-{b["b"]:.2f}s]')
    log(f'{", ".join(tids) or "(no timelines)"}: ' + ('no repeated cuts' if not bad else f'{len(bad)} REPEATS FOUND' if fail
                                                         else f'{len(bad)} repeats (allowed: the no-repeats rule is off for this project)'))
    return ok and not (bad and fail)

# ───────────── publish ─────────────
def review_dir(proj):
    return os.path.join(proj['root'], proj.get('review') or 'review')

def published_versions(proj, tid):
    """Version numbers already taken for a timeline: review/<tid>_v<N>.mp4 and timelines/.history/<tid>_v<N>.json."""
    ns = set()
    for d, ext in ((review_dir(proj), '.mp4'), (os.path.join(TL.timelines_dir(proj), '.history'), '.json')):
        if os.path.isdir(d):
            for f in os.listdir(d):
                m = re.match(r'^%s_v(\d+)%s$' % (re.escape(tid), re.escape(ext)), f)
                if m: ns.add(int(m.group(1)))
    return sorted(ns)

def shots_json(tl, fps):
    """The review page's shot list: [{"id", "name", "t0", "t1"}] with cuts on the frame grid."""
    out = []
    for s in tl['shots']:
        n0, n1 = TL.shot_frames(s, fps)
        out.append({'id': s['id'], 'name': s.get('label') or s['id'], 't0': n0 / fps, 't1': n1 / fps})
    return out

def cmd_publish(project_dir, tid, scale=0.5, jobs=None, log=print, strict=None):
    """audit -> render -> review/<tid>_v<N+1>.mp4 + _shots.json -> timelines/.history/<tid>_v<N+1>.json. Returns the version name."""
    proj = TL.load_project(project_dir)
    if not cmd_audit(project_dir, tid, log=log, strict=strict):
        raise TL.TimelineError('audit failed: fix what it reports above, nothing was published')
    tl = TL.load_timeline(proj, tid); src = TL.timeline_path(proj, tid)
    with open(src, 'rb') as f: snapshot = f.read()          # exactly what gets rendered
    name = '%s_v%d' % (tid, (published_versions(proj, tid) or [0])[-1] + 1)
    rdir = review_dir(proj); hdir = os.path.join(TL.timelines_dir(proj), '.history')
    os.makedirs(rdir, exist_ok=True); os.makedirs(hdir, exist_ok=True)
    # Rendered straight into review/ under a hidden name (the page only lists <id>_v<N>.mp4), so there is no second
    # copy in renders/; the mp4 takes its real name last, whole.
    part = os.path.join(rdir, '.' + name + '.part.mp4'); final = os.path.join(rdir, name + '.mp4')
    try:
        render_timeline(project_dir, tid, scale=scale, jobs=jobs, out=part, log=lambda *a: log(*[(final if x == part else x) for x in a]))
        with open(src, 'rb') as f:
            if f.read() != snapshot: raise TL.TimelineError('the timeline changed while it was rendering: publish again')
        if os.path.exists(final): raise TL.TimelineError(f'{final} appeared during the render: publish again')
        with open(os.path.join(hdir, name + '.json'), 'wb') as f: f.write(snapshot)
        with open(os.path.join(rdir, name + '_shots.json'), 'w') as f: json.dump(shots_json(tl, proj['fps']), f, indent=1)
        os.replace(part, final)
    finally:
        for leftover in (part, part + '.v.mp4'):
            if os.path.exists(leftover): os.remove(leftover)
    log('published', name); return name

# ───────────── source frames for placing crops ─────────────
def _inside(root, path):
    root, path = os.path.realpath(root), os.path.realpath(path)
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)

def _library_clip(project_dir, src):
    """Absolute path of a clip, only if it is a video inside one of the project's library folders in use."""
    from . import library as LIB
    real = LIB.Library(os.path.abspath(project_dir)).clip_path(src)
    if not real: raise TL.TimelineError(f'not a clip in this project\'s library: {src}')
    return real

def cmd_probe(project_dir, src):
    from . import library as LIB
    info = LIB.probe(_library_clip(project_dir, src))
    if not info: raise TL.TimelineError(f'cannot read {src} as video')
    return {'src': src, 'duration': info['duration'], 'width': info['width'], 'height': info['height'], 'fps': info['fps']}

def cmd_grid(project_dir, src, times, out_dir=None, log=print):
    """One jpg per time (source seconds): the frame at 1280 px wide with a 10x10 grid over it, so a crop's centre can be
    read off in tenths of the frame. Written to <project>/.cutvelvet/grid/ (or --out, which must be inside the project)."""
    from . import library as LIB
    root = os.path.abspath(project_dir); path = _library_clip(root, src)
    out_dir = os.path.abspath(out_dir) if out_dir else os.path.join(root, '.cutvelvet', 'grid')
    if not _inside(root, out_dir): raise TL.TimelineError(f'--out must be inside the project folder: {out_dir}')
    if not times or len(times) > 40: raise TL.TimelineError('give between 1 and 40 times, in seconds')
    os.makedirs(out_dir, exist_ok=True); outs = []
    stem = _safe(os.path.splitext(os.path.basename(src))[0])
    for t in times:
        if not (0 <= t < 1e6): raise TL.TimelineError(f'not a time in seconds: {t}')
        out = os.path.join(out_dir, f'{stem}_{t:07.2f}.jpg')
        r = subprocess.run([LIB._bin('ffmpeg'), '-v', 'error', '-nostdin', '-y', '-ss', f'{t:.3f}', '-i', path, '-frames:v', '1',
                            '-vf', 'scale=1280:-2,drawgrid=w=iw/10:h=ih/10:t=1:c=yellow@0.6', '-q:v', '3', out], capture_output=True, text=True)
        if r.returncode != 0 or not os.path.exists(out): raise TL.TimelineError(f'no frame at {t}s in {src}: {(r.stderr or "").strip()[-200:] or "past the end of the clip?"}')
        outs.append(out); log(out)
    return outs

def _confine(args):
    """During an agent run the runner sets $CUTVELVET_PROJECT. The command line then works on that project only and
    writes inside it only, so an agent whose one shell permission is `cutvelvet ...` cannot use it to reach elsewhere."""
    root = os.environ.get('CUTVELVET_PROJECT')
    if not root: return
    def no(why): sys.exit(f'cutvelvet: {why} (this is an agent run, confined to {root})')
    if args.cmd in ('serve', 'new'): no(f'"{args.cmd}" is not available here')
    proj = getattr(args, 'project', None)
    if proj is not None and os.path.realpath(proj) != os.path.realpath(root): no(f'{proj} is not this run\'s project')
    for name in ('out', 'audio'):
        v = getattr(args, name, None)
        if v and not _inside(root, v if os.path.isabs(v) else os.path.join(os.getcwd(), v)): no(f'{v} is outside the project folder')

def main(argv=None):
    ap = argparse.ArgumentParser(prog='cutvelvet', description='JSON timelines -> layered strip compositor -> ffmpeg')
    sub = ap.add_subparsers(dest='cmd', required=True)
    env_scale = float(os.environ.get('SCALE', '1')); env_jobs = int(os.environ['JOBS']) if os.environ.get('JOBS') else None
    r = sub.add_parser('render', help='render a timeline (per-shot cache, parallel) and mux audio')
    r.add_argument('project'); r.add_argument('timeline')
    r.add_argument('--scale', type=float, default=env_scale, help='0.5 = fast draft (default $SCALE or 1)')
    r.add_argument('--jobs', type=int, default=env_jobs, help='parallel shot renders (default $JOBS, else this machine\'s performance setting)')
    r.add_argument('--shots', help='comma-separated shot ids: render and join only these')
    r.add_argument('--out', help='output mp4 (default <project>/<out>/<timeline>_<draft|full>.mp4)')
    s = sub.add_parser('stills', help='render single frames')
    s.add_argument('project'); s.add_argument('timeline'); s.add_argument('times', help='comma-separated seconds')
    s.add_argument('--scale', type=float, default=env_scale); s.add_argument('--out', help='output directory (default <project>/.cutvelvet/stills/<timeline>)')
    a = sub.add_parser('audit', help='validate timelines and report repeated footage')
    a.add_argument('project'); a.add_argument('timeline', nargs='?')
    for x in (a,):
        x.add_argument('--strict', dest='strict', action='store_const', const=True, default=None, help='repeated footage fails, whatever the project rules say')
        x.add_argument('--allow-repeats', dest='strict', action='store_const', const=False, help='repeated footage is only a warning, whatever the project rules say')
    pb = sub.add_parser('publish', help='audit, render, and add the result to review/ as the next version')
    pb.add_argument('project'); pb.add_argument('timeline')
    pb.add_argument('--scale', type=float, default=0.5, help='0.5 = draft size (default)'); pb.add_argument('--jobs', type=int, default=env_jobs, help='parallel shot renders (default $JOBS, else this machine\'s performance setting)')
    pb.add_argument('--strict', dest='strict', action='store_const', const=True, default=None, help='repeated footage fails, whatever the project rules say')
    pb.add_argument('--allow-repeats', dest='strict', action='store_const', const=False, help='repeated footage is only a warning, whatever the project rules say')
    gr = sub.add_parser('grid', help='frames of a library clip with a 10x10 grid over them, for placing crops')
    gr.add_argument('project'); gr.add_argument('src', help='the clip, as written in the timeline (project-relative)'); gr.add_argument('times', help='comma-separated source seconds')
    gr.add_argument('--out', help='output directory inside the project (default <project>/.cutvelvet/grid)')
    pr = sub.add_parser('probe', help='duration, size and fps of a library clip, as one JSON line')
    pr.add_argument('project'); pr.add_argument('src')
    sub.add_parser('schema', help='print the timeline file format')
    v = sub.add_parser('serve', help='open the project in the web UI')
    v.add_argument('project'); v.add_argument('--port', type=int, default=8771)
    nw = sub.add_parser('new', help='start a new project folder')
    nw.add_argument('dir'); nw.add_argument('--name')
    an = sub.add_parser('analyze', help='analyse a song (beats, lines, sections) for cutting')
    an.add_argument('project'); an.add_argument('audio')
    an.add_argument('--lyrics', action='store_true', help='also find lyric lines (optional, slow)'); an.add_argument('--force', action='store_true', help='ignore the cached analysis')
    args = ap.parse_args(argv)
    _confine(args)
    if args.cmd == 'schema':
        print(TL.__doc__.strip()); return
    if args.cmd in ('new', 'analyze'):
        try: from . import newcut
        except ImportError as e: sys.exit(f'cutvelvet: {args.cmd} is not available in this install ({e})')
        r = newcut.cli_new(args) if args.cmd == 'new' else newcut.cli_analyze(args)
        sys.exit(r if isinstance(r, int) else 0)
    try:
        if args.cmd in ('render', 'publish', 'stills'): SETTINGS.be_nice()
        if args.cmd == 'render':
            render_timeline(args.project, args.timeline, scale=args.scale, jobs=args.jobs, shots=[x for x in args.shots.split(',') if x] if args.shots else None, out=args.out, log=lambda *a: print(*a, flush=True))
        elif args.cmd == 'stills':
            render_stills(args.project, args.timeline, [float(x) for x in args.times.split(',') if x], scale=args.scale, out_dir=args.out)
        elif args.cmd == 'audit':
            sys.exit(0 if cmd_audit(args.project, args.timeline, strict=args.strict) else 1)
        elif args.cmd == 'publish':
            print(cmd_publish(args.project, args.timeline, scale=args.scale, jobs=args.jobs, strict=args.strict, log=lambda *a: print(*a, flush=True)), flush=True)
        elif args.cmd == 'grid':
            try: times = [float(x) for x in args.times.split(',') if x.strip()]
            except ValueError: raise TL.TimelineError(f'times must be seconds separated by commas: {args.times}')
            cmd_grid(args.project, args.src, times, out_dir=args.out, log=lambda *a: print(*a, flush=True))
        elif args.cmd == 'probe':
            print(json.dumps(cmd_probe(args.project, args.src)), flush=True)
        elif args.cmd == 'serve':
            from cutvelvet import server
            server.main(os.path.abspath(args.project), args.port)
    except TL.TimelineError as e:
        sys.exit(f'cutvelvet: {e}')

if __name__ == '__main__':
    main()
