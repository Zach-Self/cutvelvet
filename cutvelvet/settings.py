"""Per-machine settings and "how busy is this computer" -- about the computer, not the film.

``~/.cutvelvet/settings.json`` ($CUTVELVET_HOME overrides the folder)::

    {"performance": {"mode": "balanced"}}            light | balanced | fast
    {"performance": {"mode": "custom", "jobs": 3}}

Performance decides how many shots render in parallel when ``--jobs`` is not given (render, publish, a Run)
and how many thumbnail jobs run at once: Light / Balanced / Fast = about 25% / 50% / 75% of the cores
(at least 1). Light and Balanced also run the work at low priority (``nice``). Default: Balanced.

Renders report into ``<project>/.cutvelvet/render_status.json`` (what is rendering now) and
``<project>/.cutvelvet/timings.json`` (seconds each shot took last time, used for estimates).
Stdlib only.
"""
import json
import os
import subprocess
import time

MODES = {'light': 0.25, 'balanced': 0.5, 'fast': 0.75}
NICE = {'light': 10, 'balanced': 10, 'fast': 0, 'custom': 0}
DEFAULT_MODE = 'balanced'


def home():
    return os.environ.get('CUTVELVET_HOME') or os.path.join(os.path.expanduser('~'), '.cutvelvet')


def path():
    return os.path.join(home(), 'settings.json')


def cores():
    return os.cpu_count() or 1


CORES_PER_JOB = 1.3   # measured: one shot render (compositing + one-thread ffmpeg decode and encode) keeps about 1.3 cores busy

def mode_jobs(mode):
    """Shots rendered at once so that the whole render uses about MODES[mode] of the cores."""
    return max(1, int(round(cores() * MODES[mode] / CORES_PER_JOB)))

def cores_for(jobs):
    """About how many cores `jobs` shot renders keep busy, for the estimate shown before a Run or Publish."""
    return max(1, min(cores(), int(round(jobs * CORES_PER_JOB))))


def _read_json(p):
    try:
        with open(p, encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(p, data):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = '%s.%d.tmp' % (p, os.getpid())
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, p)


def performance():
    """{"mode", "jobs", "nice", "thumb_jobs", "options": {mode: jobs}} -- read fresh from disk every call."""
    perf = _read_json(path()).get('performance')
    perf = perf if isinstance(perf, dict) else {}
    mode = perf.get('mode') if perf.get('mode') in MODES or perf.get('mode') == 'custom' else DEFAULT_MODE
    if mode == 'custom':
        j = perf.get('jobs')
        if isinstance(j, (int, float)) and not isinstance(j, bool) and 1 <= j <= 256:
            jobs = int(j)
        else:
            mode, jobs = DEFAULT_MODE, mode_jobs(DEFAULT_MODE)
    else:
        jobs = mode_jobs(mode)
    thumbs = {'light': 1, 'balanced': 2, 'fast': 3}.get(mode) or max(1, min(4, jobs // 2))
    return {'mode': mode, 'jobs': jobs, 'nice': NICE[mode], 'thumb_jobs': thumbs, 'options': {m: mode_jobs(m) for m in MODES}}


def set_performance(mode, jobs=None):
    if mode == 'custom':
        if not (isinstance(jobs, (int, float)) and not isinstance(jobs, bool) and 1 <= jobs <= 256):
            raise ValueError('custom needs a number of parallel jobs (1 or more)')
        perf = {'mode': 'custom', 'jobs': int(jobs)}
    elif mode in MODES:
        perf = {'mode': mode}
    else:
        raise ValueError('mode must be light, balanced, fast or custom')
    d = _read_json(path())
    d['performance'] = perf
    _write_json(path(), d)
    return performance()


def jobs():
    return performance()['jobs']


def be_nice():
    """Lower this process's priority as the performance setting asks (children inherit it)."""
    n = performance()['nice']
    try:
        cur = os.nice(0)
        if n > cur:
            os.nice(n - cur)
    except OSError:
        pass
    return n


# ---------------------------------------------------------------- what this machine trusts from a project
# A project folder can come from anyone. Two things in it are only honoured after the user has allowed them on this
# machine, and both are remembered here (in the per-machine settings file, never in the project):
#   "folders": {"/abs/project": ["/real/outside/folder", ...]}   library folders that lie outside the project
#   "prompts": {"/abs/project": {"run_prompt.md": "<digest>"}}   the project's own instructions for the agent
PROMPT_FILES = ('run_prompt.md', 'newcut_prompt.md')


def _trust(kind, project_dir):
    d = _read_json(path()).get(kind)
    v = d.get(os.path.abspath(project_dir)) if isinstance(d, dict) else None
    return v


def _set_trust(kind, project_dir, value):
    d = _read_json(path())
    if not isinstance(d.get(kind), dict):
        d[kind] = {}
    d[kind][os.path.abspath(project_dir)] = value
    _write_json(path(), d)


def folder_allowed(project_dir, folder):
    """Is this folder (outside the project) one the user added to this project's library on this machine?"""
    v = _trust('folders', project_dir)
    return isinstance(v, list) and os.path.realpath(folder) in v


def allow_folder(project_dir, folder):
    v = _trust('folders', project_dir)
    v = [x for x in v if isinstance(x, str)] if isinstance(v, list) else []
    real = os.path.realpath(folder)
    if real not in v:
        _set_trust('folders', project_dir, v + [real])


def project_prompt(project_dir, name):
    """A project's own prompt template (``<project>/.cutvelvet/<name>``): None when there is none, else
    {"name", "text", "digest", "approved"}. Use ``text`` only when ``approved`` is true: the file steers what the
    agent does, so it counts only once the user has approved exactly this text on this machine."""
    if name not in PROMPT_FILES:
        return None
    try:
        with open(os.path.join(project_dir, '.cutvelvet', name), encoding='utf-8', errors='replace') as f:
            text = f.read(200000)
    except OSError:
        return None
    import hashlib
    digest = hashlib.sha256(text.encode('utf-8')).hexdigest()[:24]
    v = _trust('prompts', project_dir)
    return {'name': name, 'text': text, 'digest': digest, 'approved': isinstance(v, dict) and v.get(name) == digest}


def approve_prompt(project_dir, name, digest):
    """Approve the prompt file as it is now. `digest` must be the one the user was shown, so a file that changed
    in between is not approved unseen. Returns True when approved."""
    p = project_prompt(project_dir, name)
    if not p or p['digest'] != digest:
        return False
    v = _trust('prompts', project_dir)
    v = dict(v) if isinstance(v, dict) else {}
    v[name] = digest
    _set_trust('prompts', project_dir, v)
    return True


# ---------------------------------------------------------------- render status + timings (written by the CLI)

def _dot(project_dir, name):
    return os.path.join(project_dir, '.cutvelvet', name)


def write_render_status(project_dir, status):
    """status = {timeline, scale, total, done, running, jobs} or None when the render is over."""
    p = _dot(project_dir, 'render_status.json')
    try:
        if status is None:
            if _read_json(p).get('pid') == os.getpid():
                os.remove(p)
        else:
            _write_json(p, dict(status, pid=os.getpid(), updated=time.time()))
    except OSError:
        pass


def alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def render_status(project_dir):
    d = _read_json(_dot(project_dir, 'render_status.json'))
    return d if d and alive(d.get('pid')) else None


def record_timings(project_dir, timeline, tag, secs_by_shot, frames_by_shot):
    """Remember how long each shot took (wall seconds while rendering alongside others) for later estimates."""
    if not secs_by_shot:
        return
    p = _dot(project_dir, 'timings.json')
    d = _read_json(p)
    shots = d.setdefault('shots', {}).setdefault(timeline, {}).setdefault(tag, {})
    for sid, secs in secs_by_shot.items():
        shots[sid] = {'secs': round(secs, 2), 'frames': frames_by_shot.get(sid), 'at': int(time.time())}
    # a per-frame figure for shots that have never been rendered
    fr = sum(v['frames'] or 0 for v in shots.values())
    if fr:
        d.setdefault('per_frame', {})[tag] = round(sum(v['secs'] for v in shots.values() if v['frames']) / fr, 4)
    try:
        _write_json(p, d)
    except OSError:
        pass


def timings(project_dir):
    return _read_json(_dot(project_dir, 'timings.json'))


def estimate_seconds(project_dir, timeline, tag, frames_by_shot, jobs):
    """Rough wall-clock seconds to render these shots `jobs` at a time, or None when nothing was ever timed."""
    t = timings(project_dir)
    known = ((t.get('shots') or {}).get(timeline) or {}).get(tag) or {}
    per_frame = (t.get('per_frame') or {}).get(tag)
    each = []
    for sid, frames in frames_by_shot.items():
        k = known.get(sid)
        if k and k.get('frames') and frames:
            each.append(k['secs'] * frames / k['frames'])
        elif k:
            each.append(k['secs'])
        elif per_frame and frames:
            each.append(per_frame * frames)
        else:
            return None
    if not each:
        return 0.0
    return max(max(each), sum(each) / max(1, min(jobs, len(each))))


# ---------------------------------------------------------------- machine load

def machine():
    try:
        load1 = os.getloadavg()[0]
    except (OSError, AttributeError):
        load1 = 0.0
    n = cores()
    return {'cores': n, 'load1': round(load1, 2), 'busy': int(max(0, min(100, round(load1 / n * 100))))}


def tree_cpu(roots):
    """Summed %CPU (100 = one core) of the given pids and all their descendants, via one ``ps`` call."""
    roots = {int(p) for p in roots if p}
    if not roots:
        return 0.0
    try:
        out = subprocess.run(['ps', '-axo', 'pid=,ppid=,pgid=,%cpu='], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return 0.0
    rows = []
    for line in out.splitlines():
        f = line.split()
        if len(f) == 4:
            try:
                rows.append((int(f[0]), int(f[1]), int(f[2]), float(f[3].replace(',', '.'))))
            except ValueError:
                pass
    kids = {}
    for pid, ppid, _, _ in rows:
        kids.setdefault(ppid, []).append(pid)
    mine, stack = set(), list(roots)
    while stack:
        p = stack.pop()
        if p not in mine:
            mine.add(p)
            stack += kids.get(p, [])
    return round(sum(c for pid, _, pgid, c in rows if pid in mine or pgid in roots), 1)
