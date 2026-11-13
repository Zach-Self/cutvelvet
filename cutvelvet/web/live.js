'use strict';
/* cutvelvet live preview.
   Geom is a port of the geometry / timing maths in cutvelvet/engine.py (Reader, draw_panel, trans, draw_text).
   It has no DOM dependencies so it can be checked against the engine from node (see tools). */
const Geom = (() => {
  const clamp = (x, a = 0, b = 1) => Math.max(a, Math.min(b, x));
  const lerp = (a, b, u) => a + (b - a) * u;
  const smooth = u => { u = clamp(u); return u * u * (3 - 2 * u); };
  const eout = u => { u = clamp(u); return 1 - Math.pow(1 - u, 3); };
  const pyRound = v => {                       // Python 3 round(): half to even
    const f = Math.floor(v), d = v - f;
    if (d < 0.5) return f;
    if (d > 0.5) return f + 1;
    return f % 2 === 0 ? f : f + 1;
  };
  const rectOf = p => p.rect.map(pyRound);

  /* engine.Reader: the static super-region of the source that is decoded for a panel */
  function superRegion(p, sw, sh, M) {
    const [x, y, w, h] = rectOf(p);
    const [cx, cy, wf] = p.region;
    let rw = wf * sw * M, rh = rw * h / w;
    const s = Math.min(1, sw / rw, sh / rh); rw *= s; rh *= s;
    const x0 = clamp(cx * sw - rw / 2, 0, sw - rw), y0 = clamp(cy * sh - rh / 2, 0, sh - rh);
    const q = p.q === undefined ? 1 : p.q;
    const dw = Math.floor(Math.trunc(w * M * q) / 2) * 2;
    const dh = Math.floor(Math.trunc(dw * rh / rw) / 2) * 2;
    return { x, y, w, h, dw, dh, x0: Math.trunc(x0), y0: Math.trunc(y0), rw: Math.trunc(rw), rh: Math.trunc(rh), fx0: x0, fy0: y0, frw: rw, frh: rh };
  }

  /* engine.draw_panel: the box of the decoded frame shown at time t, mapped back to source pixels */
  function view(p, sw, sh, t, M) {
    const R = superRegion(p, sw, sh, M);
    const u = clamp((t - p.t0) / (p.t1 - p.t0));
    const zm = p.zoom || [1.0, 1.08], z = lerp(zm[0], zm[1], u);
    const pan = p.pan || [[0, 0], [0, 0]];
    const px = lerp(pan[0][0], pan[1][0], u), py = lerp(pan[0][1], pan[1][1], u);
    let vw = Math.min(R.dw, R.dw / z), vh = vw * R.h / R.w;
    if (vh > R.dh) { vh = R.dh; vw = vh * R.w / R.h; }
    const sx = (R.dw - vw) / 2, sy = (R.dh - vh) / 2;
    const bx = sx + px * sx, by = sy + py * sy;
    const kx = R.rw / R.dw, ky = R.rh / R.dh;
    const flip = !!p.flipx;
    const crop = { x: R.x0 + (flip ? R.dw - bx - vw : bx) * kx, y: R.y0 + by * ky, w: vw * kx, h: vh * ky };
    return { R, u, z, px, py, box: [bx, by, bx + vw, by + vh], crop, flip };
  }

  /* inverse of view() for editing: the region that makes the panel show `want` ({cx, cy, w} in source px) at time t */
  function regionFor(p, sw, sh, t, M, want) {
    const v = view(p, sw, sh, t, M), R = v.R, c = v.crop;
    const offx = ((c.x + c.w / 2) - (R.x0 + R.rw / 2)) / R.rw, offy = ((c.y + c.h / 2) - (R.y0 + R.rh / 2)) / R.rh;
    const wfMax = Math.min(1 / M, sh * R.w / (R.h * sw * M));
    // a pure move keeps the width fraction exactly as written; a resize scales the effective one
    const wf = Math.abs(want.w - c.w) < 1e-6 ? Math.min(p.region[2], wfMax) : clamp(R.frw / (sw * M) * want.w / c.w, 0.02, wfMax);
    const rw = wf * sw * M, rh = rw * R.h / R.w;
    const ccx = clamp(want.cx - offx * rw, rw / 2, sw - rw / 2), ccy = clamp(want.cy - offy * rh, rh / 2, sh - rh / 2);
    const r4 = n => Math.round(n * 1e4) / 1e4;
    return [r4(ccx / sw), r4(ccy / sh), wf === p.region[2] ? wf : r4(wf)];
  }

  function trans(kind, u) {
    if (kind === 'lid') return [eout(u), 1, 0, 1];
    if (kind === 'wipeL') return [1, eout(u), -1, 1];
    if (kind === 'wipeR') return [1, eout(u), 1, 1];
    if (kind === 'fade') return [1, 1, 0, smooth(u)];
    return [1, 1, 0, 1];
  }

  /* visible part of the panel rect (canvas px, relative to the rect) and opacity; null when nothing is drawn */
  function reveal(p, t) {
    const [, , w, h] = rectOf(p);
    const tin = p.tin || ['lid', 0.5], tout = p.tout || ['cut', 0];
    let r = [1, 1, 0, 1];
    if (tin[1] > 0 && t - p.t0 < tin[1]) r = trans(tin[0], (t - p.t0) / tin[1]);
    if (tout[1] > 0 && p.t1 - t < tout[1]) r = trans(tout[0], (p.t1 - t) / tout[1]);
    const op = r[3] * (p.op === undefined ? 1 : p.op);
    const vhh = Math.max(0, pyRound(h * r[0])), vww = Math.max(0, pyRound(w * r[1]));
    if (vhh < 1 || vww < 1 || op <= 0) return null;
    const yo = Math.floor((h - vhh) / 2);
    let xo = r[2] <= 0 ? 0 : w - vww;
    if (r[2] === 0) xo = Math.floor((w - vww) / 2);
    return { xo, yo, vww, vhh, op, hv: r[0], wv: r[1], wd: r[2] };
  }

  const srcTime = (p, t) => (p.ss || 0) + Math.max(0, t - p.t0) * (p.speed === undefined ? 1 : p.speed);
  const srcRange = p => { const a = p.ss || 0; return [a, a + (p.t1 - p.t0) * (p.speed === undefined ? 1 : p.speed)]; };

  /* engine.draw_text: per-character alpha / rise and the tracking in px */
  function textState(tx, t) {
    const fin = tx.fade === undefined ? 0.6 : tx.fade, fo = tx.fadeout === undefined ? fin : tx.fadeout;
    const grow = (1 - eout((t - tx.t0) / Math.max(fin * 2.2, 1e-3))) * (tx.spread || 0);
    const st = tx.stagger || 0, chars = [];
    for (let i = 0; i < tx.text.length; i++) {
      const a = smooth((t - tx.t0 - i * st) / fin) * smooth((tx.t1 - t) / fo);
      chars.push({ a: a > 0.004 ? a : 0, dy: (1 - eout((t - tx.t0 - i * st) / (fin * 1.4))) * (tx.rise || 0) });
    }
    return { trk: (tx.track || 0) + grow, chars };
  }

  /* timeline fx (timeline.build_fx): {fade, flash, shred} values at t */
  function fx(spec, dur, t) {
    const out = { fade: 1, flash: 0, shred: 0 };
    if (spec.fade) {
      const fi = spec.fade.in || 0, fo = spec.fade.out || 0;
      out.fade = (fi > 0 ? smooth(t / fi) : 1) * (fo > 0 ? smooth((dur - t) / fo) : 1);
    }
    for (const k of ['flash', 'shred']) {
      if (!spec[k]) continue;
      for (const q of (Array.isArray(spec[k]) ? spec[k] : [spec[k]])) {
        const d = t - q.t;
        if (d >= 0 && d < q.dur) out[k] += q.amp * Math.exp(-d * q.decay);
      }
    }
    return out;
  }

  /* engine.grade tone curve for one channel value v (display 0..1), treating the pixel as neutral (exact for bw).
     The highlight glow is folded in using the pixel's own value instead of the blurred neighbourhood. */
  function tone(G, expo, ch, v) {
    const sh = G[0], hi = G[1], con = G[3];
    let x = Math.pow(v, 2.2) * expo;
    const ll = Math.sqrt(clamp(x));
    x = x * (sh[ch] * (1 - ll) + hi[ch] * ll);
    x = Math.pow(clamp(x, 0, 4), 1 / 2.2);
    x = clamp((x - 0.42) * con + 0.42 - 0.035);
    x = x * x * (3 - 2 * x) * 0.35 + x * 0.65;
    return clamp(1 - (1 - x) * (1 - 0.22 * x * x));
  }

  return { clamp, lerp, smooth, eout, pyRound, rectOf, superRegion, view, regionFor, trans, reveal, srcTime, srcRange, textState, fx, tone };
})();
if (typeof module !== 'undefined') module.exports = Geom;

if (typeof document !== 'undefined') (() => {
  const $ = id => document.getElementById(id);
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; };
  const { clamp, pyRound } = Geom;
  const EDIT = ['src', 'ss', 'region', 'grade', 'expo', 'speed'];
  const LOOK = 3.0;                           // seconds ahead that panels are loaded and parked on their first frame
  const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
  const clone = o => JSON.parse(JSON.stringify(o));
  const base = s => (s || '').split('/').pop();
  const fmt = (n, d = 2) => (Math.round(n * 10 ** d) / 10 ** d).toFixed(d);

  const EMBED = new URLSearchParams(location.search).has('embed');
  const S = { cfg: null, tid: null, tl: null, saved: null, rev: null, fps: 24, W: 1920, H: 1080, M: 1.16, t: 0, playing: false, loop: false,
    P: [], sel: null, undo: [], redo: [], clips: {}, lib: [], used: {}, history: 0, scale: 1, outline: true, gq: 'svg', gesture: 0, curShot: null };
  window.cutvelvetLive = S;                     // handy in the console and for tests
  S.Geom = Geom;

  const api = async (path, body) => {
    const r = await fetch(path, body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    let d = null;
    try { d = await r.json(); } catch (e) { /* not json */ }
    if (!r.ok) { const e = new Error((d && d.error) || ('HTTP ' + r.status)); e.data = d; e.status = r.status; throw e; }
    return d;
  };
  const fail = e => { $('err').textContent = e ? (e.message || String(e)) + (e.data && e.data.errors ? '\n' + e.data.errors.join('\n') : '') : ''; };

  // ───────────── clock (the timeline's audio is the master) ─────────────
  const audio = new Audio(); audio.preload = 'auto';
  let audioOK = false, useAudio = false, perf0 = 0, perfT = 0;
  audio.addEventListener('canplay', () => { audioOK = true; });
  audio.addEventListener('error', () => { audioOK = false; });
  /* a steady clock that follows the audio: extrapolate with performance.now() and steer towards audio.currentTime
     (some browsers report it in coarse steps); re-anchor outright when they disagree by more than 60 ms */
  let held = 0, rolling = false;              // rolling: the clock is actually advancing (not waiting for the audio to start)
  const now = () => {
    const n = performance.now(); let t = perfT + (n - perf0) / 1000;
    if (useAudio) {
      if (audio.paused || audio.seeking || audio.readyState < 3) {      // audio not running yet: the picture waits for it
        if (n - held > 6000) { useAudio = false; audio.pause(); }        // ...but not forever
        perf0 = n; rolling = false; return perfT;
      }
      held = n;
      const e = audio.currentTime - t;
      t = Math.abs(e) > 0.06 ? audio.currentTime : t + e * 0.1;
    }
    perf0 = n; perfT = t; rolling = true; return t;
  };
  const frame = t => Math.floor(t * S.fps + 1e-6) / S.fps;
  const lastT = () => S.tl ? (Math.round(S.tl.duration * S.fps) - 1) / S.fps : 0;

  function play() {
    if (!S.tl || S.playing) return;
    if (S.t >= lastT() - 1e-6) S.t = 0;
    S.playing = true; perf0 = held = performance.now(); perfT = S.t; useAudio = false;
    if (audioOK) {
      try { audio.currentTime = S.t; } catch (e) { /* not seekable yet */ }
      useAudio = true;
      audio.play().catch(() => { useAudio = false; perf0 = performance.now(); perfT = S.t; });
    }
    $('play').textContent = 'Pause';
  }
  function pause() {
    if (!S.playing) return;
    S.playing = false; audio.pause(); S.t = frame(S.t);
    $('play').textContent = 'Play';
  }
  function seek(t) {
    if (!S.tl) return;
    S.t = clamp(S.playing ? t : Math.round(t * S.fps) / S.fps, 0, lastT());
    perf0 = performance.now(); perfT = S.t;
    if (S.playing && useAudio) { held = performance.now(); try { audio.currentTime = S.t; } catch (e) { /* ignore */ } }
  }
  let audioUrl = null, audioFor = '';
  function loadAudio(tid, rev) {              // the mix is fetched whole so seeking it is instant
    const want = tid + '@' + rev; audioFor = want; audioOK = false;
    audio.removeAttribute('src'); audio.load();
    fetch('/api/timeline/' + encodeURIComponent(tid) + '/audio').then(r => { if (!r.ok) throw new Error('no audio'); return r.blob(); }).then(bl => {
      if (audioFor !== want) return;
      if (audioUrl) URL.revokeObjectURL(audioUrl);
      audioUrl = URL.createObjectURL(bl); audio.src = audioUrl; audio.load();
    }).catch(() => { $('pxmsg').dataset.audio = 'none'; });
  }
  S.clock = () => ({ useAudio, rolling, audioOK });
  audio.addEventListener('ended', () => { if (S.playing) { pause(); S.t = lastT(); } });

  // ───────────── model ─────────────
  const shotFrames = s => [Math.round(s.t0 * S.fps), Math.round(s.t1 * S.fps)];
  function shotAt(t) {
    const n = Math.round(t * S.fps);
    for (const s of S.tl.shots) { const [a, b] = shotFrames(s); if (a <= n && n < b) return s; }
    return S.tl.shots[S.tl.shots.length - 1];
  }
  function index() {                          // flatten in shot order; stable sort by z gives the engine's draw order
    S.P = [];
    S.tl.shots.forEach((shot, si) => (shot.panels || []).forEach((p, pi) => S.P.push({ p, shot, si, pi, key: shot.id + '#' + pi })));
    S.P.slice().sort((a, b) => (a.p.z || 0) - (b.p.z || 0)).forEach((P, i) => { P.rank = i + 1; });
    S.X = [];
    S.tl.shots.forEach(shot => (shot.texts || []).forEach((x, xi) => S.X.push({ x, key: shot.id + '~' + xi })));
  }
  const find = key => S.P.find(P => P.key === key);
  const selP = () => S.sel && find(S.sel);
  const savedPanel = P => S.saved.shots[P.si].panels[P.pi];
  const pick = p => { const o = {}; for (const k of EDIT) if (p[k] !== undefined) o[k] = clone(p[k]); return o; };
  function edits() {
    const out = [];
    for (const P of S.P) {
      const a = savedPanel(P), set = {};
      for (const k of EDIT) if (!same(a[k], P.p[k])) set[k] = P.p[k];
      if (Object.keys(set).length) out.push({ shot: P.shot.id, panel: P.pi, set });
    }
    return out;
  }

  function put(P, patch) { for (const k in patch) { if (patch[k] === undefined) delete P.p[k]; else P.p[k] = clone(patch[k]); } }
  /* apply an edit to a panel; edits sharing `tag` (one drag, one slider gesture) collapse into one undo step */
  function edit(P, patch, tag) {
    const before = {}; let changed = false;
    for (const k in patch) { before[k] = P.p[k] === undefined ? undefined : clone(P.p[k]); if (!same(before[k], patch[k])) changed = true; }
    if (!changed) return;
    const top = S.undo[S.undo.length - 1];
    if (top && tag && top.tag === tag && top.key === P.key) { for (const k in patch) { if (!(k in top.before)) top.before[k] = before[k]; top.after[k] = clone(patch[k]); } }
    else S.undo.push({ key: P.key, tag, before, after: clone(patch) });
    S.redo = [];
    put(P, patch);
    if ('src' in patch) ensureProxies([patch.src], patch.src);
    refresh();
  }
  function undo() { const e = S.undo.pop(); if (!e) return; const P = find(e.key); if (P) put(P, e.before); S.redo.push(e); select(e.key, true); refresh(); }
  function redo() { const e = S.redo.pop(); if (!e) return; const P = find(e.key); if (P) put(P, e.after); S.undo.push(e); select(e.key, true); refresh(); }

  function refresh() {                        // everything that depends on the edit state (not on the playhead)
    const n = edits().length;
    $('saved').textContent = !S.tl ? '—' : n ? `unsaved changes · ${n} panel${n > 1 ? 's' : ''}` : 'saved';
    $('saved').classList.toggle('dirty', n > 0);
    $('save').disabled = !n; $('revert').disabled = !n;
    $('undo').disabled = !S.undo.length; $('redo').disabled = !S.redo.length;
    $('undoSave').disabled = !S.history || n > 0;
    $('hist').textContent = S.history ? `${S.history} undo cop${S.history > 1 ? 'ies' : 'y'} on disk` : 'no undo copies yet';
    document.title = (n ? '• ' : '') + 'cutvelvet — Live';
    const dirty = new Set(edits().map(e => e.shot));
    for (const seg of $('bar').querySelectorAll('.seg')) seg.classList.toggle('edited', dirty.has(seg.dataset.id));
    drawList(false); drawControls(); tell();
  }

  // ───────────── loading / saving ─────────────
  function adopt(d) {                          // a payload from GET/POST /api/timeline/<id>
    S.tl = d.timeline; S.saved = clone(d.timeline); S.rev = d.rev; S.history = d.history || 0;
    index();
    if (S.sel && !find(S.sel)) S.sel = null;
  }
  async function open(tid, at) {
    fail(); pause();
    const d = await api('/api/timeline/' + encodeURIComponent(tid));
    S.tid = tid; S.undo = []; S.redo = []; S.sel = null; S.curShot = null;
    adopt(d);
    for (const N of nodes.values()) drop(N);
    nodes.clear(); $('texts').textContent = ''; tnodes.clear();
    loadAudio(tid, d.rev);
    S.t = 0; seek(at || 0);
    buildBar(); refresh();
    if (d.errors && d.errors.length) fail(new Error('This timeline has errors:\n' + d.errors.join('\n')));
    history.replaceState(null, '', '?' + (EMBED ? 'embed=1&' : '') + 'tl=' + encodeURIComponent(tid));
    const srcs = []; for (const P of S.P) if (!srcs.includes(P.p.src)) srcs.push(P.p.src);
    ensureProxies(srcs);
    api('/api/used').then(u => { S.used = u || {}; }).catch(() => {});
  }
  async function save() {
    const e = edits(); if (!e.length) return;
    fail(); $('save').disabled = true;
    try {
      const d = await api('/api/timeline/' + encodeURIComponent(S.tid), { base_rev: S.rev, edits: e });
      adopt(d); refresh();
      api('/api/used').then(u => { S.used = u || {}; }).catch(() => {});
    } catch (x) {
      if (x.status === 409) x.message += ' — someone else saved it. Reload the page to pick up their version (your unsaved edits here will be lost).';
      fail(x); refresh();
    }
  }
  function revert() {
    if (!edits().length || !confirm('Discard every unsaved change in this timeline?')) return;
    const patch = S.P.map(P => ({ P, back: savedPanel(P) })).filter(o => EDIT.some(k => !same(o.back[k], o.P.p[k])));
    S.gesture++;
    for (const o of patch) { const b = {}; for (const k of EDIT) b[k] = o.back[k]; put(o.P, b); }
    S.undo = []; S.redo = []; refresh();
  }
  async function undoSave() {
    if (!confirm('Put the timeline file back to how it was before the last save?')) return;
    fail();
    try { const d = await api('/api/timeline/' + encodeURIComponent(S.tid) + '/undo', {}); S.undo = []; S.redo = []; adopt(d); refresh(); }
    catch (x) { fail(x); }
  }

  // ───────────── proxies ─────────────
  let pollTimer = 0;
  async function ensureProxies(srcs, first) {
    try {
      const d = await api('/api/timeline/proxy', { srcs, ensure: true, first });
      Object.assign(S.clips, d.clips); drawControls();
    } catch (e) { fail(e); }
    proxyUI(); warm();
  }
  function proxyUI() {
    const want = []; for (const P of S.P) if (!want.includes(P.p.src)) want.push(P.p.src);
    const st = want.map(s => (S.clips[s] || {}).state);
    const pending = want.filter((s, i) => st[i] === 'queued' || st[i] === 'running' || st[i] === undefined);
    const ready = st.filter(x => x === 'ready').length, bad = want.filter((s, i) => st[i] === 'error' || st[i] === 'missing');
    $('pxbar').hidden = !pending.length;
    const part = pending.reduce((a, s) => a + ((S.clips[s] || {}).progress || 0), 0);
    $('pxbar').firstElementChild.style.width = (want.length ? (ready + part) / want.length * 100 : 0) + '%';
    $('pxmsg').textContent = pending.length ? `Preparing preview proxies: ${ready} of ${want.length} clips ready. Panels show a placeholder until theirs is done.`
      : bad.length ? `No preview for ${bad.map(base).join(', ')} (${(S.clips[bad[0]] || {}).error || 'missing'})` : '';
    clearTimeout(pollTimer);
    if (pending.length) pollTimer = setTimeout(() => ensureProxies(pending), 1200);
  }

  /* proxies are small, so each one is fetched whole and played from memory: seeks are instant and a shot full of
     panels cannot run into the browser's six-connections-per-host limit */
  const blobs = new Map(); let blobBytes = 0; const BLOB_MAX = 900e6;
  function media(url) {
    let b = blobs.get(url);
    if (!b) {
      b = { state: 'loading', size: 0 }; blobs.set(url, b);
      fetch(url).then(r => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.blob(); })
        .then(bl => { b.obj = URL.createObjectURL(bl); b.size = bl.size; blobBytes += bl.size; b.state = 'ready'; trim(); })
        .catch(() => { b.obj = url; b.state = 'ready'; });          // fall back to streaming it
    }
    b.last = performance.now();
    return b.state === 'ready' ? b.obj : null;
  }
  function trim() {
    if (blobBytes < BLOB_MAX) return;
    const live = new Set([...nodes.values()].map(N => N.url)); live.add(svUrl);
    const old = [...blobs.entries()].filter(([, b]) => b.state === 'ready' && b.size && !live.has(b.obj)).sort((a, b) => a[1].last - b[1].last);
    for (const [u, b] of old) { if (blobBytes < BLOB_MAX * 0.7) break; URL.revokeObjectURL(b.obj); blobBytes -= b.size; blobs.delete(u); }
  }
  let warmTimer = 0;
  function warm() {                            // fetch the open timeline's proxies in the background, two at a time, in cut order
    clearTimeout(warmTimer);
    let loading = 0; for (const b of blobs.values()) if (b.state === 'loading') loading++;
    for (const P of S.P) {
      if (loading >= 2 || blobBytes > BLOB_MAX * 0.6) break;
      const c = S.clips[P.p.src];
      if (c && c.state === 'ready' && !blobs.has(c.url)) { media(c.url); loading++; }
    }
    if (S.P.some(P => { const c = S.clips[P.p.src]; return !c || c.state !== 'ready' || (blobs.get(c.url) || {}).state !== 'ready'; })) warmTimer = setTimeout(warm, 400);
  }

  // ───────────── grades ─────────────
  const filters = new Set();
  function gradeFilter(name, expo) {
    const G = S.cfg.grades[name] || S.cfg.grades.bw;
    if (S.gq === 'off') return '';
    if (S.gq === 'css') return `${G[2] === 0 ? 'grayscale(1)' : `saturate(${G[2]})`} contrast(${G[3]}) brightness(${Math.pow(expo, 1 / 2.2).toFixed(3)})`;
    const id = 'g_' + name + '_' + Math.round(expo * 100);
    if (!filters.has(id)) {
      filters.add(id);
      const NS = 'http://www.w3.org/2000/svg';
      const f = document.createElementNS(NS, 'filter');
      f.setAttribute('id', id); f.setAttribute('color-interpolation-filters', 'sRGB');
      f.setAttribute('x', '0'); f.setAttribute('y', '0'); f.setAttribute('width', '100%'); f.setAttribute('height', '100%');
      const m = document.createElementNS(NS, 'feColorMatrix'); m.setAttribute('type', 'saturate'); m.setAttribute('values', String(G[2])); f.appendChild(m);
      const ct = document.createElementNS(NS, 'feComponentTransfer');
      ['R', 'G', 'B'].forEach((c, ch) => {
        const fn = document.createElementNS(NS, 'feFunc' + c), tv = [];
        for (let i = 0; i < 65; i++) tv.push(Geom.tone(G, expo, ch, i / 64).toFixed(4));
        fn.setAttribute('type', 'table'); fn.setAttribute('tableValues', tv.join(' ')); ct.appendChild(fn);
      });
      f.appendChild(ct); $('defs').appendChild(f);
    }
    return `url(#${id})`;
  }
  function vignette(v) {                      // engine: x *= 1 - vig * 2 * 1.15^2 * r^2, r^2 = dx^2 + dy^2 in panel fractions
    if (!v || S.gq === 'off') return 'none';
    const stops = [];
    for (let i = 0; i <= 8; i++) { const f = i / 8; stops.push(`rgba(0,0,0,${clamp(v * 1.3225 * f * f).toFixed(4)}) ${(f * 100).toFixed(1)}%`); }
    return `radial-gradient(ellipse farthest-corner at center, ${stops.join(', ')})`;
  }

  // ───────────── stage ─────────────
  const nodes = new Map(), tnodes = new Map();
  S.nodes = nodes; S.audio = audio;
  function make(P) {
    const N = { el: el('div', 'pn'), v: el('video'), vg: el('div', 'vg'), ph: el('div', 'ph'), url: null, css: {} };
    N.v.muted = true; N.v.playsInline = true; N.v.preload = 'auto'; N.v.disableRemotePlayback = true;
    N.el.append(N.v, N.vg, N.ph); N.el.style.display = 'none';
    $('panels').appendChild(N.el); nodes.set(P.key, N); return N;
  }
  function drop(N) { try { N.v.pause(); N.v.removeAttribute('src'); N.v.load(); } catch (e) { /* ignore */ } N.el.remove(); }
  const setCss = (N, k, v) => { if (N.css[k] !== v) { N.css[k] = v; N.el.style[k] = v; } };

  /* keep a <video> on source time `tau`, playing at `rate` when the timeline plays. Small drift is corrected by
     nudging the playback rate, large drift (and every paused frame) by seeking. */
  function sync(v, tau, rate, playing, dur, fps) {
    if (v.readyState < 1) return;
    // ffmpeg's -ss starts on the first frame at or after the time; a browser shows the frame before it. Aim just
    // short of one source frame later so both land on the same frame.
    if (fps > 0) tau += 1 / fps - 0.004;
    const end = (dur || v.duration || 1e9) - 0.04;
    tau = clamp(tau, 0, Math.max(0, end));
    if (!playing || tau >= end) {
      if (!v.paused) v.pause();
      if (!v.seeking && Math.abs(v.currentTime - tau) > 0.012) v.currentTime = tau;
      return;
    }
    const e = (v.currentTime - tau) / rate;                    // error in timeline seconds
    if (!v.seeking && Math.abs(e) > 0.25) { v.currentTime = tau + 0.06 * rate; }
    const r = clamp(rate * (1 - clamp(e * 2.5, -0.5, 0.5)), 0.0625, 16);
    if (Math.abs(v.playbackRate - r) > 0.004) v.playbackRate = r;
    if (v.paused && !v.seeking) v.play().catch(() => {});
  }

  function drawPanel(P, tq, t) {
    const p = P.p, active = p.t0 <= tq && tq < p.t1, near = tq >= p.t0 - LOOK && tq < p.t1;
    let N = nodes.get(P.key);
    if (!near) { if (N) { drop(N); nodes.delete(P.key); } return; }
    if (!N) N = make(P);
    const c = S.clips[p.src], url = c && c.state === 'ready' ? media(c.url) : null, ready = !!url;
    if (ready && N.url !== url) { N.url = url; N.v.src = url; N.v.load(); }
    if (!ready && N.url) { N.url = null; N.v.removeAttribute('src'); N.v.load(); }
    const speed = p.speed === undefined ? 1 : p.speed;
    const rv = active ? Geom.reveal(p, tq) : null;
    if (!rv) {
      setCss(N, 'display', 'none');
      // parked on its first frame, decoded and waiting; in the last moments before its cue it starts rolling so it is
      // already up to speed when it appears
      const lead = (p.t0 - t) * speed, roll = S.playing && rolling && !active && lead > 0 && lead < 0.09 * speed && (p.ss || 0) - lead >= 0;
      if (ready) sync(N.v, (p.ss || 0) - (roll ? lead : 0), speed, roll, c.duration, c.fps);
      return;
    }
    const [x, y, w, h] = Geom.rectOf(p);
    setCss(N, 'display', 'block'); setCss(N, 'zIndex', String(P.rank));
    setCss(N, 'left', x + 'px'); setCss(N, 'top', y + 'px'); setCss(N, 'width', w + 'px'); setCss(N, 'height', h + 'px');
    setCss(N, 'clipPath', (rv.vww === w && rv.vhh === h) ? 'none' : `inset(${rv.yo}px ${w - rv.xo - rv.vww}px ${h - rv.yo - rv.vhh}px ${rv.xo}px)`);
    setCss(N, 'opacity', String(Math.min(1, rv.op)));
    setCss(N, 'mixBlendMode', p.blend === 'screen' ? 'screen' : 'normal');
    setCss(N, 'background', p.blend === 'screen' ? 'transparent' : '#000');
    const vg = vignette(p.vig === undefined ? 0.34 : p.vig);
    if (N.vgCss !== vg) { N.vgCss = vg; N.vg.style.background = vg; }
    if (!ready) {
      N.ph.hidden = false; N.v.style.visibility = 'hidden';
      const s = c ? c.state : 'queued';
      N.ph.textContent = base(p.src) + '\n' + (s === 'ready' ? 'loading preview' : s === 'running' ? `preparing preview ${Math.round((c.progress || 0) * 100)}%` : s === 'error' || s === 'missing' ? 'no preview: ' + (c.error || s) : 'preview queued');
      N.ph.style.whiteSpace = 'pre-line';
      return;
    }
    N.ph.hidden = true; N.v.style.visibility = '';
    const g = Geom.view(p, c.sw, c.sh, tq, S.M).crop;
    const kx = c.sw / g.w, ky = c.sh / g.h;
    N.v.style.transform = p.flipx
      ? `translate(${(w + g.x * w / g.w).toFixed(3)}px, ${(-g.y * h / g.h).toFixed(3)}px) scale(${(-kx).toFixed(5)}, ${ky.toFixed(5)})`
      : `translate(${(-g.x * w / g.w).toFixed(3)}px, ${(-g.y * h / g.h).toFixed(3)}px) scale(${kx.toFixed(5)}, ${ky.toFixed(5)})`;
    const f = gradeFilter(p.grade || 'bw', p.expo === undefined ? 1 : p.expo) + (p.ghost ? ` blur(${(p.blur === undefined ? 3 : p.blur) * 10}px)` : '');
    if (N.fCss !== f) { N.fCss = f; N.v.style.filter = f || 'none'; }
    sync(N.v, Geom.srcTime(p, t), speed, S.playing && rolling, c.duration, c.fps);
  }

  const FONTS = { didot: ['Didot, "Bodoni 72", Baskerville, "Times New Roman", serif', 'normal', 400], didot_i: ['Didot, "Bodoni 72", Baskerville, "Times New Roman", serif', 'italic', 400],
    sans: ['Satoshi, Helvetica, Arial, sans-serif', 'normal', 500] };
  const meas = document.createElement('canvas').getContext('2d');
  function drawText(X, tq) {
    const x = X.x, on = x.t0 <= tq && tq < x.t1;
    let N = tnodes.get(X.key);
    if (!on) { if (N) { N.el.remove(); tnodes.delete(X.key); } return; }
    const size = Math.max(6, pyRound(x.size)), F = FONTS[x.font || 'didot'] || FONTS.sans;
    if (!N || N.text !== x.text) {
      if (N) N.el.remove();
      N = { el: el('div', 'tx'), text: x.text, spans: [] };
      for (const ch of x.text) { const s = el('span', '', ch); N.spans.push(s); N.el.appendChild(s); }
      meas.font = `${F[1]} ${F[2]} ${size}px ${F[0]}`;
      const m = meas.measureText('Hg');
      N.lead = (size - ((m.fontBoundingBoxAscent || size * 0.8) + (m.fontBoundingBoxDescent || size * 0.2))) / 2;
      const col = x.color || S.cfg.bone;
      Object.assign(N.el.style, { fontFamily: F[0], fontStyle: F[1], fontWeight: F[2], fontSize: size + 'px', color: `rgb(${col.join(',')})` });
      $('texts').appendChild(N.el); tnodes.set(X.key, N);
    }
    const st = Geom.textState(x, tq), a = x.anchor || 'l';
    N.el.style.letterSpacing = st.trk + 'px';
    N.el.style.left = x.pos[0] + 'px'; N.el.style.top = (x.pos[1] - N.lead) + 'px';
    N.el.style.transform = a === 'c' ? `translateX(calc(-50% + ${st.trk / 2}px))` : a === 'r' ? `translateX(calc(-100% + ${st.trk}px))` : 'none';
    st.chars.forEach((c, i) => { const s = N.spans[i]; s.style.opacity = c.a.toFixed(3); s.style.transform = c.dy ? `translateY(${c.dy.toFixed(2)}px)` : 'none'; });
  }

  function drawStage(tq, t) {
    for (const P of S.P) drawPanel(P, tq, t);
    for (const X of S.X) drawText(X, tq);
    const shot = shotAt(tq), spec = Object.assign({}, S.tl.fx || {}, shot.fx || {});
    const f = Geom.fx(spec, S.tl.duration, tq);
    $('fxFade').style.opacity = clamp(1 - f.fade).toFixed(4);
    $('fxFlash').style.opacity = clamp(f.flash).toFixed(4);
    $('badge').hidden = !(f.shred > 0.5); if (f.shred > 0.5) $('badge').textContent = 'shred effect here (not previewed)';
    const P = selP(), box = $('selbox');
    const show = P && S.outline && P.p.t0 <= tq && tq < P.p.t1 && !(S.playing && !drag);
    box.style.display = show ? 'block' : 'none';
    if (show) {
      const [x, y, w, h] = Geom.rectOf(P.p), b = 3 / S.scale;
      Object.assign(box.style, { left: x + 'px', top: y + 'px', width: w + 'px', height: h + 'px', borderWidth: b + 'px' });
      const lab = box.firstElementChild, text = `${P.shot.id} · ${P.pi + 1}`;
      if (lab.textContent !== text) lab.textContent = text;
      lab.style.top = y < 40 ? '0' : '-34px';
    }
  }

  function fit() {
    const wrap = $('wrap');
    wrap.style.maxWidth = Math.max(320, (window.innerHeight - 300) * S.W / S.H) + 'px';
    wrap.style.aspectRatio = S.W + ' / ' + S.H;
    S.scale = wrap.clientWidth / S.W;
    Object.assign($('stage').style, { width: S.W + 'px', height: S.H + 'px', transform: `scale(${S.scale})` });
  }

  // ───────────── transport ─────────────
  function buildBar() {
    const bar = $('bar'); bar.querySelectorAll('.seg').forEach(s => s.remove());
    const jump = $('jump'); jump.textContent = '';
    for (const s of S.tl.shots) {
      const seg = el('div', 'seg'); seg.dataset.id = s.id;
      seg.style.left = s.t0 / S.tl.duration * 100 + '%'; seg.style.width = (s.t1 - s.t0) / S.tl.duration * 100 + '%';
      seg.title = `${s.id} — ${s.label || ''}`; seg.appendChild(el('span', '', s.id)); bar.insertBefore(seg, $('head'));
      const o = el('option', '', `${s.id} — ${s.label || ''}`); o.value = s.id; jump.appendChild(o);
    }
  }
  function drawTransport(tq) {
    $('head').style.left = tq / S.tl.duration * 100 + '%';
    const shot = shotAt(tq);
    $('tc').textContent = `${fmt(tq, 3)}s / ${fmt(S.tl.duration, 1)}s · frame ${Math.round(tq * S.fps)}`;
    if (S.curShot !== shot.id) {
      S.curShot = shot.id;
      $('cid').textContent = shot.id; $('cname').textContent = shot.label || ''; $('jump').value = shot.id;
      for (const seg of $('bar').querySelectorAll('.seg')) seg.classList.toggle('cur', seg.dataset.id === shot.id);
      drawList(true);
    } else drawList(false);
  }
  const gotoShot = d => { const i = S.tl.shots.indexOf(shotAt(frame(S.t))); const s = S.tl.shots[clamp(i + d, 0, S.tl.shots.length - 1)]; seek(shotFrames(s)[0] / S.fps); };
  function scrub(barEl, onT) {
    let wasPlaying = false;
    const at = e => { const r = barEl.getBoundingClientRect(); return clamp((e.clientX - r.left) / r.width); };
    barEl.addEventListener('pointerdown', e => { if (e.button) return; barEl.setPointerCapture(e.pointerId); wasPlaying = onT(at(e), 'down', e); e.preventDefault(); });
    barEl.addEventListener('pointermove', e => { if (barEl.hasPointerCapture(e.pointerId)) onT(at(e), 'move', e); });
    const up = e => { if (barEl.hasPointerCapture(e.pointerId)) { barEl.releasePointerCapture(e.pointerId); onT(at(e), 'up', e, wasPlaying); } };
    barEl.addEventListener('pointerup', up); barEl.addEventListener('pointercancel', up);
  }

  // ───────────── panel list ─────────────
  let listSig = '';
  function drawList(force) {
    if (!S.tl) return;
    const tq = frame(S.t), shot = shotAt(tq);
    const rows = S.P.filter(P => P.shot === shot || (P.p.t0 <= tq && tq < P.p.t1));
    const sig = rows.map(P => P.key + (P.p.t0 <= tq && tq < P.p.t1 ? '+' : '-') + P.p.src + (P.p.grade || '') + (EDIT.some(k => !same(savedPanel(P)[k], P.p[k])) ? '*' : '')).join(',') + '|' + S.sel + '|' + S.lib.length;
    if (!force && sig === listSig) return;
    listSig = sig;
    const list = $('plist'); list.textContent = '';
    $('plabel').textContent = `Panels in ${shot.id} — ${rows.length}`;
    for (const P of rows) {
      const p = P.p, on = p.t0 <= tq && tq < p.t1;
      const row = el('div', 'prow' + (P.key === S.sel ? ' sel' : '') + (on ? '' : ' off'));
      const clip = S.lib.find(c => c.src === p.src), img = el('img');
      if (clip && clip.poster) img.src = clip.poster;
      img.alt = '';
      const mid = el('div'); mid.appendChild(el('b', '', `${P.shot.id} · ${P.pi + 1}  ${base(p.src)}`));
      const tags = [p.grade || 'bw']; if (p.ghost) tags.push('ghost'); if (p.blend) tags.push(p.blend); if (p.pan) tags.push('pan'); if (p.zoom) tags.push('zoom');
      mid.appendChild(el('div', 'hint', tags.join(' · ')));
      const changed = EDIT.some(k => !same(savedPanel(P)[k], p[k]));
      if (changed) mid.appendChild(el('span', 'e', 'edited'));
      row.append(img, mid, el('span', 'd', `${fmt(p.t0, 2)}–${fmt(p.t1, 2)}s\n${p.rect[2]}×${p.rect[3]}`));
      row.lastChild.style.whiteSpace = 'pre';
      row.addEventListener('click', () => select(P.key, true));
      list.appendChild(row);
    }
    if (!rows.length) list.appendChild(el('span', 'hint', 'No footage panels in this shot.'));
  }
  function select(key, bring) {
    S.sel = key; const P = selP();
    if (P && bring) { const tq = frame(S.t); if (!(P.p.t0 <= tq && tq < P.p.t1)) { pause(); seek(Math.ceil((P.p.t0 + Math.min(0.5, (P.p.t1 - P.p.t0) / 2)) * S.fps) / S.fps); } }
    listSig = ''; drawControls();
  }

  // ───────────── inspector ─────────────
  let svUrl = null, ctlSig = '';
  function inspT(P) { return clamp(frame(S.t), Math.ceil(P.p.t0 * S.fps - 1e-6) / S.fps, (Math.ceil(P.p.t1 * S.fps - 1e-6) - 1) / S.fps); }
  function drawControls() {                   // values that only change with edits / selection
    const P = selP(); $('insp').hidden = !P;
    if (!P) { ctlSig = ''; return; }
    const p = P.p, c = S.clips[p.src] || {};
    $('ilabel').textContent = `Crop — ${P.shot.id} · ${P.pi + 1} · ${base(p.src)}`;
    const active = document.activeElement;
    const setv = (id, v) => { if (active !== $(id)) $(id).value = v; };
    if (!$('grade').options.length) for (const g of Object.keys(S.cfg.grades)) { const o = el('option', '', g); o.value = g; $('grade').appendChild(o); }
    $('grade').value = p.grade || 'bw';
    $('bw').classList.toggle('on', (p.grade || 'bw') === 'bw');
    const ex = p.expo === undefined ? 1 : p.expo, sp = p.speed === undefined ? 1 : p.speed;
    setv('expo', ex); setv('expoN', ex); setv('speed', sp); setv('speedN', sp); setv('ss', p.ss || 0);
    $('resetCrop').disabled = same(savedPanel(P).region, p.region);
    // source bar: this panel's window, and what other panels already use of the same clip
    const dur = c.duration || Math.max(Geom.srcRange(p)[1], 1), [a, b] = Geom.srcRange(p);
    const bar = $('sbar'); bar.querySelectorAll('i.u').forEach(n => n.remove());
    const others = [];
    for (const u of (S.used[p.src] || [])) if (u.timeline !== S.tid) others.push({ a: u.a, b: u.b, who: `${u.timeline} · ${u.shot}`, other: true });
    for (const Q of S.P) if (Q !== P && Q.p.src === p.src) { const r = Geom.srcRange(Q.p); others.push({ a: r[0], b: r[1], who: `${S.tid} · ${Q.shot.id}`, other: Q.shot !== P.shot }); }
    const clashes = [];
    for (const o of others) {
      const hit = o.other && o.a < b && a < o.b; if (hit) clashes.push(o.who);
      const i = el('i', 'u' + (hit ? ' clash' : '')); i.style.left = clamp(o.a / dur) * 100 + '%'; i.style.width = clamp((o.b - o.a) / dur) * 100 + '%'; i.title = o.who; bar.appendChild(i);
    }
    const win = $('swin'); win.style.left = clamp(a / dur) * 100 + '%'; win.style.width = clamp((b - a) / dur, 0, 1 - clamp(a / dur)) * 100 + '%';
    win.classList.toggle('clash', clashes.length > 0);
    $('sskv').textContent = `plays ${fmt(a)}–${fmt(b)}s of ${c.duration ? fmt(c.duration) + 's' : '…'}`;
    const w = [];
    if (clashes.length) w.push('Repeats footage already used in ' + [...new Set(clashes)].join(', ') + '.');
    if (c.duration && b > c.duration + 0.02) w.push(`Runs ${fmt(b - c.duration)}s past the end of the clip (the last frame will hold).`);
    $('swarn').textContent = w.join(' ');
  }
  function drawInspector() {                  // per frame: source frame + crop box
    const P = selP(); if (!P) return;
    const p = P.p, c = S.clips[p.src], url = c && c.state === 'ready' ? media(c.url) : null, ready = !!url, sv = $('sv');
    $('swait').hidden = !!ready; $('crop').hidden = !ready;
    if (!ready) { $('swait').textContent = c && c.state === 'ready' ? 'Loading preview…' : c && c.state === 'running' ? `Preparing preview ${Math.round((c.progress || 0) * 100)}%…` : c && c.error ? 'No preview: ' + c.error : 'Preview queued…';
      $('srcv').style.aspectRatio = '16 / 9'; if (svUrl) { svUrl = null; sv.removeAttribute('src'); sv.load(); } return; }
    if (svUrl !== url) { svUrl = url; sv.src = url; sv.load(); }
    $('srcv').style.aspectRatio = c.sw + ' / ' + c.sh;
    const ti = inspT(P), inside = p.t0 <= frame(S.t) && frame(S.t) < p.t1;
    const g = Geom.view(p, c.sw, c.sh, ti, S.M).crop, box = $('crop').style;
    box.left = g.x / c.sw * 100 + '%'; box.top = g.y / c.sh * 100 + '%'; box.width = g.w / c.sw * 100 + '%'; box.height = g.h / c.sh * 100 + '%';
    sync(sv, Geom.srcTime(p, inside ? S.t : ti), p.speed === undefined ? 1 : p.speed, S.playing && rolling && inside, c.duration, c.fps);
    $('ikv').textContent = `region ${p.region.join(', ')} · showing ${Math.round(g.w)}×${Math.round(g.h)} of ${c.sw}×${c.sh} at source ${fmt(Geom.srcTime(p, ti), 2)}s`;
    const dur = c.duration || 1; $('shead').style.left = clamp(Geom.srcTime(p, ti) / dur) * 100 + '%';
  }

  /* crop editing: `want` is the crop rectangle (source px) the panel should show at the inspector's time */
  let drag = null;
  function cropNow(P) { const c = S.clips[P.p.src]; return Geom.view(P.p, c.sw, c.sh, inspT(P), S.M).crop; }
  function setCrop(P, want, tag) {
    const c = S.clips[P.p.src]; if (!c || c.state !== 'ready') return;
    edit(P, { region: Geom.regionFor(P.p, c.sw, c.sh, inspT(P), S.M, want) }, tag);
  }
  function cropDrag() {
    const box = $('crop'), host = $('srcv');
    box.addEventListener('pointerdown', e => {
      const P = selP(); if (!P || e.button) return;
      const c = S.clips[P.p.src], r = host.getBoundingClientRect();
      drag = { kind: e.target.dataset.c || 'move', k: c.sw / r.width, x: e.clientX, y: e.clientY, g: cropNow(P), tag: 'crop' + (++S.gesture), c };
      box.setPointerCapture(e.pointerId); e.preventDefault();
    });
    box.addEventListener('pointermove', e => {
      if (!drag || !box.hasPointerCapture(e.pointerId)) return;
      const P = selP(); if (!P) return;
      const dx = (e.clientX - drag.x) * drag.k, dy = (e.clientY - drag.y) * drag.k, g = drag.g, c = drag.c;
      if (drag.kind === 'move') return setCrop(P, { cx: g.x + g.w / 2 + dx, cy: g.y + g.h / 2 + dy, w: g.w }, drag.tag);
      const asp = g.w / g.h, west = drag.kind[1] === 'w', north = drag.kind[0] === 'n';
      const ax = west ? g.x + g.w : g.x, ay = north ? g.y + g.h : g.y;          // the corner that stays put
      const mx = (west ? g.x : g.x + g.w) + dx, my = (north ? g.y : g.y + g.h) + dy;
      let w = Math.max(Math.abs(mx - ax), Math.abs(my - ay) * asp, 24);
      w = Math.min(w, west ? ax : c.sw - ax, (north ? ay : c.sh - ay) * asp);
      const h = w / asp;
      setCrop(P, { cx: west ? ax - w / 2 : ax + w / 2, cy: north ? ay - h / 2 : ay + h / 2, w }, drag.tag);
    });
    const up = e => { if (box.hasPointerCapture(e.pointerId)) box.releasePointerCapture(e.pointerId); drag = null; };
    box.addEventListener('pointerup', up); box.addEventListener('pointercancel', up);
    const wheel = e => {
      const P = selP(); if (!P || !(e.altKey || e.ctrlKey)) return;
      const c = S.clips[P.p.src]; if (!c || c.state !== 'ready') return;
      e.preventDefault();
      const g = cropNow(P), k = Math.exp(clamp(e.deltaY, -60, 60) * 0.003);
      setCrop(P, { cx: g.x + g.w / 2, cy: g.y + g.h / 2, w: g.w * k }, 'wheel' + Math.floor(performance.now() / 700));
    };
    host.addEventListener('wheel', wheel, { passive: false });
    $('stage').addEventListener('wheel', wheel, { passive: false });
  }

  /* stage: click selects the panel under the pointer (click again to reach the one beneath); dragging the selected
     panel slides the footage under its crop */
  function stageInput() {
    const stage = $('stage'); let sd = null, lastHit = null;
    const pt = e => { const r = stage.getBoundingClientRect(); return [(e.clientX - r.left) / S.scale, (e.clientY - r.top) / S.scale]; };
    const under = (x, y) => {
      const tq = frame(S.t), out = [];
      for (const P of S.P) {
        const p = P.p; if (!(p.t0 <= tq && tq < p.t1)) continue;
        const rv = Geom.reveal(p, tq); if (!rv) continue;
        const [rx, ry] = Geom.rectOf(p);
        if (x >= rx + rv.xo && x < rx + rv.xo + rv.vww && y >= ry + rv.yo && y < ry + rv.yo + rv.vhh) out.push(P);
      }
      return out.sort((a, b) => b.rank - a.rank);
    };
    stage.addEventListener('pointerdown', e => {
      if (e.button || !S.tl) return;
      if (document.activeElement && document.activeElement !== document.body) document.activeElement.blur();
      const [x, y] = pt(e), hits = under(x, y), P = selP();
      sd = { x: e.clientX, y: e.clientY, hits, moved: false, P: P && hits.includes(P) ? P : null, tag: 'slide' + (++S.gesture) };
      if (sd.P) { const c = S.clips[sd.P.p.src]; if (c && c.state === 'ready') sd.g = cropNow(sd.P); else sd.P = null; }
      stage.setPointerCapture(e.pointerId); e.preventDefault();
    });
    stage.addEventListener('pointermove', e => {
      if (!sd || !stage.hasPointerCapture(e.pointerId)) return;
      const dx = e.clientX - sd.x, dy = e.clientY - sd.y;
      if (!sd.moved && Math.hypot(dx, dy) < 4) return;
      sd.moved = true;
      if (!sd.P) return;
      if (S.playing) pause();
      drag = sd;
      const [, , w, h] = Geom.rectOf(sd.P.p), g = sd.g;
      setCrop(sd.P, { cx: g.x + g.w / 2 - dx / S.scale * g.w / w, cy: g.y + g.h / 2 - dy / S.scale * g.h / h, w: g.w }, sd.tag);
    });
    const up = e => {
      if (!sd) return;
      if (stage.hasPointerCapture(e.pointerId)) stage.releasePointerCapture(e.pointerId);
      if (!sd.moved) {
        if (!sd.hits.length) { S.sel = null; listSig = ''; drawControls(); }
        else { const i = sd.hits.findIndex(P => P.key === S.sel); const here = `${Math.round(sd.x / 6)},${Math.round(sd.y / 6)}`;
          select(sd.hits[(i >= 0 && lastHit === here) ? (i + 1) % sd.hits.length : 0].key); lastHit = here; }
      }
      sd = null; drag = null;
    };
    stage.addEventListener('pointerup', up); stage.addEventListener('pointercancel', up);
  }

  // ───────────── swap clip ─────────────
  function openSwap() {
    const P = selP(); if (!P) return;
    $('swap').hidden = false; $('swapQ').value = ''; fillSwap(); $('swapQ').focus();
  }
  function usesOf(src) {
    let n = (S.used[src] || []).filter(u => u.timeline !== S.tid).length;
    for (const Q of S.P) if (Q.p.src === src) n++;
    return n;
  }
  function fillSwap() {
    const P = selP(), grid = $('swapGrid'), q = $('swapQ').value.trim().toLowerCase(), unused = $('swapUnused').checked;
    grid.textContent = '';
    for (const c of S.lib) {
      const text = (c.name + ' ' + ((c.meta || {}).q || '') + ' ' + (c.folder || '')).toLowerCase(), n = usesOf(c.src);
      if (q && !q.split(/\s+/).every(w => text.includes(w))) continue;
      if (unused && n) continue;
      const card = el('div', 'card' + (P && c.src === P.p.src ? ' sel' : '')), th = el('div', 'th'), img = el('img');
      img.loading = 'lazy'; img.alt = ''; if (c.poster) img.src = c.poster;
      th.append(img, el('span', 'd', c.duration ? fmt(c.duration, 1) + 's' : ''));
      if (n) th.appendChild(el('span', 'tag', n + (n > 1 ? ' uses' : ' use')));
      const m = el('div', 'm'); m.appendChild(el('b', '', c.name));
      card.append(th, m, el('div', 'q', (c.meta || {}).q || `${c.width || '?'}×${c.height || '?'}`));
      card.addEventListener('click', () => {
        const p = P.p, len = (p.t1 - p.t0) * (p.speed === undefined ? 1 : p.speed);
        const patch = { src: c.src };
        if (c.src !== p.src) patch.ss = c.duration ? Math.round(clamp(Math.min(p.ss || 0, c.duration - len), 0, c.duration) * 100) / 100 : 0;
        $('swap').hidden = true; edit(P, patch);
      });
      grid.appendChild(card);
    }
    if (!grid.children.length) grid.appendChild(el('span', 'hint', S.lib.length ? 'No clips match.' : 'The library is empty or still being indexed.'));
  }

  // ───────────── wiring ─────────────
  function wire() {
    $('play').addEventListener('click', () => (S.playing ? pause() : play()));
    $('prev').addEventListener('click', () => gotoShot(-1)); $('next').addEventListener('click', () => gotoShot(1));
    $('jump').addEventListener('change', e => { const s = S.tl.shots.find(x => x.id === e.target.value); if (s) seek(shotFrames(s)[0] / S.fps); e.target.blur(); });
    $('loop').addEventListener('click', e => { S.loop = !S.loop; e.target.classList.toggle('on', S.loop); });
    $('mute').addEventListener('click', e => { audio.muted = !audio.muted; e.target.classList.toggle('on', audio.muted); });
    $('outline').addEventListener('click', e => { S.outline = !S.outline; e.target.classList.toggle('on', S.outline); });
    $('gq').addEventListener('change', e => { S.gq = e.target.value; try { localStorage.setItem('cutvelvet.gq', S.gq); } catch (x) { /* private mode */ } e.target.blur(); });
    $('tl').addEventListener('change', e => { switchTo(e.target.value); e.target.blur(); });
    scrub($('bar'), (f, phase, e, was) => {
      if (phase === 'down') { const w = S.playing; pause(); seek(f * S.tl.duration); return w; }
      seek(f * S.tl.duration); if (phase === 'up' && was) play();
    });
    $('undo').addEventListener('click', undo); $('redo').addEventListener('click', redo);
    $('save').addEventListener('click', save); $('revert').addEventListener('click', revert); $('undoSave').addEventListener('click', undoSave);
    $('swapBtn').addEventListener('click', openSwap); $('swapClose').addEventListener('click', () => { $('swap').hidden = true; });
    $('swapQ').addEventListener('input', fillSwap); $('swapUnused').addEventListener('change', fillSwap);
    $('resetCrop').addEventListener('click', () => { const P = selP(); if (P) edit(P, { region: savedPanel(P).region }); });
    $('grade').addEventListener('change', e => { const P = selP(); if (P) edit(P, { grade: e.target.value }); });
    let lastColour = 'vivid';
    $('bw').addEventListener('click', () => {
      const P = selP(); if (!P) return; const g = P.p.grade || 'bw';
      if (g === 'bw') { const was = savedPanel(P).grade; edit(P, { grade: was && was !== 'bw' ? was : lastColour }); } else { lastColour = g; edit(P, { grade: 'bw' }); }
    });
    const num = (id, key, lo, hi, dp) => {
      const tagOf = () => key + (S.gesture);
      $(id).addEventListener('pointerdown', () => { S.gesture++; }); $(id).addEventListener('focus', () => { S.gesture++; });
      $(id).addEventListener('input', e => { const P = selP(), v = parseFloat(e.target.value); if (!P || !isFinite(v)) return;
        edit(P, { [key]: Math.round(clamp(v, lo, hi) * 10 ** dp) / 10 ** dp }, tagOf()); });
      $(id).addEventListener('change', e => { if (e.target.type === 'range') e.target.blur(); drawControls(); });
    };
    num('expo', 'expo', 0.1, 4, 2); num('expoN', 'expo', 0.1, 4, 2); num('speed', 'speed', 0.05, 8, 3); num('speedN', 'speed', 0.05, 8, 3); num('ss', 'ss', 0, 1e5, 3);
    // source bar: drag the window (or click) to move the in-point
    let grab = 0;
    scrub($('sbar'), (f, phase, e) => {
      const P = selP(); if (!P) return; const p = P.p, c = S.clips[p.src] || {}, dur = c.duration; if (!dur) return;
      const len = Geom.srcRange(p)[1] - Geom.srcRange(p)[0];
      if (phase === 'down') { S.gesture++; pause(); const a = (p.ss || 0) / dur; grab = (f >= a && f <= a + len / dur) ? f - a : Math.min(len / dur / 2, 0.5); }
      const ss = Math.round(clamp((f - grab) * dur, 0, Math.max(0, dur - len)) * 100) / 100;
      edit(P, { ss }, 'ss' + S.gesture);
    });
    window.addEventListener('keydown', e => {
      const tag = (e.target.tagName || '').toLowerCase(), typing = tag === 'input' && e.target.type !== 'range' && e.target.type !== 'checkbox' || tag === 'textarea' || tag === 'select';
      const mod = e.metaKey || e.ctrlKey;
      if (mod && e.key.toLowerCase() === 's') { e.preventDefault(); save(); return; }
      if (mod && e.key.toLowerCase() === 'z' && !typing) { e.preventDefault(); (e.shiftKey ? redo : undo)(); return; }
      if (e.key === 'Escape') { if (!$('swap').hidden) $('swap').hidden = true; else if (typing) e.target.blur(); else { S.sel = null; listSig = ''; drawControls(); } return; }
      if (typing || mod || !S.tl || !$('swap').hidden) return;
      if (e.key === ' ') { e.preventDefault(); S.playing ? pause() : play(); }
      else if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') { e.preventDefault(); pause(); seek(frame(S.t) + (e.key === 'ArrowLeft' ? -1 : 1) * (e.shiftKey ? 1 : 1 / S.fps)); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); gotoShot(-1); }
      else if (e.key === 'ArrowDown') { e.preventDefault(); gotoShot(1); }
      else if (e.key.toLowerCase() === 'l') $('loop').click();
    });
    window.addEventListener('beforeunload', e => { if (S.tl && edits().length) { e.preventDefault(); e.returnValue = ''; } });
    document.addEventListener('click', e => { const b = e.target.closest && e.target.closest('button'); if (b) b.blur(); });   // keep space / arrows on the transport
    new ResizeObserver(fit).observe($('wrap')); window.addEventListener('resize', fit);
    cropDrag(); stageInput();
  }

  function tick() {
    requestAnimationFrame(tick);
    if (!S.tl) return;
    if (S.playing) {
      let t = now();
      if (S.loop) { const s = S.tl.shots.find(x => x.id === S.curShot); if (s) { const [a, b] = shotFrames(s); if (t >= b / S.fps || t < a / S.fps - 0.05) { seek(a / S.fps); t = S.t; } } }
      if (t >= lastT()) { S.t = lastT(); pause(); } else S.t = t;
    }
    const tq = frame(S.t);
    drawStage(tq, S.t); drawTransport(tq); drawInspector();
  }

  // ───────────── embedded in the main page (/live.html?embed=1) ─────────────
  // The shell reads state through window.cutvelvetLive and is told about changes with postMessage.
  function tell() {
    if (!EMBED || parent === window) return;
    try { parent.postMessage({ cutvelvetLive: { ready: !!S.tl, tid: S.tid, dirty: S.tl ? edits().length : 0 } }, location.origin); } catch (e) { /* not same-origin: ignore */ }
  }
  async function switchTo(tid) {              // false when the user keeps their unsaved changes
    if (tid === S.tid) return true;
    if (edits().length && !confirm('Discard unsaved changes and open another timeline?')) { $('tl').value = S.tid; return false; }
    $('tl').value = tid;
    try { await open(tid); } catch (e) { fail(e); }
    return true;
  }
  window.cutvelvetLive = {
    state: () => ({ ready: !!S.tl, tid: S.tid, dirty: S.tl ? edits().length : 0, t: S.t, sel: S.sel, playing: S.playing }),
    open: switchTo, pause: () => { if (S.tl) pause(); },
    save: async () => { if (S.tl && edits().length) await save(); return !edits().length; },
    reload: async () => { if (S.tl && !edits().length) { const t = S.t, sel = S.sel; await open(S.tid, t); if (sel && find(sel)) select(sel, false); } },
  };

  async function boot() {
    // SVG filters on <video> are only known-good in Chromium; elsewhere start with the plain CSS approximation
    const dflt = /Chrome\//.test(navigator.userAgent) ? 'svg' : 'css';
    try { S.gq = localStorage.getItem('cutvelvet.gq') || dflt; } catch (e) { S.gq = dflt; }
    $('gq').value = S.gq;
    wire();
    try {
      S.cfg = await api('/api/timeline');
      S.fps = S.cfg.project.fps; [S.W, S.H] = S.cfg.project.canvas; S.M = S.cfg.margin;
      $('title').textContent = S.cfg.project.name || 'cutvelvet';
      for (const t of S.cfg.timelines) { const o = el('option', '', `${t.id}${t.title ? ' — ' + t.title : ''}`); o.value = t.id; $('tl').appendChild(o); }
      fit();
      api('/api/library').then(d => { S.lib = (d && d.clips) || []; listSig = ''; }).catch(() => {});
      const q = new URLSearchParams(location.search), want = q.get('tl');
      const first = S.cfg.timelines.find(t => t.id === want) || S.cfg.timelines[0];
      if (!first) return fail(new Error('This project has no timelines yet.'));
      $('tl').value = first.id;
      await open(first.id, parseFloat(q.get('t')) || 0);
    } catch (e) { fail(e); }
    requestAnimationFrame(tick);
  }
  boot();
})();
