"""New cut: start a timeline from nothing -- a song, the footage library and a few lines of intent.

    python -m cutvelvet.newcut new <dir> [--name "My film"]         scaffold a project folder
    python -m cutvelvet.newcut analyze <project> <audio> [--lyrics] [--force]

What happens when the page's "Build first cut" is pressed:

Sound is optional: a brief's ``sound`` is "song" (default), "audio" (anything else to cut to: a voice, ambience --
same analysis, cuts default to its sections) or "none" (a silent cut of ``length`` seconds: no analysis, no
``audio`` in the timeline, cut times free).

1. The brief is saved to ``<timelines>/briefs/<id>.brief.json`` (re-runnable; a sub-folder, because every
   ``*.json`` directly inside the timelines folder is taken for a timeline).
2. The song is analysed (``analyze``): duration, tempo + beat times, coarse sections and -- only when asked
   for and ``faster_whisper`` is installed -- lyric lines. Cached in ``.cutvelvet/analysis/<hash>.json``.
3. Working files for the agent are written to ``.cutvelvet/newcut/<id>/``: ``markers.json`` (the analysis
   shifted to timeline seconds, with the cut points the brief asked for), ``footage.json`` (the clips it
   may use) and ``sheet_NN.jpg`` contact sheets (the library's scrub sprites, a dozen clips per image).
4. A prompt is assembled (PROMPT below, or ``<project>/.cutvelvet/newcut_prompt.md``) and handed to the
   project's agent through ``Runner.start_job('newcut', prompt, id)``: the same single job slot, log,
   cancel and status as Run notes.

HTTP (all under /api/new, routed here by server.py -- see ``handle``)::

    GET  /api/new                      project, songs, footage folders + clips, rules, agent, job, past runs
    GET  /api/new/analysis?song=S      {state: none|running|done|failed, analysis, envelope}
    POST /api/new/analyze              {"song": S, "lyrics": bool, "force": bool} -> starts it, same answer
    GET  /api/new/audio?song=S         the audio file (Range), for listening to the chosen window
    GET  /api/new/brief?id=ID          the saved brief, or {}
    POST /api/new/brief                {brief} -> saved
    POST /api/new/estimate             {brief} -> what a run would involve, and every reason it cannot start
    POST /api/new/prompt               {brief} -> the prompt as it would be sent (nothing is started)
    POST /api/new/start                {brief} -> save, prepare, start the agent
    POST /api/new/song/pick            native file chooser on this machine; the file is copied into music/
    POST /api/new/song/upload?name=N   body = one audio file, stored in music/

The analysis is deliberately simple: a spectral-flux onset envelope, its autocorrelation for the tempo, a
dynamic-programming beat tracker, and a change detector on band energies for sections. It reports how sure
it is and what it did not find (bars / downbeats are never detected). numpy is only imported when a song is
analysed, pillow only when contact sheets are made.
"""
import argparse
import hashlib
import importlib.util
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, unquote, urlsplit

from . import agents as AGENTS
from . import library as L
from . import rules as RULES
from . import settings as SETTINGS

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUDIO_EXT = {'.wav', '.mp3', '.m4a', '.aac', '.flac', '.aif', '.aiff', '.ogg', '.opus'}
AUDIO_TYPES = {'.wav': 'audio/wav', '.mp3': 'audio/mpeg', '.m4a': 'audio/mp4', '.aac': 'audio/aac', '.flac': 'audio/flac',
               '.aif': 'audio/aiff', '.aiff': 'audio/aiff', '.ogg': 'audio/ogg', '.opus': 'audio/ogg'}
ID_RE = re.compile(r'^[A-Za-z0-9]{1,40}$')
ANALYSIS_VERSION = 3
CLEAR_CHANGE = 1.75                       # a section change this many times the song's typical change is a clear one
MIN_PULSE = 1.8                           # onsets on the beats vs. anywhere: below this there is no beat worth reporting
SR, NFFT, HOP = 11025, 1024, 256          # analysis sample rate, window, hop (23 ms)
FRAME_RATE = SR / float(HOP)
ENV_STEP = 0.1                            # seconds per value of the display envelope
WHISPER_MODELS = ('tiny', 'base', 'small')  # never anything heavier than "small"
SNAPS = ('beats', 'lines', 'sections', 'free')
MAX_SONG = 2 * 1024 ** 3
SHEET_W, SHEET_MAX_H, LABEL_H = 1600, 1500, 18
CHUNK = 256 * 1024
DEFAULT_AVG_SHOT = 5.0
MIN_LENGTH = 5.0


class NewCutError(ValueError):
    pass


# ───────────────────────────────────────────────────────────── 1. project scaffolding

PROJECT_DIRS = ('timelines', 'footage', 'music', 'review', 'feedback')


def new_project(project_dir, name=None):
    """Create the folder layout of a project. Never overwrites: an existing project.json is left exactly as it is
    and only missing folders are added. Returns {"dir", "created", "made": [what was created]}."""
    root = os.path.abspath(os.path.expanduser(project_dir))
    if os.path.exists(root) and not os.path.isdir(root):
        raise NewCutError('not a folder: %s' % root)
    made = []
    os.makedirs(root, exist_ok=True)
    pj = os.path.join(root, 'project.json')
    created = not os.path.exists(pj)
    if created:
        L.write_json(pj, {'name': (name or '').strip() or os.path.basename(root.rstrip(os.sep)) or 'Untitled',
                          'canvas': [1920, 1080], 'fps': 24, 'library': ['footage'], 'timelines': 'timelines',
                          'cache': '.cutvelvet/cache', 'out': 'renders'})
        made.append('project.json')
        gi = os.path.join(root, '.gitignore')
        if not os.path.exists(gi):  # caches and run logs hold local paths; keys live in .env files
            with open(gi, 'w') as f:
                f.write('.cutvelvet/\n.env\n.env.*\nrenders/\n')
            made.append('.gitignore')
    proj = L.load_project(root)
    dirs = [proj.get('timelines') or 'timelines', 'footage', proj.get('music') or 'music',
            proj.get('review') or 'review', proj.get('feedback') or 'feedback']
    for d in dirs:
        p = L.abs_path(root, d)
        if not os.path.isdir(p):
            os.makedirs(p)
            made.append(d + '/')
    return {'dir': root, 'created': created, 'made': made}


def _proj_dirs(project_dir):
    proj = L.load_project(project_dir)
    return {k: L.abs_path(project_dir, proj.get(k) or k) for k in ('timelines', 'music', 'review', 'feedback')}


def music_dir(project_dir):
    return _proj_dirs(project_dir)['music']


def song_path(project_dir, song):
    """Absolute path of a brief's ``song`` -- only an audio file that really is inside the project's music folder
    (symlinks followed: a project can come from anyone, and this file is served and decoded); else None."""
    if not isinstance(song, str) or not song.strip() or '\0' in song:
        return None
    p = L.abs_path(project_dir, song.strip())
    if os.path.splitext(p)[1].lower() not in AUDIO_EXT or not L.inside(music_dir(project_dir), p):
        return None
    return p


# ───────────────────────────────────────────────────────────── 2. song analysis

def _dump(obj):
    """Readable JSON with number lists kept on one line (beat lists are long)."""
    s = json.dumps(obj, indent=1, ensure_ascii=False)
    return re.sub(r'\[\s*((?:-?[\d.eE+-]+,\s*)*-?[\d.eE+-]+)\s*\]', lambda m: '[' + re.sub(r'\s+', ' ', m.group(1)) + ']', s) + '\n'


def _write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = '%s.%d.%d.tmp' % (path, os.getpid(), threading.get_ident())
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(_dump(obj))
    os.replace(tmp, path)


def _read(path):
    try:
        with open(path, encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def _nice(cmd):
    return (['nice', '-n', '10'] if shutil.which('nice') else []) + cmd


def probe_audio(path):
    """Duration in seconds of the first audio stream, or None when the file is not readable audio."""
    try:
        o = subprocess.run([L._bin('ffprobe'), '-v', 'error', '-select_streams', 'a:0', '-show_entries',
                            'stream=duration:format=duration', '-of', 'json', path], capture_output=True, text=True, timeout=60)
        d = json.loads(o.stdout or '{}')
        if not d.get('streams'):
            return None
        dur = float((d['streams'][0] or {}).get('duration') or 0) or float((d.get('format') or {}).get('duration') or 0)
        return round(dur, 3) if dur > 0 else None
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        return None


def find_project(path):
    """The nearest folder at or above `path` that holds a project.json, or None."""
    d = os.path.abspath(path if os.path.isdir(path) else os.path.dirname(path))
    while True:
        if os.path.isfile(os.path.join(d, 'project.json')):
            return d
        up = os.path.dirname(d)
        if up == d:
            return None
        d = up


def analysis_path(audio_path, project_dir=None):
    """Where the analysis of this exact file (path + size + modification time) is cached."""
    a = os.path.realpath(audio_path)
    st = os.stat(a)
    key = hashlib.sha1(('%s|%d|%d' % (a, st.st_mtime_ns, st.st_size)).encode()).hexdigest()[:16]
    root = project_dir or find_project(a) or os.path.dirname(a)
    return os.path.join(os.path.abspath(root), '.cutvelvet', 'analysis', key + '.json')


def _decode(path):
    import numpy as np
    cmd = _nice([L._bin('ffmpeg'), '-v', 'error', '-nostdin', '-threads', '1', '-i', path, '-vn', '-ac', '1', '-ar', str(SR), '-f', 's16le', '-'])
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=900)
    except (OSError, subprocess.SubprocessError) as e:
        raise NewCutError('could not decode %s: %s' % (os.path.basename(path), e))
    if r.returncode != 0 or len(r.stdout) < SR:  # less than half a second of sound
        raise NewCutError('could not decode %s as audio%s' % (os.path.basename(path), ': ' + r.stderr.decode('utf-8', 'replace').strip()[-200:] if r.stderr else ''))
    return np.frombuffer(r.stdout[:len(r.stdout) // 2 * 2], dtype='<i2').astype(np.float32) / 32768.0


def _spectrum(y, bands=36):
    """(log band magnitudes [frames, bands], rms [frames]) on a 23 ms hop."""
    import numpy as np
    n = 1 + max(0, (len(y) - NFFT) // HOP)
    win = np.hanning(NFFT).astype(np.float32)
    freqs = np.fft.rfftfreq(NFFT, 1.0 / SR)
    edges = np.geomspace(60, SR / 2.0, bands + 1)
    idx = [np.where((freqs >= a) & (freqs < b))[0] for a, b in zip(edges[:-1], edges[1:])]
    idx = [i for i in idx if len(i)]
    S, rms = np.zeros((n, len(idx)), np.float32), np.zeros(n, np.float32)
    for s in range(0, n, 2048):  # in blocks: a ten-minute file stays under ~20 MB of working memory
        e = min(n, s + 2048)
        fr = np.lib.stride_tricks.sliding_window_view(y[s * HOP:(e - 1) * HOP + NFFT], NFFT)[::HOP]
        rms[s:e] = np.sqrt((fr ** 2).mean(1))
        m = np.abs(np.fft.rfft(fr * win, axis=1))
        for j, ix in enumerate(idx):
            S[s:e, j] = m[:, ix].mean(1)
    return np.log1p(50 * S), rms


def _smooth(x, n):
    import numpy as np
    n = max(1, int(n))
    return np.convolve(x, np.ones(n) / n, mode='same')


def _onsets(S):
    """Onset strength: positive spectral change, minus its local average, in units of its own deviation."""
    import numpy as np
    flux = np.concatenate([[0.0], np.maximum(0, np.diff(S, axis=0)).sum(1)])
    env = np.maximum(0, flux - _smooth(flux, FRAME_RATE * 0.4))
    sd = float(env.std())
    return env / sd if sd > 0 else env


def _tempo(env):
    """(bpm, period in frames, confidence 0..1, other candidates) from the onset envelope's autocorrelation."""
    import numpy as np
    x = env - env.mean()
    n = len(x)
    f = np.fft.rfft(x, 2 * n)
    ac = np.fft.irfft(f * np.conj(f))[:n]
    if ac[0] <= 0:
        return None
    ac = ac / ac[0]
    lo, hi = int(FRAME_RATE * 60 / 220.0), min(int(FRAME_RATE * 60 / 45.0), n - 2)
    if hi <= lo + 2:
        return None
    lags = np.arange(lo, hi)
    bpms = 60 * FRAME_RATE / lags
    score = ac[lags] * np.exp(-0.5 * (np.log2(bpms / 110.0) / 0.9) ** 2)  # mild preference for 60-200 bpm
    lag = int(lags[int(np.argmax(score))])
    a, b, c = ac[lag - 1], ac[lag], ac[lag + 1]
    den = a - 2 * b + c
    period = lag + ((a - c) / (2 * den) if den else 0.0)
    rng = ac[lags]
    conf = float((ac[lag] - np.median(rng)) / (4 * rng.std() + 1e-9))  # how far the peak stands out of the range
    alts = []
    for i in np.argsort(-score):
        cand = float(bpms[i])
        if all(abs(cand - o) / o > 0.08 for o in [60 * FRAME_RATE / period] + alts):
            alts.append(round(cand, 1))
        if len(alts) == 2:
            break
    return 60 * FRAME_RATE / period, period, max(0.0, min(1.0, conf)), alts


def _track(env, period, tight=100.0):
    """Beat frames: the path through the onset envelope that best trades onset strength against a steady period
    (Ellis 2007's dynamic programme)."""
    import numpy as np
    n = len(env)
    lo, hi = max(1, int(round(period / 2))), int(round(period * 2))
    if n <= hi + 2:
        return np.zeros(0, int)
    score, back = env.astype(float).copy(), np.full(n, -1)
    offs = np.arange(-hi, -lo + 1)
    pen = -tight * np.log(-offs / period) ** 2
    for t in range(hi, n):
        cand = score[t + offs] + pen
        k = int(np.argmax(cand))
        score[t] = env[t] + cand[k]
        back[t] = t + offs[k]
    last = n - max(1, int(period))
    t = int(np.argmax(score[last:])) + last
    beats = []
    while t >= 0:
        beats.append(t)
        t = int(back[t])
    return np.array(beats[::-1])


def _silences(rms, min_len=0.5):
    """[[start, end]] where the level stays 30 dB under the song's typical level for at least `min_len` seconds."""
    import numpy as np
    db = 20 * np.log10(_smooth(rms, FRAME_RATE * 0.2) + 1e-7)
    quiet = db < (np.median(db) - 30)
    out, start = [], None
    for i, q in enumerate(list(quiet) + [False]):
        if q and start is None:
            start = i
        elif not q and start is not None:
            if (i - start) / FRAME_RATE >= min_len:
                out.append([round(start / FRAME_RATE, 2), round(i / FRAME_RATE, 2)])
            start = None
    return out


def _sections(S, duration, window=3.0, step=0.25, min_gap=6.0):
    """Candidate section boundaries: moments where the sound of the next `window` seconds differs most from the last.
    Returns [(time, height)] sorted by time; height is the change measured against the song's typical (median) change,
    so steady noise scores about 1.3-1.5 everywhere and a real change of part 1.75 or more."""
    import numpy as np
    k = max(1, int(round(FRAME_RATE * step)))
    n = len(S) // k
    w = int(window / step)
    if n < 2 * w + 3:
        return []
    F = S[:n * k].reshape(n, k, -1).mean(1)
    F = (F - F.mean(0)) / (F.std(0) + 1e-6)
    cs = np.vstack([np.zeros((1, F.shape[1])), np.cumsum(F, 0)])
    nov = np.zeros(n)
    for i in range(w, n - w):
        nov[i] = np.linalg.norm((cs[i + w] - cs[i]) / w - (cs[i] - cs[i - w]) / w)
    thr, med = nov.mean() + 0.5 * nov.std(), float(np.median(nov[w:n - w]))
    picked = []
    if med <= 0:
        return []
    for i in np.argsort(-nov):
        if nov[i] < thr:
            break
        t = i * k / FRAME_RATE
        if 0 < i < n - 1 and nov[i] >= nov[i - 1] and nov[i] >= nov[i + 1] and window <= t <= duration - window and all(abs(t - p[0]) >= min_gap for p in picked):
            picked.append((t, float(nov[i] / med)))
    return sorted(picked)


def _envelope(rms, env):
    """Loudness and onset strength at ENV_STEP resolution, 0..255, for the page's strip."""
    import numpy as np
    k = max(1, int(round(FRAME_RATE * ENV_STEP)))
    n = len(rms) // k
    if not n:
        return {'step': ENV_STEP, 'level': [], 'onset': []}
    lv = rms[:n * k].reshape(n, k).max(1)
    db = np.clip((20 * np.log10(lv + 1e-7) + 60) / 60.0, 0, 1)  # -60 dB .. 0 dB
    on = env[:n * k].reshape(n, k).max(1)
    on = np.clip(on / (np.percentile(on, 99) + 1e-9), 0, 1)
    return {'step': round(k / FRAME_RATE, 5), 'level': [int(v) for v in np.round(db * 255)], 'onset': [int(v) for v in np.round(on * 255)]}


def whisper_available():
    try:
        return importlib.util.find_spec('faster_whisper') is not None
    except (ImportError, ValueError):
        return False


def _whisper_model():
    m = (os.environ.get('CUTVELVET_WHISPER_MODEL') or 'small').strip().lower()
    return m if m in WHISPER_MODELS else 'small'


def _lyrics_child(path):
    """Runs in its own low-priority process (see ``transcribe``): prints one JSON object on the last line."""
    try:
        os.nice(10)
    except OSError:
        pass
    from faster_whisper import WhisperModel
    name = _whisper_model()
    model = WhisperModel(name, device='cpu', compute_type='int8', cpu_threads=2)
    segs, info = model.transcribe(path, beam_size=1, word_timestamps=True, condition_on_previous_text=False)
    lines = []
    for s in segs:
        text = ' '.join((s.text or '').split())
        if not text:
            continue
        words = list(s.words or [])
        line = {'t0': round(float(s.start), 2), 't1': round(float(s.end), 2), 'text': text,
                'confidence': round(math.exp(min(0.0, float(s.avg_logprob))), 2)}
        if words:
            line['first_word_end'] = round(float(words[0].end), 2)
        lines.append(line)
    sys.stdout.write('\n' + json.dumps({'model': name, 'language': info.language,
                                        'language_confidence': round(float(info.language_probability), 2), 'lines': lines}) + '\n')


def transcribe(path):
    """Lyric lines via faster-whisper (optional). Raises NewCutError when it is not installed or fails."""
    if not whisper_available():
        raise NewCutError('faster_whisper is not installed (optional: pip install faster-whisper)')
    env = dict(os.environ, OMP_NUM_THREADS='2', PYTHONPATH=REPO + (os.pathsep + os.environ['PYTHONPATH'] if os.environ.get('PYTHONPATH') else ''))
    try:
        r = subprocess.run([sys.executable, '-m', 'cutvelvet.newcut', '_lyrics', path], capture_output=True, text=True, timeout=3600, env=env,
                           cwd=REPO)  # -m puts the working directory first on sys.path: never a project folder
    except (OSError, subprocess.SubprocessError) as e:
        raise NewCutError('lyric transcription did not finish: %s' % e)
    last = (r.stdout or '').strip().splitlines()[-1:] or ['']
    try:
        d = json.loads(last[0])
        if r.returncode != 0 or not isinstance(d.get('lines'), list):
            raise ValueError
        return d
    except (ValueError, AttributeError):
        raise NewCutError('lyric transcription failed: %s' % ((r.stderr or '').strip().splitlines() or ['no output'])[-1][:300])


def _nearest(sorted_times, t):
    if not sorted_times:
        return None
    return min(sorted_times, key=lambda b: abs(b - t))


def _finish_lyrics(d):
    """Add a suggested cut point to every lyric line and refresh the plain-language notes."""
    beats = d.get('beats') or []
    ly = d.get('lyrics')
    if isinstance(ly, dict):
        for ln in ly.get('lines') or []:
            # Whisper stretches the first word of a line back over the pause before it, so the END of that word
            # sits closer to where the line lands than the reported start does. Snap that to a beat when we have beats.
            t = ln.get('first_word_end', ln['t0'])
            b = _nearest(beats, t)
            ln['cut'] = round(b if b is not None and abs(b - t) <= (d.get('tempo') or {}).get('period', 0) else t, 3)
    d['missing'], d['notes'] = _summary(d)
    return d


def _summary(d):
    """(missing, notes): what the analysis does not contain, and how far to trust what it does."""
    missing, notes = ['bars / downbeats: not detected (beats are not grouped into bars)'], []
    tempo = d.get('tempo')
    if not tempo:
        missing.append('tempo and beats: no steady pulse found (nothing in the sound repeats strongly enough to call a beat)')
    else:
        lvl = tempo['level']
        notes.append('tempo %.1f bpm, confidence %s (%.2f); it may really be half or double that%s' % (
            tempo['bpm'], lvl, tempo['confidence'], ' (other candidates: %s bpm)' % ', '.join('%g' % a for a in tempo['alternatives']) if tempo.get('alternatives') else ''))
        if lvl == 'low':
            notes.append('LOW CONFIDENCE: the beat grid is a guess for this song; treat beats as hints and trust your ears / the sections more')
        notes.append('%d beats; onsets are %.1f times stronger on the beats than elsewhere (under 2.2 is weak evidence)' % (len(d.get('beats') or []), tempo.get('pulse') or 0))
    if not d.get('sections'):
        missing.append('sections: no clear changes in the sound were found')
    else:
        faint = sum(1 for x in d['sections'] if x.get('kind') == 'faint change')
        notes.append('%d section candidates (%d clear, %d faint): changes in the sound, coarse — a clear one is usually within a second of where a person '
                     'would mark a new line or part; faint ones are right about half the time, and some real boundaries are missed' % (len(d['sections']), len(d['sections']) - faint, faint))
    ly = d.get('lyrics')
    if isinstance(ly, dict) and ly.get('lines'):
        notes.append('%d lyric lines from speech recognition (%s model): the words are often wrong on sung material and two lines can run together; '
                     'starts tend to be early, "cut" is the suggested cut point for a line' % (len(ly['lines']), ly.get('model')))
    elif isinstance(ly, dict):
        missing.append('lyric lines: the recogniser found no words (instrumental, or too hard to hear)')
    elif d.get('lyrics_error'):
        missing.append('lyric lines: %s' % d['lyrics_error'])
    elif not whisper_available():
        missing.append('lyric lines: not available (the optional faster_whisper package is not installed)')
    else:
        missing.append('lyric lines: not requested (analyse again with lyrics switched on)')
    return missing, notes


def analyze(audio_path, project_dir=None, lyrics=False, force=False):
    """Markers for cutting to a song. Cached; a cached result without lyrics is completed when lyrics are asked for.

    {"song", "duration", "tempo": {"bpm", "period", "confidence", "level": high|medium|low, "pulse", "steadiness", "alternatives"} | null,
     "beats": [s, ...], "sections": [{"t", "raw", "strength", "kind"}], "silences": [[a, b]],
     "lyrics": {"model", "language", "lines": [{"t0", "t1", "text", "confidence", "cut"}]} | null,
     "missing": [...], "notes": [...]}

    ``sections[].t`` is snapped to the nearest beat when one is close (``raw`` is the unsnapped time).
    The display envelope is cached beside it as ``<hash>.env.json``."""
    audio_path = os.path.abspath(os.path.expanduser(audio_path))
    if not os.path.isfile(audio_path):
        raise NewCutError('no such file: %s' % audio_path)
    out = analysis_path(audio_path, project_dir)
    old = _read(out) or {}
    d = None if force or not old else old
    if d is None or d.get('version') != ANALYSIS_VERSION or not os.path.isfile(out[:-5] + '.env.json'):
        import numpy as np
        duration = probe_audio(audio_path)
        if not duration:
            raise NewCutError('%s is not readable audio' % os.path.basename(audio_path))
        t_start = time.time()
        y = _decode(audio_path)
        S, rms = _spectrum(y)
        env = _onsets(S)
        tempo, beats = None, []
        tp = _tempo(env) if len(env) > FRAME_RATE * 4 else None
        if tp:
            bpm, period, conf, alts = tp
            frames = _track(env, period)
            # The tracker returns an even grid for ANY sound, so its evenness proves nothing. What counts is whether
            # onsets really sit on that grid: onset strength at the beats against onset strength anywhere.
            # Measured on the sounding parts only, with the median over beats: a few loud entries after a pause
            # (speech, ambience) must not pass for a pulse.
            sil = _silences(rms)
            loud = lambda i: not any(a <= i / FRAME_RATE <= c for a, c in sil)
            on = [env[max(0, i - 1):i + 2].max() for i in frames if loud(i)]
            typical = [env[i:i + 3].max() for i in range(0, len(env) - 3, 3) if loud(i)]
            pulse = float(np.median(on) / (np.mean(typical) + 1e-9)) if on and typical else 0.0
            bt = [float(b) for b in frames / FRAME_RATE if not any(a <= b <= c for a, c in sil)]
            ibi = np.diff(bt) if len(bt) > 2 else np.array([period / FRAME_RATE])
            steady = float(max(0.0, 1 - ibi.std() / (np.median(ibi) + 1e-9)))
            level = 'high' if conf >= 0.5 and pulse >= 3.0 and steady >= 0.9 else 'low' if conf < 0.3 or pulse < 2.2 or steady < 0.8 else 'medium'
            if len(bt) >= 4 and pulse >= MIN_PULSE:
                tempo = {'bpm': round(float(bpm), 1), 'period': round(float(period) / FRAME_RATE, 4), 'confidence': round(conf, 2), 'level': level,
                         'pulse': round(pulse, 2), 'steadiness': round(steady, 2), 'alternatives': alts}
                beats = [round(b, 3) for b in bt]
        silences = _silences(rms)
        sections = []
        found = _sections(S, duration)
        for t, height in found if any(h >= CLEAR_CHANGE for _, h in found) else []:  # nothing clear: the rest is probably noise
            b = _nearest(beats, t)
            snapped = b if b is not None and tempo and abs(b - t) <= tempo['period'] else t
            sections.append({'t': round(float(snapped), 3), 'raw': round(float(t), 2), 'strength': round(min(1.0, max(0.15, (height - 1) / 1.5)), 2),
                             'kind': 'change' if height >= CLEAR_CHANGE else 'faint change'})
        for a, b in silences:
            if b - a >= 1.0:
                sections += [{'t': x, 'raw': x, 'strength': 1.0, 'kind': k} for x, k in ((a, 'silence starts'), (b, 'silence ends')) if 0.5 < x < duration - 0.5]
        sections.sort(key=lambda s: s['t'])
        proj = project_dir or find_project(audio_path)
        d = {'version': ANALYSIS_VERSION, 'song': L.rel_src(proj, audio_path) if proj else audio_path, 'duration': duration,
             'analysed_at': time.strftime('%Y-%m-%dT%H:%M:%S'), 'seconds_taken': round(time.time() - t_start, 2),
             'tempo': tempo, 'beats': beats, 'sections': sections, 'silences': silences,
             'lyrics': old.get('lyrics') if isinstance(old.get('lyrics'), dict) and not force else None}  # the slow part is kept
        _finish_lyrics(d)
        _write(out[:-5] + '.env.json', _envelope(rms, env))
        _write(out, d)
    if lyrics and not isinstance(d.get('lyrics'), dict):
        try:
            d['lyrics'] = transcribe(audio_path)
            d.pop('lyrics_error', None)
        except NewCutError as e:
            d['lyrics_error'] = str(e)
        _write(out, _finish_lyrics(d))
    d['missing'], d['notes'] = _summary(d)  # worded by this version of the tool, whatever the cache says
    return d


def envelope(audio_path, project_dir=None):
    return _read(analysis_path(audio_path, project_dir)[:-5] + '.env.json')


def cached_analysis(audio_path, project_dir=None):
    try:
        d = _read(analysis_path(audio_path, project_dir))
    except OSError:
        return None
    if not d or d.get('version') != ANALYSIS_VERSION:
        return None
    d['missing'], d['notes'] = _summary(d)
    return d


# ───────────────────────────────────────────────────────────── 3. the brief

def briefs_dir(project_dir):
    return os.path.join(_proj_dirs(project_dir)['timelines'], 'briefs')


def brief_path(project_dir, tid):
    return os.path.join(briefs_dir(project_dir), tid + '.brief.json')


def _numf(v, lo, hi, default=None):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) and lo <= v <= hi else default


SOUNDS = ('song', 'audio', 'none')   # a song / other audio (voice, ambience: same analysis) / a silent cut
SILENT_DEFAULT, SILENT_MAX = 30.0, 1800.0


def silent_analysis(brief):
    """Stands in for a song's analysis when the cut has no sound: only a duration, nothing to snap to."""
    return {'song': None, 'duration': min(brief.get('length') or SILENT_DEFAULT, SILENT_MAX), 'tempo': None, 'beats': [], 'sections': [],
            'silences': [], 'lyrics': None, 'missing': [], 'notes': []}


def clean_brief(d):
    """Whatever the page sent -> a well-formed brief (no judgement about whether it can run; see ``problems``)."""
    d = d if isinstance(d, dict) else {}
    foot = d.get('footage') if isinstance(d.get('footage'), dict) else {}
    st = d.get('structure') if isinstance(d.get('structure'), dict) else {}
    folders = foot.get('folders')
    shots = _numf(st.get('shots'), 1, 2000)
    sound = d.get('sound') if d.get('sound') in SOUNDS else 'song'
    silent = sound == 'none'
    return {
        'sound': sound,
        'id': str(d.get('id') or '').strip()[:60],
        'title': ' '.join(str(d.get('title') or '').split())[:200],
        'song': '' if silent else str(d.get('song') or '').strip()[:500],
        'offset': 0.0 if silent else round(_numf(d.get('offset'), 0, 86400, 0.0), 3),
        'length': (lambda v: round(v, 3) if v else (SILENT_DEFAULT if silent else None))(_numf(d.get('length'), 0.001, 86400)),
        'fadeout': round(_numf(d.get('fadeout'), 0, 60, 4.0), 2),
        'footage': {'folders': [str(x) for x in folders if isinstance(x, str)][:200] if isinstance(folders, list) else None,
                    'exclude': [str(x) for x in (foot.get('exclude') or []) if isinstance(x, str)][:5000] if isinstance(foot.get('exclude'), list) else []},
        'intent': str(d.get('intent') or '').strip()[:8000],
        'outline': str(d.get('outline') or '').strip()[:12000],
        'structure': {'snap': 'free' if silent else st.get('snap') if st.get('snap') in SNAPS else 'sections' if sound == 'audio' else 'beats',
                      'avg_shot': round(_numf(st.get('avg_shot'), 0.3, 120, DEFAULT_AVG_SHOT), 2),
                      'shots': int(shots) if shots else None},
        'lyrics': bool(d.get('lyrics')) and not silent,
    }


def save_brief(project_dir, brief):
    brief = clean_brief(brief)
    if not ID_RE.match(brief['id']):
        raise NewCutError('the timeline id must be letters and digits only (for example "ch3")')
    brief['saved_at'] = time.strftime('%Y-%m-%dT%H:%M:%S')
    L.write_json(brief_path(project_dir, brief['id']), brief)
    return brief


def load_brief(project_dir, tid):
    if not ID_RE.match(tid or ''):
        return None
    d = _read(brief_path(project_dir, tid))
    return dict(clean_brief(d), saved_at=d.get('saved_at')) if d else None


def list_briefs(project_dir):
    try:
        return sorted(n[:-len('.brief.json')] for n in os.listdir(briefs_dir(project_dir)) if n.endswith('.brief.json'))
    except OSError:
        return []


# ───────────────────────────────────────────────────────────── footage

def library_index(project_dir, lib=None):
    """The clip index: the running server's Library when given, else ``.cutvelvet/library.json`` as last scanned."""
    if lib is not None:
        return lib.snapshot()
    d = _read(os.path.join(project_dir, '.cutvelvet', 'library.json')) or {}
    return {'folders': d.get('folders') or [], 'clips': d.get('clips') or [], 'status': {'scanning': False}, 'scanned_at': d.get('scanned_at')}


def usable(c):
    return isinstance(c, dict) and not c.get('error') and (c.get('duration') or 0) > 0 and c.get('src')


def select_clips(index, footage):
    """The clips a brief may draw from: usable clips in the chosen folders (default all), minus excluded ones."""
    footage = footage or {}
    folders, exclude = footage.get('folders'), set(footage.get('exclude') or [])
    return [c for c in index.get('clips') or [] if usable(c) and (folders is None or c.get('folder') in folders) and c['src'] not in exclude]


def existing_timelines(project_dir):
    try:
        return sorted(n[:-5] for n in os.listdir(_proj_dirs(project_dir)['timelines']) if n.endswith('.json'))
    except OSError:
        return []


def id_taken(project_dir, tid):
    """Why this id cannot be used for a new cut, or ''."""
    d = _proj_dirs(project_dir)
    if os.path.exists(os.path.join(d['timelines'], tid + '.json')):
        return 'a timeline called "%s" already exists — choose another id (New cut never overwrites a cut)' % tid
    pat = re.compile(r'^%s_v\d+\.(mp4|json)$' % re.escape(tid))
    for folder in (d['review'], os.path.join(d['timelines'], '.history')):
        try:
            if any(pat.match(n) for n in os.listdir(folder)):
                return 'published versions of "%s" already exist in %s — choose another id' % (tid, L.rel_src(project_dir, folder))
        except OSError:
            pass
    return ''


def suggest_id(project_dir):
    """The next free id in the project's own naming pattern ("ch1", "ch2" -> "ch3"), or '' when the existing
    timelines follow no letters+number pattern (the page then derives one from the song's name)."""
    groups = {}
    for t in existing_timelines(project_dir):
        m = re.match(r'^([A-Za-z]+)(\d+)$', t)
        if m:
            groups.setdefault(m.group(1), []).append(int(m.group(2)))
    if not groups:
        return ''
    prefix = max(groups, key=lambda k: (len(groups[k]), max(groups[k])))
    n = max(groups[prefix]) + 1
    while id_taken(project_dir, '%s%d' % (prefix, n)):
        n += 1
    return '%s%d' % (prefix, n)


# ───────────────────────────────────────────────────────────── markers for one cut

def window(brief, song_duration):
    """(offset, length) of the song the cut uses, clamped to the song."""
    off = min(max(0.0, brief.get('offset') or 0.0), max(0.0, song_duration))
    length = brief.get('length') or (song_duration - off)
    return round(off, 3), round(max(0.0, min(length, song_duration - off)), 3)


def suggest_cuts(primary, beats, duration, target, min_len=None):
    """A first guess at cut times (timeline seconds, strictly inside 0..duration): walk the chosen markers aiming
    at `target` seconds a shot, then split anything much longer on a beat."""
    min_len = min_len if min_len is not None else max(0.4, target * 0.45)
    cands = sorted(t for t in primary if min_len <= t <= duration - min_len)
    cuts, t = [], 0.0
    while cands:
        nxt = [c for c in cands if c - t >= min_len]
        if not nxt:
            break
        c = min(nxt, key=lambda c: abs(c - (t + target)))
        if c - t > 1.6 * target and not primary:
            break
        cuts.append(c)
        t = c
        cands = [x for x in cands if x > c]
    out, edges = [], [0.0] + cuts + [duration]
    for a, b in zip(edges[:-1], edges[1:]):  # split long stretches on beats (or evenly when there are none)
        n = int(round((b - a) / target))
        if b - a > 1.6 * target and n >= 2:
            for i in range(1, n):
                want = a + (b - a) * i / n
                near = [x for x in beats if a + min_len <= x <= b - min_len and all(abs(x - o) >= min_len for o in out[-1:])]
                x = min(near, key=lambda x: abs(x - want)) if near else want
                if all(abs(x - o) >= min_len for o in out) and a + min_len <= x <= b - min_len:
                    out.append(round(x, 3))
        if b < duration:
            out.append(round(b, 3))
    return sorted(set(out))


def cut_markers(analysis, brief):
    """The analysis as the agent should use it for this cut: timeline seconds (song time minus the offset),
    trimmed to the window, with the cut points the brief asked for."""
    off, length = window(brief, analysis['duration'])
    inside = lambda t: 0 < t - off < length
    beats = [round(b - off, 3) for b in analysis.get('beats') or [] if inside(b)]
    sections = [dict(s, t=round(s['t'] - off, 3), raw=round(s['raw'] - off, 2)) for s in analysis.get('sections') or [] if inside(s['t'])]
    lines = []
    for ln in ((analysis.get('lyrics') or {}).get('lines') or []):
        if ln['t1'] > off and ln['t0'] < off + length:
            lines.append({'t0': round(max(0.0, ln['t0'] - off), 2), 't1': round(min(length, ln['t1'] - off), 2),
                          'cut': round(ln.get('cut', ln['t0']) - off, 3), 'text': ln['text'], 'confidence': ln.get('confidence')})
    st = brief['structure']
    snap, notes = st['snap'], []
    if snap == 'lines' and not lines:
        snap = 'sections' if sections else 'beats'
        notes.append('The brief asked for cuts on lyric lines but there are none for this song; using %s instead.' % snap)
    if snap == 'sections' and not sections and beats:
        snap = 'beats'
        notes.append('No sections were found in this part of the song; using beats instead.')
    elif snap == 'sections' and not sections:
        snap = 'free'
        notes.append('Neither sections nor a steady beat were found in this audio; cut times are free.')
    if snap == 'beats' and not beats:
        snap = 'sections' if sections else 'free'
        notes.append('No steady beat was found in this part of the audio; %s.' % ('using its section changes instead' if sections else 'cut times are free'))
    primary = {'beats': beats, 'sections': [s['t'] for s in sections], 'lines': [l['cut'] for l in lines if 0 < l['cut'] < length], 'free': []}[snap]
    target = (length / st['shots']) if st.get('shots') else st['avg_shot']
    target = max(0.3, min(target, length))
    if snap == 'free':
        n = max(1, int(round(length / target)))
        cuts = [round(length * i / n, 3) for i in range(1, n)]
    else:
        cuts = suggest_cuts(primary, beats, length, target)
    return {
        'timeline': brief['id'], 'song': analysis.get('song'), 'song_offset': off, 'duration': length,
        'time_base': 'timeline seconds: 0 is the first frame of the cut (song time minus song_offset)',
        'tempo': analysis.get('tempo'), 'snap': snap, 'target_shot_seconds': round(target, 2),
        'cut_candidates': primary, 'suggested_cuts': cuts,
        'beats': beats, 'sections': sections, 'lyric_lines': lines,
        'missing': analysis.get('missing') or [], 'notes': notes + (analysis.get('notes') or []), 'changed': notes,
    }


def audio_track(brief, analysis):
    """The timeline's ``audio`` object for this brief (None for a silent cut)."""
    if brief.get('sound') == 'none':
        return None
    off, length = window(brief, analysis['duration'])
    a = {'src': brief['song'], 'start': 0}
    if off > 0:
        a['ss'] = off
    if off + length < analysis['duration'] - 0.05:
        a['dur'] = length
    fade = min(brief.get('fadeout') or 0, length / 2.0)
    if fade > 0:
        a['fadeout'] = round(fade, 2)
    return a


# ───────────────────────────────────────────────────────────── working files for the agent

def work_dir(project_dir, tid):
    return os.path.join(project_dir, '.cutvelvet', 'newcut', tid)


def _about(c):
    m = c.get('meta') or {}
    for k in ('q', 'title', 'description', 'desc', 'caption', 'tags'):
        if isinstance(m.get(k), str) and m[k].strip():
            return ' '.join(m[k].split())[:160]
    return ''


def plan_sheets(clips):
    """Group clips into contact sheets by the height their sprite rows need. Returns [[clip, ...], ...]."""
    sheets, cur, h = [], [], 0
    for c in clips:
        sp = c.get('sprite') or {}
        row = LABEL_H + int(round((sp.get('h') or 90) * SHEET_W / float((sp.get('w') or L.SPRITE_W) * (sp.get('frames') or L.SPRITE_FRAMES))))
        if cur and h + row > SHEET_MAX_H:
            sheets.append(cur)
            cur, h = [], 0
        cur.append(c)
        h += row
    if cur:
        sheets.append(cur)
    return sheets


def make_sheets(project_dir, groups, out_dir):
    """Paste the library's scrub sprites into labelled contact sheets. Returns {src: (sheet file name, row number)};
    empty when pillow or the sprites are missing (the prompt then points at the single sprites instead)."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return {}
    try:
        font = ImageFont.load_default(size=13)
    except TypeError:  # older pillow: fixed-size bitmap font
        font = ImageFont.load_default()
    thumbs, where, n = os.path.join(project_dir, '.cutvelvet', 'thumbs'), {}, 0
    for old in os.listdir(out_dir) if os.path.isdir(out_dir) else []:
        if re.match(r'^sheet_\d+\.jpg$', old):
            os.remove(os.path.join(out_dir, old))
    for group in groups:
        rows = []
        for c in group:
            try:
                im = Image.open(os.path.join(thumbs, c['key'] + '_s.jpg')).convert('RGB')
            except (OSError, KeyError, ValueError):
                continue
            if im.width != SHEET_W:
                im = im.resize((SHEET_W, max(2, int(round(im.height * SHEET_W / float(im.width))))))
            rows.append((c, im))
        if not rows:
            continue
        n += 1
        name = 'sheet_%02d.jpg' % n
        sheet = Image.new('RGB', (SHEET_W, sum(LABEL_H + im.height for _, im in rows)), (12, 12, 13))
        draw, y = ImageDraw.Draw(sheet), 0
        for r, (c, im) in enumerate(rows, 1):
            times = (c.get('sprite') or {}).get('times') or []
            label = '%d  %s   %.1fs  %dx%d   frames at: %s' % (r, c['src'], c.get('duration') or 0, c.get('width') or 0, c.get('height') or 0,
                                                               ' '.join('%g' % round(t, 1) for t in times))
            draw.text((4, y + 2), label, fill=(236, 231, 220), font=font)
            sheet.paste(im, (0, y + LABEL_H))
            where[c['src']] = (name, r)
            y += LABEL_H + im.height
        os.makedirs(out_dir, exist_ok=True)
        sheet.save(os.path.join(out_dir, name), quality=82)
    return where


def footage_digest(project_dir, clips, where):
    used = L.used_ranges(project_dir)
    thumbs = os.path.join(project_dir, '.cutvelvet', 'thumbs')
    out = []
    for c in clips:
        e = {'src': c['src'], 'duration': c.get('duration'), 'size': [c.get('width'), c.get('height')], 'fps': c.get('fps')}
        if _about(c):
            e['about'] = _about(c)
        if c['src'] in where:
            e['sheet'], e['row'] = where[c['src']]
        elif c.get('key') and os.path.isfile(os.path.join(thumbs, c['key'] + '_s.jpg')):
            e['sprite'] = os.path.join(thumbs, c['key'] + '_s.jpg')
        e['frames_at'] = (c.get('sprite') or {}).get('times') or []
        u = used.get(c['src']) or []
        if u:
            e['used'] = [[r['a'], r['b'], '%s/%s' % (r.get('timeline'), r.get('shot'))] for r in u]
        out.append(e)
    return out


# ───────────────────────────────────────────────────────────── 4. the prompt

PROMPT = """\
You are making the FIRST CUT of a new timeline with the cutvelvet tool: a short film cut from existing
footage{to_what}. Nobody has edited this timeline before; you build it from the brief below, check your
own frames, and publish version 1 for the editor to review. You work alone: nobody can answer questions
during this run, so make reasonable choices and write them down at the end.

Project folder (your working directory): {project}
Timeline to create: {timeline_path}   (timeline id: {timeline})

Read these first, in full:
- {agents_md} — how cutvelvet projects, timelines, the command line and crop placement work.
- The output of `cutvelvet schema` — the complete timeline file format.

## The brief (the editor's own words; also saved at {brief_path})

Title: {title}

What this cut is, what it should feel like, what must or must not appear:
{intent}

Shot-by-shot outline from the editor:
{outline}

## Rules

{rules_text}
Where a rule and the brief disagree, the rule wins; say so in your summary.
The tool has no house style. Layout, number of panels, grading, transitions and any text on screen are
decided by the brief and the rules. Where they are silent, choose something plain that serves the brief
and say what you chose. Do not copy the look of other timelines in this project unless the brief asks.

{sound_text}
## The footage you may use

Clip list: {footage_path}   ({clips} clips, {clip_minutes} of footage)
  Each entry: `src` (use exactly this in the timeline), `duration`, `size`, `about` (a description, when
  the library has one), `frames_at` (the source seconds of the ten sprite frames), `used` (source seconds
  other timelines already use: [from, to, timeline/shot] — it matters if a rule forbids reusing footage).
{sheets_text}  The full library index is {library_index}; every clip's own poster and ten-frame sprite are
  {thumbs_dir}/<key>_p.jpg and <key>_s.jpg (`key` from the index). You should not need them: the sheets
  hold the same sprites.
You MUST look at the footage before choosing clips; do not pick a clip from its file name or description
alone. Use only clips from the clip list. If something the brief needs is not there, use the closest
thing or leave it out, and say so in your summary. Do not download anything.

## What to write

{timeline_path} in the documented schema, starting from exactly this head:

{header_json}

- `shots`: in order, contiguous, the first starts at 0 and the last ends at {duration}. Shot ids "S1",
  "S2", …; `label` is a few words saying what the shot is.
- Every panel needs `src`, `ss`, `t0`, `t1`, `rect`, `region`; panel times lie inside their shot. Keep
  `ss + (t1 - t0) * speed` inside the clip's `duration`.
- {audio_rule} Add no on-screen text unless the brief or outline asks for it.

## How to work (in this order)

1. Read the two documents, the markers file and the clip list.
2. Look at the footage ({image_plan}). Note for each useful clip which seconds show what.
3. Decide the shot list: cut times from the markers, then a clip and a moment for every shot, following
   the outline where there is one. Write the whole timeline file now, with your best-guess `region`s —
   a complete draft on disk is worth more than a perfect half.
4. Go through it shot by shot and place each crop with the grid-frame method from the guide: extract the
   exact source frame you chose with a coordinate grid, for example
   `cutvelvet grid {project} <src> <ss>[,<ss>…]` (it writes the gridded frames to .cutvelvet/grid/ and prints
   their paths; several moments of one clip in one call), look at it, read off where the subject is, and set `region`. Check a second frame near the end of the
   panel's source range when the subject moves.
5. Render stills and LOOK at them: `cutvelvet stills {project} {timeline} <t1,t2,…> --scale 0.5`
   (one time inside every shot; several times in one command). Fix what is wrong — a strip on a forehead,
   closed eyes, a frame too dark to read — and re-check only the shots you changed.
6. `cutvelvet audit {project} {timeline}` and fix every error it reports.
7. `cutvelvet publish {project} {timeline} --scale 0.5`. It prints the version name (normally {timeline}_v1)
   and puts the video in the project's review folder (the "wrote …/renders/…" line it also prints is only its working copy).
8. Write {changes_path_hint} (use the version name publish printed):
   - one line per shot: id, time range, what it shows and why it is there;
   - "Weak spots": the shots you are least sure of, and why;
   - "Footage I wished I had": what the brief needed that the library does not hold;
   - anything in the brief or rules you could not follow.
   Plain language, no preamble. Do not claim a shot is checked if you did not look at a still of it.

## Economy

This is the heaviest job the tool runs and the editor pays for every image you look at.
- Look at each contact sheet once. Extract single frames only for clips you have decided to use, about
  one or two per panel. Never open a video file and never re-read an image you have already seen.
- Batch stills: many times in one `cutvelvet stills` command, then read only those files.
- Do not render the timeline yourself; `cutvelvet publish` renders it once at the end.

## Limits

- `cutvelvet` is on PATH for this run; the shell only allows `cutvelvet …` (no ffmpeg or ffprobe: use
  `cutvelvet grid` for source frames and `cutvelvet probe {project} <src>` for a clip's length and size), one
  plain command per call (no pipes, `&&`, `cd` or redirection). `cutvelvet grid` and `cutvelvet stills` put
  their jpgs under .cutvelvet/ by themselves and print the paths; any other scratch file goes in {work_dir}/.
- Create {timeline_path} and the summary file; change nothing else. Do not touch other timelines
  ({other_timelines}), review/, existing feedback files, the footage, the music or project.json.
- If you cannot finish (audit errors you cannot fix, publish fails), leave the timeline file in the best
  valid state you can and still write the summary to {feedback_dir}/{timeline}_v1_changes.md saying
  exactly what is missing.
"""

SOUND_TEXT = """\
## The {what} and where cuts go

Audio: {song}, starting {offset} s into the file; the cut is {duration} s long.{not_music}
Markers file: {markers_path}
  All times in it are TIMELINE seconds (0 = first frame of the cut). It holds `beats`, `sections`,
  `lyric_lines` (may be empty), `cut_candidates` (the kind of marker the editor chose: {snap_text}) and
  `suggested_cuts` (a first guess at about {shots} shots, {target} s each on average).
{markers_notes}
Cut placement: {snap_rule}
The number and average length of shots are a guide, not a quota: follow the outline and the {what} first.
"""

SILENT_TEXT = """\
## No sound

This cut is SILENT: there is no audio at all. The timeline has no `audio` key; do not add one. It is
{duration} s long.
Markers file: {markers_path}
  It holds only `duration` and `suggested_cuts`: an even split into about {shots} shots of {target} s, as a
  starting point. There are no beats or sections to snap to (`cut_candidates` is empty).

Cut placement: with nothing to hear, the rhythm comes from the pictures. Pace the cut by the shot length the
editor asked for (about {target} s) and by the footage itself: cut on a movement, a look, the end of a
gesture; let a shot run longer when something is happening in it and shorter when nothing is.
The number and average length of shots are a guide, not a quota: follow the outline first.
"""

SNAP_TEXT = {'beats': 'beats', 'lines': 'the starts of lyric lines', 'sections': 'section changes', 'free': 'none — cut times are free'}


def prompt_template(project_dir):
    """PROMPT, or the project's own ``.cutvelvet/newcut_prompt.md`` -- but only once the user has approved exactly
    that text on this machine (it steers the agent, and a project folder can come from anyone)."""
    own = SETTINGS.project_prompt(project_dir, 'newcut_prompt.md')
    return own['text'] if own and own['approved'] else PROMPT


def rules_text(project_dir):
    """Only the enabled rules, as text (never creates rules.json)."""
    if not any(r['on'] for r in RULES.load(project_dir, write=False)):
        return 'This project has no standing rules: the brief alone decides the look.\n'
    return RULES.as_text(project_dir, write=False)


def _mmss(s):
    s = int(round(s))
    return '%d min %02d s' % (s // 60, s % 60) if s >= 60 else '%d s' % s


def _indent(text, empty):
    text = (text or '').strip()
    return '\n'.join('    ' + ln for ln in text.splitlines()) if text else '    ' + empty


def prepare(project_dir, brief, lib=None, write=True):
    """Everything a run needs, from a clean brief: analysis, markers, clip list, contact sheets, prompt.

    With ``write=False`` nothing is created except the (cached) song analysis: sheets are only planned.
    Returns {"prompt", "markers", "clips", "sheets", "shots", "work", ...}. Raises NewCutError."""
    tid = brief['id']
    silent = brief.get('sound') == 'none'
    if silent:
        an = silent_analysis(brief)
    else:
        spath = song_path(project_dir, brief['song'])
        if not spath or not os.path.isfile(spath):
            raise NewCutError('song not found: %s' % (brief['song'] or '(none chosen)'))
        an = analyze(spath, project_dir, lyrics=brief.get('lyrics') and whisper_available())
    markers = cut_markers(an, brief)
    clips = select_clips(library_index(project_dir, lib), brief['footage'])
    wd = work_dir(project_dir, tid)
    groups = plan_sheets([c for c in clips if c.get('key') and os.path.isfile(os.path.join(project_dir, '.cutvelvet', 'thumbs', c['key'] + '_s.jpg'))])
    if write:
        os.makedirs(wd, exist_ok=True)
        where = make_sheets(project_dir, groups, wd)
        _write(os.path.join(wd, 'markers.json'), {k: v for k, v in markers.items() if k != 'changed' and
                                                  (not silent or k in ('timeline', 'duration', 'target_shot_seconds', 'cut_candidates', 'suggested_cuts'))})
        _write(os.path.join(wd, 'footage.json'), {'clips': footage_digest(project_dir, clips, where)})
        sheets = sorted({v[0] for v in where.values()})
    else:
        sheets = ['sheet_%02d.jpg' % (i + 1) for i in range(len(groups))]
    loose = len(clips) - sum(len(g) for g in groups) if sheets else len(clips)
    d = _proj_dirs(project_dir)
    tpath = os.path.join(d['timelines'], tid + '.json')
    if sheets:
        sheets_text = ('Contact sheets: %s/%s … %s (%d images)\n  Each row is one clip: a label (row number, src, duration, size, frame times) above ten frames\n'
                       '  spread evenly through the clip. `sheet` and `row` in the clip list say where a clip is.\n' % (wd, sheets[0], sheets[-1], len(sheets)))
        if loose:
            sheets_text += '  %d clips are not on a sheet; their entry has `sprite`, the path of their own ten-frame strip.\n' % loose
        image_plan = 'all %d contact sheets, each once' % len(sheets)
    else:
        sheets_text = ('Each clip-list entry has `sprite`: the path of a ten-frame strip of that clip (thumbnails live in %s). Posters are the\n'
                       '  same name ending _p.jpg.\n' % os.path.join(project_dir, '.cutvelvet', 'thumbs'))
        image_plan = 'the sprite of every clip that could plausibly serve the brief, each once'
    snap = markers['snap']
    snap_rule = {
        'beats': 'every cut lands on a time from `beats`. `suggested_cuts` is a reasonable starting set; move a cut to another beat when the picture wants it.',
        'lines': 'every cut lands on a lyric line\'s `cut` time (`cut_candidates`); where a shot would be far too long, split it on a beat.',
        'sections': 'every cut lands on a section change (`cut_candidates`); where a shot would be far too long, split it on a beat.',
        'free': 'cut times are yours to choose; `suggested_cuts` is an even split to start from.',
    }[snap]
    others = [t for t in existing_timelines(project_dir) if t != tid]
    head = {'id': tid, 'title': brief['title'] or tid, 'duration': markers['duration'], 'audio': audio_track(brief, an), 'shots': []}
    if silent:
        del head['audio']
    vals = {
        'project': project_dir, 'timeline': tid, 'timeline_path': tpath, 'title': brief['title'] or tid,
        'agents_md': os.path.join(AGENTS.GUIDE, 'AGENTS.md'), 'cutvelvet': AGENTS.GUIDE,
        'brief_path': brief_path(project_dir, tid),
        'intent': _indent(brief['intent'], '(nothing written)'), 'outline': _indent(brief['outline'], '(none — the structure is yours to propose)'),
        'rules_text': rules_text(project_dir),
        'song': brief['song'], 'offset': '%g' % markers['song_offset'], 'duration': '%g' % markers['duration'],
        'markers_path': os.path.join(wd, 'markers.json'), 'snap_text': SNAP_TEXT[snap], 'snap_rule': snap_rule,
        'shots': str(len(markers['suggested_cuts']) + 1), 'target': '%g' % markers['target_shot_seconds'],
        'markers_notes': ''.join('  Note: %s\n' % n for n in markers['notes'] + ['not in the analysis — ' + m for m in markers['missing']]),
        'footage_path': os.path.join(wd, 'footage.json'), 'clips': str(len(clips)),
        'clip_minutes': _mmss(sum(c.get('duration') or 0 for c in clips)),
        'sheets_text': sheets_text, 'image_plan': image_plan, 'library_index': os.path.join(project_dir, '.cutvelvet', 'library.json'),
        'thumbs_dir': os.path.join(project_dir, '.cutvelvet', 'thumbs'),
        'header_json': '\n'.join('    ' + ln for ln in json.dumps(head, indent=1, ensure_ascii=False).splitlines()),
        'work_dir': wd, 'feedback_dir': d['feedback'],
        'changes_path_hint': os.path.join(d['feedback'], '<version>_changes.md'),
        'other_timelines': ', '.join(others) or 'there are none yet',
    }
    other = brief.get('sound') == 'audio'
    vals.update(what='audio' if other else 'song', to_what=', with no sound' if silent else ' to a piece of audio' if other else ' to a song',
                not_music='\n  It is not necessarily music (a voice, a reading, ambience): do not expect a beat unless the markers have one.' if other else '',
                audio_rule='Write no `audio` key: the cut is silent.' if silent else 'Keep the `audio` object as given.')
    vals['sound_text'] = re.sub(r'\{(\w+)\}', lambda m: vals.get(m.group(1), m.group(0)), SILENT_TEXT if silent else SOUND_TEXT)
    tpl = prompt_template(project_dir)
    prompt = re.sub(r'\{(\w+)\}', lambda m: vals[m.group(1)] if m.group(1) in vals else m.group(0), tpl)
    return {'prompt': prompt, 'markers': markers, 'analysis': an, 'clips': clips, 'sheets': sheets, 'loose': loose,
            'shots': len(markers['suggested_cuts']) + 1, 'work': wd, 'timeline_path': tpath}


# ───────────────────────────────────────────────────────────── refusals + estimate

RULE_SNAP = {'beat': 'beats', 'bar': 'beats', 'line': 'lines', 'phrase': 'sections'}   # bars are not detected: beats are the nearest marker


def cut_rule(project_dir):
    """The project's "Cuts land on the music's … boundaries" rule when it is switched on: {"unit", "snap", "text"}
    (``snap`` is the marker the form should default to, None for a unit typed by hand), else None."""
    for r in RULES.load(project_dir, write=False):
        if r.get('id') == 'cut-on-music' and r.get('on'):
            unit = str(((r.get('params') or {}).get('unit') or {}).get('value') or '').strip().lower()
            return {'unit': unit, 'snap': RULE_SNAP.get(unit), 'text': r.get('text')}
    return None


def _runner(app):
    return getattr(app, 'runner', None) or getattr(app, 'run', None)


def problems(project_dir, brief, lib=None, runner=None):
    """Every reason this brief cannot be built right now, as plain sentences ([] = it can start)."""
    out = []
    tid = brief['id']
    if not tid:
        out.append('Give the cut an id (letters and digits, for example "ch3").')
    elif not ID_RE.match(tid):
        out.append('The timeline id must be letters and digits only, at most 40 (got "%s").' % tid)
    else:
        taken = id_taken(project_dir, tid)
        if taken:
            out.append(taken[0].upper() + taken[1:] + '.')
    spath = song_path(project_dir, brief['song'])
    duration = None
    if brief.get('sound') == 'none':
        if (brief['length'] or 0) < MIN_LENGTH:
            out.append('A cut needs to be at least %g s long.' % MIN_LENGTH)
        elif brief['length'] > SILENT_MAX:
            out.append('A silent cut can be at most %d minutes long.' % (SILENT_MAX // 60))
    elif not brief['song']:
        out.append('Choose a song.')
    elif not spath or not os.path.isfile(spath):
        out.append('The song is missing: %s (it has to be an audio file in the project\'s music folder).' % brief['song'])
    else:
        duration = probe_audio(spath)
        if not duration:
            out.append('The song cannot be read as audio: %s.' % brief['song'])
        with _jobs_lock:
            if (_jobs.get((project_dir, spath)) or {}).get('state') == 'running':
                out.append('The song is still being analysed — wait for that to finish.')
    if duration:
        off, length = window(brief, duration)
        if brief['offset'] >= duration:
            out.append('The start offset (%g s) is past the end of the song (%g s).' % (brief['offset'], duration))
        elif length < MIN_LENGTH:
            out.append('The cut would be %.1f s long; it needs at least %g s of song.' % (length, MIN_LENGTH))
    index = library_index(project_dir, lib)
    clips = select_clips(index, brief['footage'])
    if not clips:
        total = sum(1 for c in index.get('clips') or [] if usable(c))
        if (index.get('status') or {}).get('scanning') and not total:
            out.append('The library is still being indexed — wait for it to finish.')
        elif not total:
            out.append('No footage is indexed: add a footage folder in the Library tab first.')
        else:
            out.append('The footage selection is empty: tick at least one folder (the library has %d clips).' % total)
    if not brief['intent'] and not brief['outline']:
        out.append('The brief is empty: write a line or two about what this cut is, or an outline.')
    if runner is not None:
        if runner.agent_error():   # nothing chosen, or the chosen agent is not installed: see Settings > Agent
            out.append(runner.agent_error())
        if runner.status().get('state') == 'running':
            out.append('Another job is running — wait for it to finish or cancel it.')
    else:
        out.append('No agent runner is available in this server.')
    return out


def history(runner):
    try:
        rows = runner.job_history('newcut') if runner is not None and hasattr(runner, 'job_history') else []
    except Exception:
        rows = []
    return [r for r in rows if isinstance(r, dict)]


def estimate(project_dir, brief, lib=None, runner=None):
    """What "Build first cut" would involve. Honest: no cost figure exists until a run has been recorded."""
    probs = problems(project_dir, brief, lib, runner)
    index = library_index(project_dir, lib)
    clips = select_clips(index, brief['footage'])
    out = {'ok': not probs, 'problems': probs, 'clips': len(clips), 'clip_seconds': round(sum(c.get('duration') or 0 for c in clips), 1),
           'sheets': len(plan_sheets([c for c in clips if c.get('key')])), 'shots': None, 'duration': None, 'snap': None, 'notes': [],
           'agent': os.path.basename((runner.command() or [''])[0]) if runner is not None else None}
    spath = song_path(project_dir, brief['song'])
    an = silent_analysis(brief) if brief.get('sound') == 'none' else cached_analysis(spath, project_dir) if spath and os.path.isfile(spath) else None
    out['sound'] = brief.get('sound')
    if an:
        m = cut_markers(an, brief)
        out.update(shots=len(m['suggested_cuts']) + 1, duration=m['duration'], snap=m['snap'], notes=m['changed'], cuts=m['suggested_cuts'])
        out['images'] = out['sheets'] + 3 * out['shots']  # sheets + roughly a grid frame or two and a still per shot
        pf = (SETTINGS.timings(project_dir).get('per_frame') or {}).get('draft')
        fps = L.load_project(project_dir).get('fps') or 24
        if pf:
            out['render_minutes'] = round(pf * m['duration'] * fps / max(1, SETTINGS.performance()['jobs']) / 60.0, 1)
    done = [r for r in history(runner) if r.get('state') == 'done' and r.get('seconds')]
    costs = [r['cost'] for r in done if isinstance(r.get('cost'), (int, float))]
    out['past'] = {'runs': len(done), 'minutes': [round(r['seconds'] / 60.0, 1) for r in done[-5:]], 'costs': [round(c, 2) for c in costs[-5:]],
                   'last': done[-1] if done else None}
    return out


def start(project_dir, brief, lib, runner):
    """Save the brief, prepare the working files and start the agent. Returns (ok, error, info)."""
    brief = clean_brief(brief)
    probs = problems(project_dir, brief, lib, runner)
    if probs:
        return False, ' '.join(probs), {'problems': probs}
    brief = save_brief(project_dir, brief)
    try:
        prep = prepare(project_dir, brief, lib, write=True)
    except NewCutError as e:
        return False, str(e), {'problems': [str(e)]}
    with open(os.path.join(prep['work'], 'prompt.md'), 'w', encoding='utf-8') as f:
        f.write(prep['prompt'])
    ok, err = runner.start_job('newcut', prep['prompt'], brief['id'])
    return ok, err or '', {'problems': [err] if err else [], 'shots': prep['shots'], 'clips': len(prep['clips']), 'sheets': len(prep['sheets']),
                           'prompt': L.rel_src(project_dir, os.path.join(prep['work'], 'prompt.md'))}


# ───────────────────────────────────────────────────────────── adding a song

_pick_lock = threading.Lock()
PICK_PROMPT = 'Choose a song for the new cut'
_TK_PICK = ('import tkinter as tk, tkinter.filedialog as fd, sys\n'
            'r = tk.Tk(); r.withdraw(); r.attributes("-topmost", True)\n'
            'sys.stdout.write(fd.askopenfilename(title=sys.argv[1]) or "")\n')
NO_CHOOSER = 'No file chooser is available here — use "Upload a file" instead.'


def pick_file():
    """Open the operating system's file chooser. Returns (path or None when cancelled, error or None)."""
    try:
        if sys.platform == 'darwin':
            r = subprocess.run(['osascript', '-e', 'activate', '-e', 'POSIX path of (choose file with prompt "%s" of type {"public.audio"})' % PICK_PROMPT],
                               capture_output=True, text=True, timeout=600)
            if r.returncode != 0:
                return None, None if ('-128' in r.stderr or r.returncode < 0) else (r.stderr.strip() or 'the file chooser failed')
            return r.stdout.strip() or None, None
        r = subprocess.run([sys.executable, '-c', _TK_PICK, PICK_PROMPT], capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            return None, NO_CHOOSER
        return r.stdout.strip() or None, None
    except subprocess.TimeoutExpired:
        return None, None
    except OSError:
        return None, NO_CHOOSER


def clean_name(name):
    name = os.path.basename((name or '').replace('\\', '/'))
    return re.sub(r'[^A-Za-z0-9._ ()+-]', '_', name).strip(' .')[:150]


def _free_name(folder, name, size):
    """(path, already there?) -- a name in `folder` that is free, or the existing file when it has the same size."""
    stem, ext = os.path.splitext(name)
    dst, n = os.path.join(folder, name), 1
    while os.path.exists(dst):
        if os.path.getsize(dst) == size:
            return dst, True
        n += 1
        dst = os.path.join(folder, '%s-%d%s' % (stem, n, ext))
    return dst, False


def add_song(project_dir, path):
    """Copy an audio file into the project's music folder (never overwriting). Returns its project-relative src."""
    path = os.path.abspath(os.path.expanduser(path))
    name = clean_name(path)
    if not os.path.isfile(path) or os.path.splitext(name)[1].lower() not in AUDIO_EXT:
        raise NewCutError('not an audio file: %s' % os.path.basename(path))
    folder = music_dir(project_dir)
    if L.inside(folder, path):
        return L.rel_src(project_dir, path)
    if not probe_audio(path):
        raise NewCutError('%s cannot be read as audio' % os.path.basename(path))
    os.makedirs(folder, exist_ok=True)
    dst, same = _free_name(folder, name, os.path.getsize(path))
    if not same:
        tmp = dst + '.part'
        shutil.copyfile(path, tmp)
        os.replace(tmp, dst)
    return L.rel_src(project_dir, dst)


_dur_cache = {}


def songs(project_dir):
    folder, out = music_dir(project_dir), []
    try:
        names = sorted(os.listdir(folder), key=str.lower)
    except OSError:
        names = []
    for n in names:
        p = os.path.join(folder, n)
        if n.startswith('.') or os.path.splitext(n)[1].lower() not in AUDIO_EXT or not os.path.isfile(p):
            continue
        st = os.stat(p)
        key = (p, st.st_mtime_ns, st.st_size)
        if key not in _dur_cache:
            _dur_cache[key] = probe_audio(p)
        an = cached_analysis(p, project_dir)
        out.append({'src': L.rel_src(project_dir, p), 'name': n, 'size': st.st_size, 'duration': _dur_cache[key],
                    'analysed': bool(an), 'lyrics': bool(an and isinstance(an.get('lyrics'), dict))})
    return out


# ───────────────────────────────────────────────────────────── HTTP

_jobs, _jobs_lock = {}, threading.Lock()   # (project, song path) -> {"state", "error", "lyrics", "started"}


def _analysis_state(project_dir, spath, lyrics=False):
    with _jobs_lock:
        job = dict(_jobs.get((project_dir, spath)) or {})
    an = cached_analysis(spath, project_dir)
    if job.get('state') == 'running':
        return {'state': 'running', 'lyrics': job.get('lyrics'), 'elapsed': round(time.time() - job['started'], 1), 'analysis': an,
                'envelope': envelope(spath, project_dir) if an else None}
    if an:
        return {'state': 'done', 'analysis': an, 'envelope': envelope(spath, project_dir), 'error': job.get('error')}
    return {'state': 'failed' if job.get('error') else 'none', 'error': job.get('error'), 'analysis': None, 'envelope': None}


def start_analysis(project_dir, spath, lyrics=False, force=False, wait=2.0):
    """Analyse in a background thread (one per song); returns the state after waiting briefly for the quick part."""
    key = (project_dir, spath)
    with _jobs_lock:
        job = _jobs.get(key)
        busy = bool(job and job['state'] == 'running')
        if not busy:
            job = _jobs[key] = {'state': 'running', 'error': None, 'lyrics': bool(lyrics), 'started': time.time()}
    if busy:  # (answered outside the lock: _analysis_state takes it too)
        return _analysis_state(project_dir, spath)

    def work():
        try:
            analyze(spath, project_dir, lyrics=False, force=force)  # the quick part first, so the strip can draw
            if lyrics:
                d = analyze(spath, project_dir, lyrics=True)
                job['error'] = d.get('lyrics_error')
            job['state'] = 'done'
        except Exception as e:
            job.update(state='failed', error=str(e) if isinstance(e, NewCutError) else '%s: %s' % (type(e).__name__, e))

    t = threading.Thread(target=work, name='cutvelvet-analyze', daemon=True)
    t.start()
    t.join(wait)
    return _analysis_state(project_dir, spath)


def state(app):
    """Everything the form needs to draw itself."""
    pd, runner = app.project_dir, _runner(app)
    index = library_index(pd, getattr(app, 'lib', None))
    used = L.used_ranges(pd)
    clips = [{'src': c['src'], 'id': c.get('id'), 'folder': c.get('folder'), 'duration': c.get('duration'), 'poster': c.get('poster'),
              'about': _about(c), 'used': bool(used.get(c['src']))} for c in index.get('clips') or [] if usable(c)]
    folders = []
    for f in index.get('folders') or []:
        mine = [c for c in clips if c['folder'] == f.get('path')]
        folders.append({'path': f.get('path'), 'exists': f.get('exists', True), 'count': len(mine), 'seconds': round(sum(c['duration'] or 0 for c in mine), 1)})
    rules = [{'text': r['text'], 'group': r['group'], 'note': r.get('note')} for r in RULES.load(pd, write=False) if r['on']]
    st = runner.status() if runner is not None else {'state': 'idle'}
    exe = (runner.command() or [''])[0] if runner is not None else None
    proj = L.load_project(pd)
    return {
        'project': {'name': proj.get('name') or os.path.basename(pd), 'dir': pd, 'fps': proj.get('fps') or 24, 'music': L.rel_src(pd, music_dir(pd))},
        'timelines': existing_timelines(pd), 'briefs': list_briefs(pd), 'suggested_id': suggest_id(pd),
        'songs': songs(pd), 'folders': folders, 'clips': clips, 'scanning': bool((index.get('status') or {}).get('scanning')),
        'rules': rules, 'cut_rule': cut_rule(pd), 'whisper': {'available': whisper_available(), 'model': _whisper_model()},
        'agent': {'name': os.path.basename(exe) if exe else None, 'found': bool(exe and runner._which(exe))},
        'job': {k: st.get(k) for k in ('kind', 'state', 'timeline', 'elapsed', 'new_version', 'error')},
        'past': history(runner)[-5:], 'custom_prompt': (lambda p: p and {'name': p['name'], 'digest': p['digest'], 'approved': p['approved'], 'preview': p['text'][:1500]})(SETTINGS.project_prompt(pd, 'newcut_prompt.md')),
    }


def _read_body(h, limit=8 * 1024 * 1024):
    """The request body, always consumed (or the connection marked for closing when it is too large to read)."""
    try:
        n = int(h.headers.get('Content-Length') or 0)
    except ValueError:
        n = -1
    if n < 0 or n > limit:
        h.close_connection = True
        raise NewCutError('request body too large')
    return h.rfile.read(n) if n else b''


def _json_body(h):
    raw = _read_body(h)
    try:
        d = json.loads(raw.decode('utf-8') or '{}')
    except ValueError:
        raise NewCutError('the request body is not JSON')
    if not isinstance(d, dict):
        raise NewCutError('expected a JSON object')
    return d


def _upload(h, app, q):
    """One audio file streamed into the music folder, never overwriting."""
    def refuse(code, msg):
        h.close_connection = True  # the body was not read
        return h._json({'ok': False, 'error': msg}, code)

    name = clean_name(q.get('name'))
    if not name or os.path.splitext(name)[1].lower() not in AUDIO_EXT:
        return refuse(415, 'not an audio file: %s' % (q.get('name') or '?'))
    try:
        size = int(h.headers.get('Content-Length') or '')
    except ValueError:
        return refuse(411, 'Content-Length required')
    if size <= 0 or size > MAX_SONG:
        return refuse(413, '%s is empty or larger than 2 GB' % name)
    folder = music_dir(app.project_dir)
    os.makedirs(folder, exist_ok=True)
    if not L.inside(app.project_dir, folder):
        return refuse(400, 'the music folder of this project points outside the project')
    dst, same = _free_name(folder, name, size)
    part = '%s.%d.part' % (dst, threading.get_ident())
    left = size
    try:
        with open(os.devnull if same else part, 'wb') as f:  # same name and size: read and discard
            while left > 0:
                buf = h.rfile.read(min(CHUNK, left))
                if not buf:
                    break
                f.write(buf)
                left -= len(buf)
        if left:
            raise OSError('upload interrupted')
        if not same:
            if not probe_audio(part):
                raise NewCutError('%s cannot be read as audio' % name)
            os.link(part, dst)  # fails rather than overwrites if the name was taken meanwhile
    except (OSError, NewCutError) as e:
        if left:
            h.close_connection = True
        return h._json({'ok': False, 'error': 'could not store %s: %s' % (name, e)}, 400 if isinstance(e, NewCutError) else 500)
    finally:
        try:
            os.remove(part)
        except OSError:
            pass
    return h._json({'ok': True, 'src': L.rel_src(app.project_dir, dst), 'songs': songs(app.project_dir)})


def handle(h):
    """Every /api/new… request. `h` is the server's request handler (``h.app`` is the project's App).
    The body of every POST is read here, also on error returns."""
    app, u = h.app, urlsplit(h.path)
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    p, pd, post = unquote(u.path).rstrip('/'), app.project_dir, h.command == 'POST'
    if hasattr(h, '_local') and not h._local():
        if post:
            h.close_connection = True
        return h._send(403, 'forbidden')
    runner, lib = _runner(app), getattr(app, 'lib', None)
    try:
        if not post:
            if p == '/api/new':
                return h._json(state(app))
            if p in ('/api/new/analysis', '/api/new/audio'):
                spath = song_path(pd, q.get('song'))
                if not spath or not os.path.isfile(spath):
                    return h._json({'ok': False, 'error': 'no such song in the music folder: %s' % q.get('song')}, 404)
                if p == '/api/new/audio':
                    return h._file(spath)
                return h._json(_analysis_state(pd, spath))
            if p == '/api/new/brief':
                return h._json(load_brief(pd, q.get('id') or '') or {})
            return h._send(404, 'not found')
        if p == '/api/new/song/upload':
            return _upload(h, app, q)
        d = _json_body(h)
        if p == '/api/new/analyze':
            spath = song_path(pd, d.get('song'))
            if not spath or not os.path.isfile(spath):
                return h._json({'ok': False, 'error': 'no such song in the music folder: %s' % d.get('song')}, 404)
            if d.get('lyrics') and not whisper_available():
                return h._json({'ok': False, 'error': 'lyric lines need the optional faster_whisper package, which is not installed'}, 400)
            return h._json(dict(start_analysis(pd, spath, lyrics=bool(d.get('lyrics')), force=bool(d.get('force'))), ok=True))
        if p == '/api/new/song/pick':
            if not _pick_lock.acquire(blocking=False):
                return h._json({'ok': False, 'error': 'The file chooser is already open — look for its window.'}, 409)
            try:
                path, err = pick_file()
            finally:
                _pick_lock.release()
            if err:
                return h._json({'ok': False, 'error': err}, 500)
            if not path:
                return h._json({'ok': False, 'cancelled': True})
            return h._json({'ok': True, 'src': add_song(pd, path), 'songs': songs(pd)})
        brief = clean_brief(d.get('brief') if isinstance(d.get('brief'), dict) else d)
        if p == '/api/new/brief':
            return h._json({'ok': True, 'brief': save_brief(pd, brief)})
        if p == '/api/new/estimate':
            return h._json(estimate(pd, brief, lib, runner))
        if p == '/api/new/prompt':
            return h._json({'ok': True, 'prompt': prepare(pd, brief, lib, write=False)['prompt']})
        if p == '/api/new/start':
            ok, err, info = start(pd, brief, lib, runner)
            return h._json(dict(info, ok=ok, error=err, status=runner.status() if runner is not None else None), 200 if ok else 409)
        return h._send(404, 'not found')
    except NewCutError as e:
        return h._json({'ok': False, 'error': str(e)}, 400)


# ───────────────────────────────────────────────────────────── command line

def cli_new(args):
    try:
        r = new_project(args.dir, getattr(args, 'name', None))
    except (NewCutError, OSError) as e:
        print('cutvelvet: %s' % e, file=sys.stderr)
        return 1
    if r['created']:
        print('new project: %s' % r['dir'])
    else:
        print('%s is already a project; project.json was left as it is' % r['dir'])
    if r['made']:
        print('created: ' + ', '.join(r['made']))
    print('next: put clips in footage/ (and a song in music/ if you want one), then  cutvelvet serve %s  - the page walks you through the rest' % r['dir'])
    return 0


def report(d):
    """A plain-text summary of an analysis."""
    t = d.get('tempo')
    lines = ['%s  %.1f s' % (d.get('song'), d['duration'])]
    lines.append('tempo     %s' % ('%.1f bpm, confidence %s (%.2f), %d beats' % (t['bpm'], t['level'], t['confidence'], len(d['beats'])) if t else 'not found'))
    lines.append('sections  %s' % (' '.join('%.1f' % s['t'] for s in d['sections']) or 'none found'))
    ly = d.get('lyrics')
    if isinstance(ly, dict):
        lines.append('lyrics    %d lines (%s)' % (len(ly.get('lines') or []), ly.get('model')))
        lines += ['  %6.1f  %s' % (l.get('cut', l['t0']), l['text']) for l in ly.get('lines') or []]
    lines += ['note: ' + n for n in d.get('notes') or []] + ['missing: ' + m for m in d.get('missing') or []]
    return '\n'.join(lines)


def cli_analyze(args):
    project = os.path.abspath(args.project)
    audio = args.audio
    for cand in (audio, os.path.join(project, audio), os.path.join(music_dir(project), audio)):
        if os.path.isfile(os.path.expanduser(cand)):
            audio = os.path.expanduser(cand)
            break
    lyrics = bool(getattr(args, 'lyrics', False))
    if lyrics and not whisper_available():
        print('cutvelvet: lyric lines need the optional faster_whisper package, which is not installed; analysing without', file=sys.stderr)
    try:
        d = analyze(audio, project if os.path.isfile(os.path.join(project, 'project.json')) else None, lyrics=lyrics, force=bool(getattr(args, 'force', False)))
    except (NewCutError, OSError) as e:
        print('cutvelvet: %s' % e, file=sys.stderr)
        return 1
    print(report(d))
    print('markers: %s' % analysis_path(audio, project if os.path.isfile(os.path.join(project, 'project.json')) else None))
    return 0


def add_cli(sub):
    """Register ``new`` and ``analyze`` on an argparse subparsers object (used by ``python -m cutvelvet.newcut``)."""
    nw = sub.add_parser('new', help='start a new project folder')
    nw.add_argument('dir')
    nw.add_argument('--name')
    an = sub.add_parser('analyze', help='analyse a song (tempo, beats, sections, optional lyric lines) for cutting')
    an.add_argument('project')
    an.add_argument('audio')
    an.add_argument('--lyrics', action='store_true', help='also find lyric lines (needs the optional faster_whisper package; slow)')
    an.add_argument('--force', action='store_true', help='ignore the cached analysis')


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ['_lyrics'] and len(argv) == 2:  # internal: the low-priority transcription process
        return _lyrics_child(argv[1])
    ap = argparse.ArgumentParser(prog='python -m cutvelvet.newcut', description='start a project / analyse a song')
    sub = ap.add_subparsers(dest='cmd', required=True)
    add_cli(sub)
    args = ap.parse_args(argv)
    return cli_new(args) if args.cmd == 'new' else cli_analyze(args)


if __name__ == '__main__':
    sys.exit(main() or 0)
