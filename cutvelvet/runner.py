"""Run notes: hand a version's saved review notes to a coding agent, one job at a time.

The agent is any command-line coding agent. It is started in the project folder with a prompt that tells it
to apply the notes to the timeline JSON, check its work, and ``cutvelvet publish`` the next version.

Which agent, which model and the exact command are a per-machine setting (agents.py, the page's Settings
popover). A project.json may ask for its own ``"agent": {"command": [...]}``, but that is only used once the
user has approved it on this machine.

Placeholders in the command: ``{prompt}`` (the prompt text), ``{prompt_file}`` (a file holding it),
``{project}`` (project folder), ``{guide}`` (the folder holding the agent guide and nothing else). With neither prompt placeholder the
prompt is piped to stdin. The default is Claude Code, headless, limited to an explicit tool list
(see DEFAULT_COMMAND). ``cutvelvet`` is put on PATH for the agent via a shim kept with the per-machine settings (see ``_shim``).

The enabled entries of the project's rules.json (see rules.py) are embedded in the prompt as text.
The prompt template is PROMPT below, or ``<project>/.cutvelvet/run_prompt.md`` once the user has approved that
file on this machine (settings.project_prompt): a project folder can come from anyone, and this text steers the agent.
Each run writes ``.cutvelvet/runs/<ts>.log`` (readable progress), ``<ts>.raw.log`` (the agent's raw
output) and ``<ts>.prompt.md``. The agent writes straight into the raw log, not into a pipe, so it
survives a server restart; ``.cutvelvet/runs/current.json`` lets the next server pick the job up again.
"""
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time

from . import agents as AGENTS
from . import library as L
from . import rules as RULES
from . import settings as SETTINGS

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULT_COMMAND = AGENTS.CLAUDE   # the tested default; which agent actually runs is a per-machine setting (agents.py)

PROMPT = """\
You are revising one timeline of a film project with the cutvelvet tool. A reviewer watched the published
version {version} and left notes. Apply them, check your work, and publish the next version.

Project folder (your working directory): {project}
Timeline to edit: {timeline_path}   (timeline id: {timeline})
Reviewer notes:   {feedback_path}
{snapshot_line}
Read this first:
- {agents_md} — how cutvelvet timelines, the command line and the checks work.

{rules_line}
`cutvelvet` is on PATH for this run (it is `python -m cutvelvet`).

How to read the notes file:
- `shots.<shot id>.note` — free text about that shot. A leading `[m:ss.s]` is the moment in the reviewed
  video the remark is about.
- `shots.<shot id>.verdict` — "keep" (leave it alone), "fix" (change it as the note says) or "cut"
  (remove or replace the shot as the note says).
- `shots.<shot id>.pins` — a list of {src, ss, comment}. The reviewer picked that source clip at that source
  second for that shot: use that `src` starting at that `ss` in that shot. The comment says how.
- `overall` — notes that apply to the whole timeline.

Do this, in order:
1. Apply every note and honour every pin by editing {timeline_path}. Change only what the notes ask for;
   leave every other shot exactly as it is.
2. For each shot you changed, render frames with
   `cutvelvet stills {project} {timeline} <t1,t2,...> --scale 0.5` and LOOK at the jpgs it writes (open them).
   Confirm the change does what the note asked; fix and re-check until it does.
3. Run `cutvelvet audit {project} {timeline}` and fix every error it reports (warnings are for your information).
4. Run `cutvelvet publish {project} {timeline} --scale 0.5`. It prints the new version name.
5. Write {feedback_dir}/<new version>_changes.md: one short line per note or pin saying what you did, or
   why it was not done. Plain language, no preamble.

Do not edit anything else under review/ or feedback/, and do not touch other timelines. If a note is
ambiguous, make the most conservative reasonable change and say so in the changes file.
"""

# Variables a parent agent session leaves in the environment; they must not leak into the agent we start.
_SESSION_ENV = re.compile(r'^(CLAUDECODE|CLAUDE_PID|CLAUDE_EFFORT|AI_AGENT|CLAUDE_CODE_(ENTRYPOINT|MESSAGING_\w+|EXECPATH|'
                          r'SESSION_\w+|CHILD_SESSION|EXPERIMENTAL_AGENT_TEAMS|SSE_PORT))$')
_EXTRA_PATH = ['~/.local/bin', '/opt/homebrew/bin', '/usr/local/bin']
TAIL_LINES = 60
VERSION_RE = re.compile(r'^([A-Za-z0-9]+)_v(\d+)$')


def has_notes(fb):
    """True when a feedback dict holds anything for an agent to act on."""
    if not isinstance(fb, dict):
        return False
    if str(fb.get('overall') or '').strip():
        return True
    for e in (fb.get('shots') or {}).values():
        if isinstance(e, dict) and (str(e.get('note') or '').strip() or e.get('pins') or e.get('verdict') in ('fix', 'cut')):
            return True
    return False


def _short(s, n=220):
    s = ' '.join(str(s).split())
    return s if len(s) <= n else s[:n - 1] + '…'


def humanize(line):
    """Turn one line of agent output into zero or more short readable lines.

    Understands Claude Code's ``--output-format stream-json`` events; anything else passes through as is.
    """
    line = line.rstrip('\n')
    if not line.strip():
        return []
    if not line.lstrip().startswith('{'):
        return [line]
    try:
        ev = json.loads(line)
    except ValueError:
        return [line]
    if not isinstance(ev, dict) or 'type' not in ev:
        return [line]
    t, out = ev.get('type'), []
    if t == 'system':
        if ev.get('subtype') == 'init':
            out.append('agent started (%s)' % (ev.get('model') or 'model unknown'))
    elif t == 'assistant':
        for b in (ev.get('message') or {}).get('content') or []:
            if b.get('type') == 'text' and b.get('text', '').strip():
                out += [_short(x, 400) for x in b['text'].strip().splitlines() if x.strip()]
            elif b.get('type') == 'tool_use':
                name, a = b.get('name'), b.get('input') or {}
                if name == 'Bash':
                    out.append('$ ' + _short(a.get('command', '')))
                elif name in ('Read', 'Edit', 'Write', 'MultiEdit', 'NotebookEdit'):
                    out.append('%s %s' % (name.lower(), a.get('file_path') or a.get('notebook_path') or ''))
                elif name in ('Glob', 'Grep'):
                    out.append('%s %s' % (name.lower(), _short(a.get('pattern', ''), 80)))
                else:
                    out.append('tool: %s' % name)
    elif t == 'user':
        for b in (ev.get('message') or {}).get('content') or []:
            if isinstance(b, dict) and b.get('type') == 'tool_result':
                c = b.get('content')
                if isinstance(c, list):
                    c = ' '.join(x.get('text', '') for x in c if isinstance(x, dict) and x.get('type') == 'text')
                c = str(c or '').strip()
                if b.get('is_error'):
                    out.append('  ! ' + _short(c, 300))
                elif c:
                    last = [x for x in c.splitlines() if x.strip()]
                    if last:
                        out.append('  → ' + _short(last[-1], 160))
    elif t == 'result':
        secs = (ev.get('duration_ms') or 0) / 1000.0
        cost = ev.get('total_cost_usd')
        out.append('agent %s after %ds%s' % ('reported an error' if ev.get('is_error') else 'finished', secs,
                                             ', $%.2f' % cost if isinstance(cost, (int, float)) else ''))
        denied = ev.get('permission_denials') or []
        if denied:
            out.append('  %d tool call(s) were refused by the permission list' % len(denied))
    return out


class Runner:
    def __init__(self, project_dir, feedback_dir, review_dir):
        self.project = os.path.abspath(project_dir)
        self.feedback = feedback_dir
        self.review = review_dir
        self.runs = os.path.join(self.project, '.cutvelvet', 'runs')
        self.lock = threading.Lock()
        self.proc = None
        self.job = None  # {state, version, timeline, started, ended, log, raw, pid, before, returncode, error, cancel}
        self._recover()

    # ------------------------------------------------------------ surviving a server restart
    def _current(self):
        return os.path.join(self.runs, 'current.json')

    def _remember(self, job):
        try:
            L.write_json(self._current(), {k: job[k] for k in ('kind', 'state', 'version', 'timeline', 'started', 'ended', 'log', 'raw', 'pid', 'lstart', 'before', 'returncode', 'error', 'agent', 'cost')})
        except OSError:
            pass

    def _recover(self):
        """Pick up the last job from current.json: still running -> follow it again; over -> show how it ended."""
        try:
            with open(self._current()) as f:
                job = json.load(f)
            job = {k: job.get(k) for k in ('kind', 'state', 'version', 'timeline', 'started', 'ended', 'log', 'raw', 'pid', 'lstart', 'before', 'returncode', 'error', 'agent', 'cost')}
            job['kind'] = job['kind'] or 'run'
            if not (job['timeline'] and job['log'] and job['raw'] and isinstance(job['before'], list)):
                return
            # current.json sits in the project, which can come from anyone: its paths are read from and written to,
            # and its pid can be signalled, so take nothing that is not plainly one of our own run files.
            for k in ('log', 'raw'):
                p = job[k]
                if not (isinstance(p, str) and os.path.dirname(os.path.abspath(p)) == self.runs and p.endswith('.log')
                        and not os.path.islink(p) and L.inside(self.project, p)):
                    return
            if not (isinstance(job['timeline'], str) and isinstance(job['started'], (int, float)) and not isinstance(job['started'], bool)
                    and job['state'] in ('running', 'done', 'failed', 'cancelled')
                    and (job['pid'] is None or (isinstance(job['pid'], int) and not isinstance(job['pid'], bool) and job['pid'] > 1))):
                return
        except (OSError, ValueError, AttributeError, TypeError):
            return
        job['cancel'] = False
        self.job = job
        if job['state'] != 'running':
            return
        # Still going: follow it again. Already gone: the same pass rebuilds the readable log and settles the state.
        threading.Thread(target=self._pump, args=(None, job), name='cutvelvet-run', daemon=True).start()

    @staticmethod
    def _lstart(pid):
        try:
            return subprocess.run(['ps', '-o', 'lstart=', '-p', str(pid)], capture_output=True, text=True, timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ''

    def _is_ours(self, job):
        """Is job['pid'] still the agent we started (same start time, so not a reused process number)?"""
        pid = job.get('pid')
        return bool(pid) and SETTINGS.alive(pid) and self._lstart(pid) == job.get('lstart')

    def _finish(self, job, rc):
        with self.lock:
            job['returncode'], job['ended'] = rc, job.get('ended') or time.time()
            if job.get('cancel'):
                job['state'] = 'cancelled'
            elif rc is None:  # unknown exit code: judge by whether a new version was published
                new = [n for n in self.versions(job['timeline']) if n not in job['before']]
                job['state'] = 'done' if new else 'failed'
                job['error'] = None if new else 'the agent stopped while the server was not running, without publishing'
            else:
                job['state'] = 'done' if rc == 0 else 'failed'
                job['error'] = None if rc == 0 else '%s exited with code %s' % ('publish' if job.get('kind') == 'publish' else 'the agent', rc)
                if job.get('timed_out'):
                    job['error'] = 'no answer after %d seconds, so it was stopped' % self.TEST_TIMEOUT
        self._record(job)  # duration (and cost, when the agent reports one) for later estimates
        self._remember(job)

    # ------------------------------------------------------------ config
    NO_AGENT = 'No agent set up — open Settings (the gauge in the header) and choose one.'

    def command(self):
        """The agent command for this machine and project ([] when none is set up). See agents.resolve."""
        return AGENTS.resolve(self.project)['argv']

    def agent_error(self):
        """Why no agent can be started right now, or '' when one can."""
        if AGENTS.project_pending(self.project):
            return ('This project asks for its own agent command. Open Settings (the gauge in the header) and either allow it '
                    'or choose to ignore it; nothing is started until you do.')
        cmd = self.command()
        if not cmd:
            return self.NO_AGENT
        if not self._which(cmd[0]):
            return 'Agent not found: %s is not installed or not on PATH. Open Settings (the gauge in the header) to choose another.' % cmd[0]
        return AGENTS.guard(cmd)   # test-safety switches: CUTVELVET_AGENT_DISABLED / CUTVELVET_AGENT_ONLY

    def _which(self, exe):
        return AGENTS.which(exe)

    def timelines_dir(self):
        return L.abs_path(self.project, L.load_project(self.project).get('timelines') or 'timelines')

    def versions(self, tid):
        try:
            ns = [int(m.group(1)) for m in (re.match(r'^%s_v(\d+)\.mp4$' % re.escape(tid), f) for f in os.listdir(self.review)) if m]
        except OSError:
            ns = []
        return sorted(ns)

    def check(self, version):
        """(ok, reason, info) -- can the notes of this version be run right now?"""
        m = VERSION_RE.match(version or '')
        if not m:
            return False, 'not a version name: %r' % version, {}
        tid = m.group(1)
        tpath = os.path.join(self.timelines_dir(), tid + '.json')
        fpath = os.path.join(self.feedback, version + '.json')
        info = {'timeline': tid, 'timeline_path': tpath, 'feedback_path': fpath}
        if not os.path.isfile(tpath):
            return False, 'no timeline file for %s (%s)' % (tid, L.rel_src(self.project, tpath)), info
        try:
            with open(fpath) as f:
                fb = json.load(f)
        except (OSError, ValueError):
            fb = None
        if not has_notes(fb):
            return False, 'no notes or pins saved for %s yet' % version, info
        if self.agent_error():
            return False, self.agent_error(), info
        return True, '', info

    # ------------------------------------------------------------ status
    def status(self, version=None):
        with self.lock:
            j = dict(self.job) if self.job else None
        d = {'kind': None, 'state': 'idle', 'version': None, 'timeline': None, 'started': None, 'elapsed': 0, 'tail': [],
             'new_version': None, 'changes': None, 'log': None, 'returncode': None, 'error': None, 'cost': None}
        if j:
            end = j.get('ended') or time.time()
            d.update(kind=j.get('kind') or 'run', state=j['state'], version=j['version'], timeline=j['timeline'], started=j['started'],
                     elapsed=round(end - j['started'], 1), log=L.rel_src(self.project, j['log']),
                     returncode=j.get('returncode'), error=j.get('error'), cost=j.get('cost'))
            d['tail'] = self._tail(j['log'])
            new = [n for n in self.versions(j['timeline']) if n not in j['before']]
            if new:
                d['new_version'] = '%s_v%d' % (j['timeline'], new[-1])
                d['changes'] = self.changes(d['new_version'])
        if version:
            ok, reason, _ = self.check(version)
            d['check'] = {'version': version, 'ok': ok, 'reason': reason}
        cmd = self.command()
        d['agent'] = os.path.basename(cmd[0]) if cmd else ''
        d['agent_error'] = self.agent_error()
        return d

    def cut_changed(self, version):
        """Has the timeline on disk moved on since its newest published version? (None: no snapshot to compare with.)

        Returns {"changed", "latest", "timeline"}; ``latest`` is the newest version of the timeline `version` belongs to.
        """
        m = VERSION_RE.match(version or '')
        if not m:
            return None
        tid = m.group(1)
        ns = self.versions(tid)
        if not ns:
            return None
        latest = '%s_v%d' % (tid, ns[-1])
        try:
            with open(os.path.join(self.timelines_dir(), '.history', latest + '.json')) as f:
                snap = json.load(f)
            with open(os.path.join(self.timelines_dir(), tid + '.json')) as f:
                now = json.load(f)
        except (OSError, ValueError):
            return None
        return {'changed': snap != now, 'latest': latest, 'timeline': tid}

    def changes(self, version):
        if not VERSION_RE.match(version or ''):
            return None
        try:
            with open(os.path.join(self.feedback, version + '_changes.md'), encoding='utf-8', errors='replace') as f:
                return f.read(40000)
        except OSError:
            return None

    @staticmethod
    def _tail(path, n=TAIL_LINES):
        try:
            with open(path, 'rb') as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - 24000))
                return f.read().decode('utf-8', 'replace').splitlines()[-n:]
        except OSError:
            return []

    # ------------------------------------------------------------ start / cancel
    def _shim(self):
        """A folder holding ``cutvelvet`` -> this interpreter's cutvelvet, so agents (and Publish) can just call it.

        It lives with the per-machine settings (``~/.cutvelvet/bin-<id>``), not in the project: this folder goes
        first on PATH, so nothing a project brings along, and nothing an agent writes into the project, may sit in
        it. The shim also keeps the working directory (the project) off Python's import path, so a ``json.py`` or a
        ``cutvelvet/`` folder inside a project is never imported in place of the real one."""
        import hashlib
        import shlex
        d = os.path.join(SETTINGS.home(), 'bin-' + hashlib.sha1(('%s|%s' % (REPO, sys.executable)).encode()).hexdigest()[:8])
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, 'cutvelvet')
        code = 'import sys; sys.path[0] = sys.argv.pop(1); from cutvelvet.cli import main; main()'
        body = '#!/bin/sh\nexec %s -c %s %s "$@"\n' % (shlex.quote(sys.executable), shlex.quote(code), shlex.quote(REPO))
        try:
            with open(p) as f:
                same = f.read() == body and not os.path.islink(p)
        except OSError:
            same = False
        if not same:
            tmp = '%s.%d.tmp' % (p, os.getpid())
            with open(tmp, 'w') as f:
                f.write(body)
            os.chmod(tmp, 0o755)
            os.replace(tmp, p)  # replaces a link rather than writing through it
        return d

    def build_prompt(self, version, info):
        tid = info['timeline']
        own = SETTINGS.project_prompt(self.project, 'run_prompt.md')  # the project's own template: only once approved here
        tpl = own['text'] if own and own['approved'] else PROMPT
        snap = os.path.join(self.timelines_dir(), '.history', version + '.json')
        latest = self.versions(tid)
        snapshot_line = ''
        if os.path.isfile(snap):
            snapshot_line += 'The timeline as it was when %s was published: %s (read-only reference).\n' % (version, snap)
        if latest and '%s_v%d' % (tid, latest[-1]) != version:
            snapshot_line += ('Note: %s_v%d has been published since; the notes were written against %s, so check each one '
                              'still applies to the current timeline.\n' % (tid, latest[-1], version))
        rules_line = RULES.as_text(self.project)
        vals = {
            'version': version, 'project': self.project, 'timeline': tid, 'timeline_path': info['timeline_path'],
            'feedback_path': info['feedback_path'], 'feedback_dir': self.feedback,
            'agents_md': os.path.join(AGENTS.GUIDE, 'AGENTS.md'),
            'rules_line': rules_line,
            'snapshot_line': snapshot_line, 'cutvelvet': AGENTS.GUIDE,
        }
        for k, v in vals.items():
            tpl = tpl.replace('{%s}' % k, v)
        return tpl

    def start(self, version):
        ok, reason, info = self.check(version)
        if not ok:
            return False, reason
        with self.lock:
            if self.job and self.job['state'] == 'running':
                return False, 'a run is already in progress (%s)' % (self.job['version'] or 'publishing ' + self.job['timeline'])
            os.makedirs(self.runs, exist_ok=True)
            ts = time.strftime('%Y%m%d-%H%M%S')
            base = os.path.join(self.runs, ts)
            cmd, piped = self._agent_cmd(self.build_prompt(version, info), base)
            prompt = open(base + '.prompt.md', encoding='utf-8').read() if piped else None
            return self._spawn('run', cmd, version, info['timeline'], base, prompt)

    def start_job(self, kind, prompt, timeline_id):
        """Launch the configured agent with an arbitrary prompt, in the same single job slot as Run notes.

        Same shim on PATH, tool allow-list, logs, cancel, status and restart recovery. ``GET /api/run`` reports
        ``kind`` as given (e.g. "newcut") and, once the agent has published, ``new_version`` (``<timeline_id>_v1``
        for a timeline that had no versions). Returns (ok, error)."""
        kind = re.sub(r'[^a-z0-9_]', '', str(kind or '').lower()) or 'job'
        if not re.match(r'^[A-Za-z0-9]+$', timeline_id or ''):
            return False, 'not a timeline id: %r' % timeline_id
        if not isinstance(prompt, str) or not prompt.strip():
            return False, 'empty prompt'
        if self.agent_error():
            return False, self.agent_error()
        with self.lock:
            if self.job and self.job['state'] == 'running':
                return False, 'another job is already in progress'
            os.makedirs(self.runs, exist_ok=True)
            base = os.path.join(self.runs, time.strftime('%Y%m%d-%H%M%S'))
            cmd, piped = self._agent_cmd(prompt, base)
            return self._spawn(kind, cmd, None, timeline_id, base, prompt if piped else None)

    TEST_TIMEOUT = 120

    def start_test(self):
        """Settings > Agent > Test: one trivial prompt through the real agent command, in the normal job slot.

        A real (tiny) billed call. It is stopped after TEST_TIMEOUT seconds. The result is the job's state, elapsed
        time, cost (when the agent reports one) and log tail, under ``kind: "agenttest"`` in GET /api/run."""
        if self.agent_error():
            return False, self.agent_error()
        with self.lock:
            if self.job and self.job['state'] == 'running':
                return False, 'another job is already in progress'
            os.makedirs(self.runs, exist_ok=True)
            base = os.path.join(self.runs, time.strftime('%Y%m%d-%H%M%S'))
            cmd, piped = self._agent_cmd(AGENTS.TEST_PROMPT, base)
            ok, err = self._spawn('agenttest', cmd, None, '_test', base, AGENTS.TEST_PROMPT if piped else None)
            job = self.job
        if ok:
            threading.Thread(target=self._test_timeout, args=(job,), daemon=True).start()
        return ok, err

    def _test_timeout(self, job):
        end = time.time() + self.TEST_TIMEOUT
        while time.time() < end and job['state'] == 'running':
            time.sleep(0.5)
        if job['state'] == 'running':
            job['timed_out'] = True
            self._kill(job['pid'], signal.SIGTERM)
            time.sleep(3)
            self._kill(job['pid'], signal.SIGKILL)

    def _agent_cmd(self, prompt, base):
        """The agent command with its placeholders filled in; writes <base>.prompt.md. Returns (argv, prompt goes to stdin?)."""
        with open(base + '.prompt.md', 'w', encoding='utf-8') as f:
            f.write(prompt)
        # {project} with symlinks resolved, so path-scoped permission rules match the files the agent actually opens.
        # {cutvelvet} is kept for commands written before {guide} existed: it now names the same guide-only folder.
        subs = {'{prompt}': prompt, '{prompt_file}': base + '.prompt.md', '{project}': os.path.realpath(self.project),
                '{guide}': AGENTS.GUIDE, '{cutvelvet}': AGENTS.GUIDE}
        raw = self.command()
        piped = not any('{prompt}' in a or '{prompt_file}' in a for a in raw)
        cmd = []
        for a in raw:
            for k, v in subs.items():
                a = a.replace(k, v)
            cmd.append(a)
        cmd[0] = self._which(cmd[0]) or cmd[0]
        return cmd, piped

    # ------------------------------------------------------------ what past jobs cost
    def _job_log(self):
        return os.path.join(self.project, '.cutvelvet', 'jobs.json')

    def job_history(self, kind=None):
        """Finished jobs, oldest first: [{kind, timeline, version, state, seconds, cost, at}] (cost in USD or None)."""
        try:
            with open(self._job_log()) as f:
                rows = json.load(f)
        except (OSError, ValueError):
            rows = []
        rows = [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
        return [r for r in rows if kind is None or r.get('kind') == kind]

    @staticmethod
    def _cost(raw_path):
        """total_cost_usd from the last stream-json "result" event in a raw log, if the agent reported one."""
        try:
            with open(raw_path, 'rb') as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - 200000))
                tail = f.read().decode('utf-8', 'replace')
        except OSError:
            return None
        for line in reversed(tail.splitlines()):
            if '"total_cost_usd"' in line:
                try:
                    c = json.loads(line).get('total_cost_usd')
                    return round(float(c), 4) if isinstance(c, (int, float)) else None
                except ValueError:
                    continue
        return None

    def _record(self, job):
        try:
            job['cost'] = self._cost(job['raw']) if job.get('kind') != 'publish' else None
            rows = self.job_history()[-199:]
            rows.append({'kind': job.get('kind'), 'timeline': job.get('timeline'), 'version': job.get('version'), 'state': job.get('state'),
                         'seconds': round((job.get('ended') or time.time()) - job['started'], 1), 'cost': job.get('cost'), 'at': int(time.time())})
            L.write_json(self._job_log(), rows)
        except (OSError, KeyError, TypeError):
            pass

    def check_publish(self, tid):
        if not re.match(r'^[A-Za-z0-9]+$', tid or ''):
            return False, 'not a timeline id: %r' % tid
        if not os.path.isfile(os.path.join(self.timelines_dir(), tid + '.json')):
            return False, 'no timeline file for %s' % tid
        return True, ''

    def start_publish(self, tid):
        """Publish the timeline as it is on disk -- ``cutvelvet publish``, no agent -- in the same single job slot."""
        ok, reason = self.check_publish(tid)
        if not ok:
            return False, reason
        with self.lock:
            if self.job and self.job['state'] == 'running':
                return False, 'another job is already in progress'
            os.makedirs(self.runs, exist_ok=True)
            base = os.path.join(self.runs, time.strftime('%Y%m%d-%H%M%S'))
            cmd = [os.path.join(self._shim(), 'cutvelvet'), 'publish', self.project, tid, '--scale', '0.5']
            return self._spawn('publish', cmd, None, tid, base, None)

    def _spawn(self, kind, cmd, version, tid, base, stdin_text):  # lock held
        if kind != 'publish' and AGENTS.guard(cmd):   # the one place an agent process is created: checked again here, whoever called
            return False, AGENTS.guard(cmd)
        env = {k: v for k, v in os.environ.items() if not _SESSION_ENV.match(k)}
        env['PATH'] = os.pathsep.join([self._shim(), env.get('PATH', '')] + [os.path.expanduser(p) for p in _EXTRA_PATH])
        env['CUTVELVET_PROJECT'] = self.project
        env['PYTHONUNBUFFERED'] = '1'
        if version:
            env['CUTVELVET_RUN_VERSION'] = version
        job = {'kind': kind, 'state': 'running', 'version': version, 'timeline': tid, 'started': time.time(), 'ended': None,
               'log': base + '.log', 'raw': base + '.raw.log', 'pid': None, 'before': self.versions(tid),
               'returncode': None, 'error': None, 'cancel': False, 'cost': None, 'agent': 'cutvelvet publish' if kind == 'publish' else os.path.basename(cmd[0])}
        try:
            with open(job['raw'], 'wb') as raw:  # a file, not a pipe: the job keeps running if this server stops
                proc = subprocess.Popen(cmd, cwd=self.project, env=env, stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                                        stdout=raw, stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as e:
            return False, 'could not start: %s' % e
        job['pid'], job['lstart'] = proc.pid, self._lstart(proc.pid)
        self.proc, self.job = proc, job
        self._remember(job)
        if stdin_text is not None:
            try:
                proc.stdin.write(stdin_text.encode('utf-8'))
                proc.stdin.close()
            except OSError:
                pass
        threading.Thread(target=self._pump, args=(proc, job), name='cutvelvet-run', daemon=True).start()
        return True, ''

    def _pump(self, proc, job):
        """Follow the raw log, writing readable lines to the .log, until the agent is gone. proc is None for a
        job adopted after a restart (then the readable log is rebuilt from the start of the raw one)."""
        try:
            with open(job['raw'], 'rb') as raw, open(job['log'], 'w', encoding='utf-8') as log:
                log.write(('publish %s\n' % job['timeline']) if job.get('kind') == 'publish' else
                          ('testing the agent (%s)\n' % (job.get('agent') or 'agent')) if job.get('kind') == 'agenttest' else
                          ('run notes for %s with %s\n' % (job['version'], job.get('agent') or 'the agent')) if job.get('version') else
                          '%s for %s with %s\n' % (job.get('kind'), job['timeline'], job.get('agent') or 'the agent'))
                buf, gone = b'', False
                while True:
                    chunk = raw.read(65536)
                    if chunk:
                        buf += chunk
                        *lines, buf = buf.split(b'\n')
                        for b in lines:
                            for ln in humanize(b.decode('utf-8', 'replace')):
                                log.write(self._short_paths(ln) + '\n')
                        log.flush()
                        continue
                    if gone:
                        break
                    gone = (proc.poll() is not None) if proc is not None else not self._is_ours(job)
                    if not gone:
                        time.sleep(0.3 if proc is not None else 1.0)
                for ln in humanize(buf.decode('utf-8', 'replace')):
                    log.write(self._short_paths(ln) + '\n')
                self._finish(job, proc.wait() if proc is not None else None)
                log.write('— %s —\n' % job['state'])
        except Exception as e:  # never leave a job stuck in "running"
            with self.lock:
                job.update(state='failed', ended=time.time(), error='%s: %s' % (type(e).__name__, e))
            self._remember(job)

    def _short_paths(self, line):
        """The readable log is shown in the page: project paths become relative and the home folder becomes ~, so a
        screenshot or a pasted log does not carry the user's folder names. The raw log is left as the agent wrote it."""
        for root in sorted({os.path.realpath(self.project), self.project}, key=len, reverse=True):
            line = line.replace(root + os.sep, '').replace(root, '.')
        for other, name in ((AGENTS.GUIDE, '<cutvelvet guide>'), (os.path.realpath(os.path.expanduser('~')), '~'), (os.path.expanduser('~'), '~')):
            line = line.replace(other, name)
        return line

    def cancel(self):
        with self.lock:
            job = self.job
            if not job or job['state'] != 'running' or not job.get('pid'):
                return False
            job['cancel'] = True
        self._kill(job['pid'], signal.SIGTERM)
        threading.Thread(target=self._kill_later, args=(job,), daemon=True).start()
        return True

    @staticmethod
    def _kill(pid, sig):
        try:
            os.killpg(pid, sig)  # start_new_session made the agent the leader of its own process group
        except (ProcessLookupError, PermissionError):
            pass

    def _kill_later(self, job):
        for _ in range(25):
            if job['state'] != 'running':
                break
            time.sleep(0.2)
        self._kill(job['pid'], signal.SIGKILL)  # also sweeps children that outlived the agent

    # ------------------------------------------------------------ what a run will cost
    def estimate(self, version=None, timeline=None):
        """Before a Run (version) or a Publish (timeline): which shots will re-render, how many at a time, roughly how long.

        A Run counts shots with notes plus shots not in the cache; a Publish counts only shots not in the cache."""
        if timeline:
            ok, reason = self.check_publish(timeline)
            info = {'timeline': timeline, 'timeline_path': os.path.join(self.timelines_dir(), timeline + '.json'), 'feedback_path': os.devnull}
        else:
            ok, reason, info = self.check(version)
        perf, mach = SETTINGS.performance(), SETTINGS.machine()
        out = {'ok': False, 'reason': reason, 'jobs': perf['jobs'], 'mode': perf['mode'], 'cores': mach['cores'], 'shots': 0, 'total': 0,
               'all': False, 'minutes': None, 'timed': False}
        if 'timeline_path' not in info or not os.path.isfile(info['timeline_path']):
            return out
        try:
            from . import cli, timeline as TL  # needs numpy; only imported when an estimate is asked for
            proj = TL.load_project(self.project)
            tl = TL.load_timeline(proj, info['timeline'], strict=False)
            fb = None
            if not timeline:
                with open(info['feedback_path']) as f:
                    fb = json.load(f)
        except Exception as e:
            out['reason'] = out['reason'] or str(e)
            fb, tl = None, None
        if not tl:
            return out
        fps, scale = proj['fps'], 0.5
        noted = {sid for sid, e in ((fb or {}).get('shots') or {}).items() if isinstance(e, dict) and
                 (str(e.get('note') or '').strip() or e.get('pins') or e.get('verdict') in ('fix', 'cut'))}
        whole = bool(str((fb or {}).get('overall') or '').strip())
        frames = {}
        for s in tl.get('shots') or []:
            try:
                n0, n1 = TL.shot_frames(s, fps)
                stale = not os.path.exists(cli.shot_cache_path(proj, tl, s, scale))
            except Exception:
                continue
            if whole or s['id'] in noted or stale:
                frames[s['id']] = n1 - n0
        jobs = max(1, min(perf['jobs'], len(frames) or 1))
        secs = SETTINGS.estimate_seconds(self.project, tl.get('id', info['timeline']), 'draft', frames, jobs)
        out.update(ok=ok, ids=list(frames), shots=len(frames), total=len(tl.get('shots') or []), all=whole, jobs=jobs, timed=secs is not None,
                   minutes=None if secs is None else round(secs / 60.0, 1), use_cores=SETTINGS.cores_for(jobs))
        return out
