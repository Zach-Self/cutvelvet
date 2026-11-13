"""Standing rules: an editor's own preferences, kept per project in ``<project>/rules.json``.

    {"rules": [{"id": "no-repeats", "text": "Never use the same moment of footage twice.", "on": false,
                "kind": "preset", "group": "Cutting", "check": "audit"},
               {"id": "max-shot", "text": "No shot is longer than 8 seconds.", "on": false, "kind": "preset",
                "group": "Cutting", "template": "No shot is longer than {n} seconds.",
                "params": {"n": {"type": "number", "value": 8}}},
               {"id": "c-1a2b3c4d", "text": "Less shirt, more mouth.", "on": true, "kind": "custom", "group": "Look"}]}

Nothing here is a house style. PRESETS are neutral suggestions that every project starts with switched
OFF; a project with no rule switched on is perfectly valid. No rule is hard-wired into the tool either:
the one rule the tool can check by itself (``check: "audit"`` -- repeated footage) only makes
``cutvelvet audit`` / ``publish`` fail while it is switched on.

``text`` is always the full sentence. Presets with an editable part also carry ``template`` + ``params``
(``number`` or ``choice``; a choice accepts any text, ``options`` are only suggestions). ``note`` is an
optional line of detail under a rule.

A project without rules.json starts from the all-off presets plus the bullets of ``<project>/RULES.md``. Nothing
is written until the user changes a rule. The editor's saved personal defaults (``~/.cutvelvet/rules.json``) are
never applied silently: the Rules tab offers them as "Start from my defaults". Of RULES.md, a bullet that says the same as a preset switches that preset on (keeping the bullet as
its ``note``), any other bullet becomes an enabled custom rule grouped under its heading. RULES.md itself
is never modified; afterwards rules.json is the source of truth.
"""
import hashlib
import json
import os
import re
import uuid

from . import library as L

PRESETS = [
    {'id': 'no-repeats', 'group': 'Cutting', 'text': 'Never use the same moment of footage twice.', 'check': 'audit',
     'match': r'\b(no|never|not)\b.*\b(repeat|reus)|appears? (only )?once'},
    {'id': 'cut-on-music', 'group': 'Cutting', 'template': "Cuts land on the music's {unit} boundaries.",
     'params': {'unit': {'type': 'choice', 'value': 'beat', 'options': ['beat', 'bar', 'line', 'phrase']}},
     'match': r'\bcuts? (land|fall|sit)\b.*\b(?P<unit>beat|bar|line|phrase)'},
    {'id': 'max-shot', 'group': 'Cutting', 'template': 'No shot is longer than {n} seconds.',
     'params': {'n': {'type': 'number', 'value': 8}}, 'match': r'no shot (is |runs )?(longer|more) than (?P<n>\d+(\.\d+)?) ?s'},
    {'id': 'min-shot', 'group': 'Cutting', 'template': 'No shot is shorter than {n} seconds.',
     'params': {'n': {'type': 'number', 'value': 1}}, 'match': r'no shot (is |runs )?(shorter|less) than (?P<n>\d+(\.\d+)?) ?s'},
    {'id': 'people-treatment', 'group': 'Colour', 'template': 'People are shown in {treatment}.',
     'params': {'treatment': {'type': 'choice', 'value': 'black and white',
                              'options': ['black and white', 'full colour', 'a muted, desaturated grade', 'a warm grade', 'a cold grade']}},
     'match': r'(?=.*\b(people|faces?|skin|cast)\b).*(?P<treatment>black[ -]and[ -]white)'},
    {'id': 'accent-colours', 'group': 'Colour', 'template': 'Use no more than {n} accent colour(s) per timeline.',
     'params': {'n': {'type': 'number', 'value': 1}}, 'match': r'(?P<n>\d+) accent colou?r'},
    {'id': 'titles-only', 'group': 'Text', 'text': 'No on-screen text except title cards.',
     'match': r'no (on-?screen )?text except'},
    {'id': 'no-lyrics', 'group': 'Text', 'text': 'Do not put lyrics on screen.', 'match': r'^(no|do not put|never put) lyrics?( words)? on[- ]?screen\.?$'},
    {'id': 'always-show', 'group': 'Framing', 'template': 'Every shot includes {subject}.',
     'params': {'subject': {'type': 'choice', 'value': 'a face', 'options': ['a face', 'a pair of eyes', 'hands', 'a wide establishing view']}},
     'match': r'^every shot (includes|shows|has) (?P<subject>[^.,;:]{3,40})\.?$'},
    {'id': 'no-macro', 'group': 'Framing', 'text': 'No extreme close-ups.', 'match': r'^(no|avoid|never use) extreme (macro|close)[- ]?(up)?s?( shots)?\.?$'},
    {'id': 'off-centre', 'group': 'Framing', 'text': 'Keep faces off-centre.', 'match': r'faces? .*off-?cent'},
    {'id': 'max-panels', 'group': 'Framing', 'template': 'No more than {n} panels on screen at once.',
     'params': {'n': {'type': 'number', 'value': 3}}, 'match': r'no more than (?P<n>\d+) (panels|strips)'},
    {'id': 'same-cast', 'group': 'Cast', 'text': 'Keep the same stand-in for each character throughout.',
     'match': r'(stand-?ins?|actors?|cast)\b.*\bsame (people|person|actor|face)|same stand-?in'},
    {'id': 'library-only', 'group': 'Footage', 'text': 'Only use clips already in the library; do not download footage.',
     'match': r"(do not|don't|never|no) download"},
    {'id': 'pins-exact', 'group': 'Footage', 'text': 'Use pinned clips exactly as pinned: that clip, from that moment.',
     'match': r'pinned? .*exact'},
    {'id': 'minimal-change', 'group': 'Process', 'text': 'Change only what the notes ask for.',
     'match': r'(change|touch) only what'},
]
GROUP_ORDER = ['Cutting', 'Colour', 'Text', 'Framing', 'Cast', 'Footage', 'Process']
REPEATS_RULE = 'no-repeats'
_BY_ID = {p['id']: p for p in PRESETS}


def home_path():
    """The editor's personal defaults: ``~/.cutvelvet/rules.json`` ($CUTVELVET_HOME overrides the folder)."""
    return os.path.join(os.environ.get('CUTVELVET_HOME') or os.path.join(os.path.expanduser('~'), '.cutvelvet'), 'rules.json')


def path(project_dir):
    return os.path.join(project_dir, 'rules.json')


# ---------------------------------------------------------------- shaping

def _fmt(v):
    if isinstance(v, float) and v == int(v):
        v = int(v)
    return str(v)


def render(template, params):
    return re.sub(r'\{(\w+)\}', lambda m: _fmt((params.get(m.group(1)) or {}).get('value', m.group(0))), template)


def preset_rule(p, on=False):
    r = {'id': p['id'], 'text': p.get('text', ''), 'on': bool(on), 'kind': 'preset', 'group': p['group']}
    if p.get('check'):
        r['check'] = p['check']
    if p.get('template'):
        r['template'] = p['template']
        r['params'] = json.loads(json.dumps(p['params']))
        r['text'] = render(r['template'], r['params'])
    return r


def _clean(s, n=1000):
    return ' '.join(str(s or '').split())[:n]


def normalize(rules):
    """Whatever came from disk or the page -> a well-formed rule list (presets keep their fixed wording)."""
    out, seen = [], set()
    for r in rules if isinstance(rules, list) else []:
        if not isinstance(r, dict):
            continue
        rid = re.sub(r'[^A-Za-z0-9_-]', '', str(r.get('id') or ''))[:40]
        p = _BY_ID.get(rid) if r.get('kind') == 'preset' else None
        if p:
            n = preset_rule(p, r.get('on') is True)
            for k, spec in (n.get('params') or {}).items():
                v = ((r.get('params') or {}).get(k) or {}).get('value') if isinstance(r.get('params'), dict) and isinstance((r.get('params') or {}).get(k), dict) else None
                if spec['type'] == 'number':
                    if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v < 1e6:
                        spec['value'] = v
                elif _clean(v, 120):
                    spec['value'] = _clean(v, 120)
            if n.get('template'):
                n['text'] = render(n['template'], n['params'])
        else:
            text = _clean(r.get('text'))
            if not text:
                continue
            if not rid or rid in _BY_ID:
                rid = 'c-' + uuid.uuid4().hex[:8]
            n = {'id': rid, 'text': text, 'on': r.get('on') is not False, 'kind': 'custom', 'group': _clean(r.get('group'), 60) or 'Your rules'}
        if _clean(r.get('note')):
            n['note'] = _clean(r.get('note'))
        if n['id'] in seen:
            continue
        seen.add(n['id'])
        out.append(n)
    out += [preset_rule(p) for p in PRESETS if p['id'] not in seen]  # presets added by a newer cutvelvet show up, off
    return out


# ---------------------------------------------------------------- RULES.md import

def parse_md(text):
    """[(group, bullet text)] from a markdown file of headed bullet lists."""
    group, items, cur = '', [], None
    for line in text.splitlines():
        s = line.strip()
        h = re.match(r'^#{1,6}\s+(.*)$', s)
        b = re.match(r'^[-*+]\s+(.*)$', s)
        if h:
            cur = None
            if line.startswith('##'):
                group = h.group(1).strip()
        elif b:
            cur = [group, b.group(1)]
            items.append(cur)
        elif s and cur is not None and line[:1] in (' ', '\t'):
            cur[1] += ' ' + s
        else:
            cur = None
    return [(g, _clean(t)) for g, t in items if _clean(t)]


def import_md(rules, text):
    """Fold RULES.md bullets into a rule list: same-meaning presets are switched on, the rest become custom rules."""
    by_id = {r['id']: r for r in rules}
    used = set()
    for group, bullet in parse_md(text):
        hit = None
        for p in PRESETS:
            m = p['id'] not in used and re.search(p['match'], bullet, re.I)
            if m:
                hit = (p, m)
                break
        if hit and hit[0]['id'] in by_id:
            p, m = hit
            r = by_id[p['id']]
            used.add(p['id'])
            r['on'] = True
            r['note'] = bullet
            for k, v in m.groupdict().items():
                if v and k in (r.get('params') or {}):
                    spec = r['params'][k]
                    spec['value'] = float(v) if spec['type'] == 'number' else v.lower().replace('-', ' ')
            if r.get('template'):
                r['text'] = render(r['template'], r['params'])
        else:
            # a stable id (not random): until rules.json is written these are rebuilt on every load
            rid = 'c-' + hashlib.sha1(('%s|%s' % (group, bullet)).encode()).hexdigest()[:8]
            rules.append({'id': rid, 'text': bullet, 'on': True, 'kind': 'custom', 'group': group or 'Your rules'})
    return rules


# ---------------------------------------------------------------- load / save

def _read(p):
    try:
        with open(p, encoding='utf-8') as f:
            d = json.load(f)
        return d.get('rules') if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def seed(project_dir, defaults=False):
    """The rule list a project starts with: the presets, all off, plus the project's own RULES.md if it has one.
    With ``defaults`` the editor's personal defaults (~/.cutvelvet/rules.json) are the starting point instead of the
    bare presets -- only ever on request, never silently. Does not write anything."""
    rules = normalize((_read(home_path()) or []) if defaults else [])
    try:
        with open(os.path.join(project_dir, 'RULES.md'), encoding='utf-8') as f:
            rules = import_md(rules, f.read())
    except OSError:
        pass
    return normalize(rules)


def load(project_dir, write=False):
    """The project's rules. Until the user changes something there is no rules.json and this is the seed (presets
    off, plus RULES.md); nothing is written just because a project was opened. ``write`` is kept for callers that
    explicitly want the seed stored."""
    raw = _read(path(project_dir))
    if raw is not None:
        return normalize(raw)
    rules = seed(project_dir)
    if write:
        L.write_json(path(project_dir), {'rules': rules})
    return rules


def defaults_info():
    """{path, exists, on}: the editor's personal defaults and how many of them are switched on."""
    raw = _read(home_path())
    return {'path': home_path(), 'exists': raw is not None, 'on': sum(1 for r in normalize(raw or []) if r['on']) if raw is not None else 0}


def start_from_defaults(project_dir):
    """The user asked for it: make this project's rules their personal defaults (plus the project's RULES.md)."""
    rules = seed(project_dir, defaults=True)
    L.write_json(path(project_dir), {'rules': rules})
    return rules


def save(project_dir, rules):
    rules = normalize(rules)
    L.write_json(path(project_dir), {'rules': rules})
    return rules


def save_defaults(project_dir):
    """Copy the project's current rule set to the editor's personal defaults for new projects."""
    rules = load(project_dir)
    L.write_json(home_path(), {'rules': rules})
    return home_path()


def is_on(project_dir, rule_id):
    return any(r['id'] == rule_id and r['on'] for r in load(project_dir, write=False))


def as_text(project_dir, write=False):
    """The enabled rules as a block of prompt text."""
    on = [r for r in load(project_dir, write=write) if r['on']]
    if not on:
        return 'This project has no standing rules: nothing beyond the notes constrains the edit.\n'
    lines = ["The editor's standing rules for this project. They apply to everything you do and override your own taste:"]
    group = None
    for r in sorted(on, key=lambda r: (GROUP_ORDER.index(r['group']) if r['group'] in GROUP_ORDER else len(GROUP_ORDER), r['group'])):
        if r['group'] != group:
            group = r['group']
            lines.append('%s' % group)
        note = r.get('note')
        lines.append('- %s%s' % (r['text'], ' (%s)' % note if note and note != r['text'] else ''))
    return '\n'.join(lines) + '\n'
