"""Which coding agent Run notes and New cut start -- a per-machine choice, kept in ``~/.cutvelvet/settings.json``.

    "agent": {"choice": "claude" | "codex" | "gemini" | "custom",   (absent = the first known agent found on PATH)
              "model": "",                                           (optional; mapped to the agent's own model flag)
              "commands": {"claude": ["claude", "-p", "{prompt}", ...], "custom": [...]},   (only where the user edited one)
              "approved": {"/abs/project": "<hash of that project's own command>"}}

cutvelvet never holds an API key or a login. It starts a command-line agent the user has installed and signed
in to, and that agent's provider bills the user.

Known agents (KNOWN) carry an honest ``tested`` flag: only Claude Code has actually been run by cutvelvet. The
others are templates built from their ``--help`` text (or from memory, where the tool was not installed) and
say so in the page.

A project may carry its own ``"agent": {"command": [...]}`` in project.json. Because a project folder can come
from anyone and this command is executed, it is shown to the user but NOT used until they approve that exact
command for that project on this machine (``approved`` above); if the command later changes, approval lapses.

Placeholders in a command: ``{prompt}``, ``{prompt_file}``, ``{project}`` (the project folder, symlinks resolved),
``{guide}`` (the folder holding the agent guide, AGENTS.md, and nothing else; ``{cutvelvet}`` is an older name
for the same folder). See runner.py.

Two environment switches exist so tests can never start (and bill) a real agent by accident:

    CUTVELVET_AGENT_DISABLED=1          every agent start is refused
    CUTVELVET_AGENT_ONLY=/path/to/fake  an agent start is refused unless the resolved program is exactly this file

Set one of them in every test harness. ``guard(argv)`` is checked at the single place processes are started.
"""
import hashlib
import json
import os
import re
import shlex
import shutil

from . import library as L
from . import settings as SETTINGS

EXTRA_PATH = ['~/.local/bin', '/opt/homebrew/bin', '/usr/local/bin']

GUIDE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'guide')   # holds AGENTS.md and nothing else

# Claude Code, non-interactive, as tightly as its own permission system allows:
#   dontAsk            anything not allowed below is refused instead of prompting
#   reading            needs no rule inside the working directories, which are the project folder and the guide
#                      folder (--add-dir); the two "block...OutsideWorkingDirectories" settings make the file tools,
#                      and the shell's built-in read-only commands such as cat, refuse everything else
#   editing            only files inside the project folder -- Edit(//abs/path/**) is Claude Code's absolute-path
#                      rule form and covers every file-editing tool -- and never project.json or .env files
#   shell              only `cutvelvet ...` (which is itself confined to this project during a run, see cli.py),
#                      plus the read-only commands Claude Code permits in every mode (ls, cat, uname ...)
#   --setting-sources  only the user's own Claude Code settings: a project's .claude/settings.json (hooks, extra
#                      permissions) can arrive with a downloaded project and must not apply
#   also off           web fetch and search, MCP servers, skills and slash commands (a project can carry those too)
CLAUDE = [
    'claude', '-p', '{prompt}',
    '--output-format', 'stream-json', '--verbose',
    '--permission-mode', 'dontAsk',
    '--setting-sources', 'user',
    '--settings', '{"permissions":{"blockReadsOutsideWorkingDirectories":true,"blockWritesOutsideWorkingDirectories":true}}',
    '--allowedTools', 'Edit(/{project}/**)', 'Bash(cutvelvet *)',
    '--disallowedTools', 'Read(.env)', 'Read(.env.*)', 'Edit(/{project}/project.json)', 'Edit(/{project}/.claude/**)', 'WebFetch', 'WebSearch',
    '--add-dir', '{guide}',
    '--strict-mcp-config',
    '--disable-slash-commands',
]

KNOWN = [
    {'id': 'claude', 'name': 'Claude Code', 'bin': 'claude', 'tested': True, 'model_flag': '--model', 'default': CLAUDE,
     'note': 'Tested with cutvelvet. It may read the project folder and the cutvelvet guide, edit files inside the project folder, and run cutvelvet (plus harmless read-only commands such as ls). Nothing else: no other programs, no web, and no settings or skills that came with the project.',
     'install': 'Install Claude Code (from Anthropic), then run "claude" once in a terminal to sign in.'},
    {'id': 'codex', 'name': 'Codex CLI', 'bin': 'codex', 'tested': False, 'model_flag': '-m',
     'default': ['codex', 'exec', '--full-auto', '--skip-git-repo-check', '{prompt}'],
     'note': 'Untested template, written without Codex installed. Check the flags against "codex exec --help" and use Test before relying on it. '
             'Unlike the Claude Code command, it is not known to be limited to the project folder or to ignore settings that come with a project: '
             'rely on Codex\'s own sandbox, and look at a project you did not make before running it.',
     'install': 'Install Codex CLI (from OpenAI), then run "codex" once in a terminal to sign in.'},
    {'id': 'gemini', 'name': 'Gemini CLI', 'bin': 'gemini', 'tested': False, 'model_flag': '-m',
     'default': ['gemini', '-p', '{prompt}', '--approval-mode', 'auto_edit',
                 '--allowed-tools', 'run_shell_command(cutvelvet)',
                 '--include-directories', '{guide}', '-o', 'text'],
     'note': 'Untested template. The flags come from "gemini --help"; the tool name after --allowed-tools is a best guess. Use Test before relying on it. '
             'Unlike the Claude Code command, it is not known to be limited to the project folder or to ignore settings that come with a project '
             '(Gemini has a --sandbox flag you can add): look at a project you did not make before running it.',
     'install': 'Install Gemini CLI (from Google), then run "gemini" once in a terminal to sign in.'},
]
BY_ID = {k['id']: k for k in KNOWN}
MODEL_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:/\[\]@-]{0,79}$')
TEST_PROMPT = 'Reply with the single word ok. Do not use any tools and do not read or change any files.'


def guard(argv):
    """'' when this agent command may be started, else the reason it is blocked by the environment switches above."""
    if os.environ.get('CUTVELVET_AGENT_DISABLED', '').strip().lower() not in ('', '0', 'false', 'no'):
        return 'Agent starts are switched off here (CUTVELVET_AGENT_DISABLED is set).'
    only = os.environ.get('CUTVELVET_AGENT_ONLY', '').strip()
    if only:
        prog = which(argv[0]) if argv else None
        if not prog or os.path.realpath(prog) != os.path.realpath(os.path.expanduser(only)):
            return 'Agent starts are limited to %s here (CUTVELVET_AGENT_ONLY), and the configured agent is %s.' % (only, (argv or ['none'])[0])
    return ''


def which(exe):
    if not exe:
        return None
    path = os.pathsep.join([os.environ.get('PATH', '')] + [os.path.expanduser(p) for p in EXTRA_PATH])
    return shutil.which(os.path.expanduser(exe), path=path)


def _argv(v):
    """A command as a list of strings, from a list or a shell-style line; None when unusable."""
    if isinstance(v, str):
        try:
            v = shlex.split(v)
        except ValueError:
            return None
    if isinstance(v, list) and v and all(isinstance(x, str) and x for x in v) and len(v) <= 200:
        return list(v)
    return None


def text(argv):
    """The command as one readable, re-parseable line."""
    return ' '.join(shlex.quote(a) if a not in ('{prompt}', '{prompt_file}', '{project}', '{cutvelvet}', '{guide}') else a for a in argv or [])


def _digest(argv):
    return hashlib.sha256(json.dumps(argv).encode()).hexdigest()[:24]


def config():
    """The stored agent settings, cleaned: {choice, model, commands, approved}."""
    a = SETTINGS._read_json(SETTINGS.path()).get('agent')
    a = a if isinstance(a, dict) else {}
    choice = a.get('choice') if a.get('choice') in BY_ID or a.get('choice') == 'custom' else None
    model = a.get('model') if isinstance(a.get('model'), str) and MODEL_RE.match(a.get('model')) else ''
    commands = {}
    for k, v in (a.get('commands') or {}).items() if isinstance(a.get('commands'), dict) else []:
        if (k in BY_ID or k == 'custom') and _argv(v):
            commands[k] = _argv(v)
    approved = {k: v for k, v in (a.get('approved') or {}).items() if isinstance(k, str) and isinstance(v, str)} if isinstance(a.get('approved'), dict) else {}
    return {'choice': choice, 'model': model, 'commands': commands, 'approved': approved}


def _store(cfg):
    d = SETTINGS._read_json(SETTINGS.path())
    d['agent'] = {k: v for k, v in cfg.items() if v not in (None, '', {}, [])}
    SETTINGS._write_json(SETTINGS.path(), d)


def project_command(project_dir):
    """(argv or None, approved on this machine?) for the command a project.json asks for."""
    agent = L.load_project(project_dir).get('agent')
    argv = _argv(agent.get('command')) if isinstance(agent, dict) else None
    if not argv:
        return None, False
    return argv, config()['approved'].get(_key(project_dir)) == _digest(argv)


def _key(project_dir):
    return os.path.realpath(project_dir)   # the folder itself, however it was reached (a link to it is the same project)


def project_pending(project_dir):
    """True while a project asks for its own command and the user has neither approved nor declined that exact
    command on this machine. No agent is started in that state: quietly running some other agent instead (and
    billing for it) is not what the person who pressed Run was shown."""
    argv, ok = project_command(project_dir)
    return bool(argv) and not ok and config()['approved'].get(_key(project_dir)) != 'declined:' + _digest(argv)


def _with_model(argv, agent_id, model):
    flag = (BY_ID.get(agent_id) or {}).get('model_flag')
    if not (model and flag) or flag in argv or '--model' in argv:
        return list(argv)
    return list(argv) + [flag, model]


def resolve(project_dir):
    """What will actually be started: {id, source, argv, base, found, path}. ``argv`` is [] when nothing is set up.

    Order: a project's own command if (and only if) approved here; else this machine's choice; else the first
    known agent on PATH.
    """
    cfg = config()
    pargv, ok = project_command(project_dir)
    if pargv and ok:
        return {'id': 'project', 'source': 'project', 'argv': pargv, 'base': pargv, 'found': bool(which(pargv[0])), 'path': which(pargv[0])}
    choice = cfg['choice']
    if choice is None:
        choice = next((k['id'] for k in KNOWN if which(k['bin'])), None)
        source = 'auto'
    else:
        source = 'settings'
    if choice is None:
        return {'id': None, 'source': 'none', 'argv': [], 'base': [], 'found': False, 'path': None}
    base = cfg['commands'].get(choice) or list((BY_ID.get(choice) or {}).get('default') or [])
    argv = _with_model(base, choice, cfg['model']) if base else []
    p = which(argv[0]) if argv else None
    return {'id': choice, 'source': source, 'argv': argv, 'base': base, 'found': bool(p), 'path': p}


def describe(project_dir):
    """Everything the Agent settings section shows."""
    cfg, r = config(), resolve(project_dir)
    known = []
    for k in KNOWN:
        p = which(k['bin'])
        edited = cfg['commands'].get(k['id'])
        known.append({'id': k['id'], 'name': k['name'], 'bin': k['bin'], 'found': bool(p), 'path': p, 'tested': k['tested'], 'note': k['note'],
                      'install': k['install'], 'model_flag': k['model_flag'], 'default': text(k['default']), 'command': text(edited or k['default']),
                      'edited': bool(edited) and edited != k['default']})
    custom = cfg['commands'].get('custom') or []
    known.append({'id': 'custom', 'name': 'Custom command', 'bin': custom[0] if custom else '', 'found': bool(custom and which(custom[0])),
                  'path': which(custom[0]) if custom else None, 'tested': False, 'model_flag': None, 'default': '', 'command': text(custom), 'edited': False,
                  'note': 'Any command-line agent. Put {prompt} where the prompt goes (or {prompt_file} for a file holding it); with neither, '
                          'the prompt is sent on standard input. {project} is the project folder and {guide} the folder with the cutvelvet guide. '
                          'It runs in the project folder with "cutvelvet" on PATH.',
                  'install': ''})
    pargv, ok = project_command(project_dir)
    return {'choice': cfg['choice'], 'model': cfg['model'], 'known': known, 'any_found': any(k['found'] for k in known),
            'effective': {'id': r['id'], 'source': r['source'], 'command': text(r['argv']), 'found': r['found'], 'path': r['path'],
                          'name': (BY_ID.get(r['id']) or {}).get('name') or ('This project\'s own command' if r['id'] == 'project' else 'Custom command' if r['id'] == 'custom' else None)},
            'project': {'command': text(pargv), 'approved': ok, 'pending': project_pending(project_dir), 'digest': _digest(pargv)} if pargv else None,
            'settings_path': SETTINGS.path()}


def save(project_dir, data):
    """Apply {choice?, model?, command? (text for the chosen agent; "" = back to its default), approve_project? (bool)}."""
    cfg = config()
    if 'choice' in data:
        c = data.get('choice')
        if c not in BY_ID and c not in ('custom', None, ''):
            raise ValueError('unknown agent: %r' % c)
        cfg['choice'] = c or None
    if 'model' in data:
        m = (data.get('model') or '').strip()
        if m and not MODEL_RE.match(m):
            raise ValueError('that does not look like a model name (letters, digits, . - _ : / only)')
        cfg['model'] = m
    if 'command' in data:
        target = cfg['choice'] or resolve(project_dir)['id']
        if target not in BY_ID and target != 'custom':
            raise ValueError('choose an agent first')
        line = (data.get('command') or '').strip()
        if not line:
            cfg['commands'].pop(target, None)
        else:
            argv = _argv(line)
            if not argv:
                raise ValueError('that command could not be read (check the quotes)')
            if target in BY_ID and argv == BY_ID[target]['default']:
                cfg['commands'].pop(target, None)
            else:
                cfg['commands'][target] = argv
            cfg['choice'] = target  # editing a command is choosing it
    if 'approve_project' in data:
        pargv, _ = project_command(project_dir)
        key = _key(project_dir)
        shown = data.get('project_digest')   # when the page sends what it displayed, approve only that exact command
        if pargv and shown is not None and shown != _digest(pargv):
            raise ValueError("the project's command has changed; look at it again")
        if data.get('approve_project') and pargv:
            cfg['approved'][key] = _digest(pargv)
        elif pargv:                           # declined: remembered for this exact command, so the user's own agent is used
            cfg['approved'][key] = 'declined:' + _digest(pargv)
        else:
            cfg['approved'].pop(key, None)
    _store(cfg)
    return describe(project_dir)
