# Layered strip compositor: footage panels (eyelid-opening strips, ghosts, screens), type, grain -> ffmpeg.
# Nothing here knows about a particular film:
# call configure(root, scale, canvas, fps) before rendering (defaults: cwd, $SCALE or 1, 1920x1080, 24).
import subprocess, json, os, sys, math
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter

FFMPEG = os.environ.get('CUTVELVET_FFMPEG', 'ffmpeg')
FFPROBE = os.environ.get('CUTVELVET_FFPROBE', 'ffprobe')
W, H, FPS = 1920, 1080, 24
S = float(os.environ.get('SCALE', '1'))          # 0.5 = fast draft
OW, OH = int(W * S) // 2 * 2, int(H * S) // 2 * 2
ROOT = os.getcwd()                               # project root; panel 'src' paths are relative to it
THREADS = 0                                      # threads each ffmpeg (decoder, encoder) may use; 0 = ffmpeg decides
# Encoder quality. A draft (scale < 1) is for watching and taking notes: grain is expensive to encode faithfully, so
# it gets a higher CRF and a faster preset -- a few MB per minute instead of ~90. Full scale keeps CRF 18 / medium.
FULL_QUALITY = (18, 'medium')
DRAFT_QUALITY = (23, 'veryfast')                 # ~2 Mbit/s at 960x540 on the example cut (CRF 18 / medium: ~13)
def quality(): return DRAFT_QUALITY if S < 1 else FULL_QUALITY
def _th(): return ['-threads', str(int(THREADS))] if THREADS else []
def configure(root=None, scale=None, canvas=None, fps=None, threads=None):
    """Set the project root / draft scale / canvas / fps / ffmpeg thread cap for everything rendered by this process."""
    global ROOT, S, W, H, FPS, OW, OH, THREADS
    if threads is not None: THREADS = max(0, int(threads))
    if root is not None: ROOT = os.path.abspath(root)
    if scale is not None: S = float(scale)
    if canvas is not None: W, H = int(canvas[0]), int(canvas[1])
    if fps is not None: FPS = fps
    OW, OH = int(W * S) // 2 * 2, int(H * S) // 2 * 2
    _vig.clear(); _fonts.clear()
def sc(r): return tuple(int(round(v * S)) for v in r)
BONE = (238, 232, 220)

GR = {  # shadow tint, highlight tint, saturation, contrast
    'bw':    ((1.0, .99, .96), (1.0, .99, .95), 0.0, 1.38),
    'vivid': ((.96, .95, 1.0), (1.04, 1.0, .95), 1.22, 1.24),
    'amber': ((.98, .66, .42), (1.08, .98, .78), 1.08, 1.2),
    'cold':  ((.66, .80, 1.10), (.93, 1.0, 1.08), .42, 1.25),
    'blue':  ((.25, .45, 1.35), (.70, .95, 1.25), .55, 1.35),
    'green': ((.45, .80, .62), (.92, 1.05, .90), .70, 1.28),
    'violet': ((.70, .50, 1.20), (1.05, .90, 1.10), .75, 1.28),
}
LUMA = np.array([.2126, .7152, .0722], np.float32)
_vig = {}

def clamp(x, a=0., b=1.): return max(a, min(b, x))
def lerp(a, b, u): return a + (b - a) * u
def smooth(u): u = clamp(u); return u * u * (3 - 2 * u)
def eout(u): u = clamp(u); return 1 - (1 - u) ** 3
def ein(u): u = clamp(u); return u ** 3

def grade(x, name, expo=1.0, vig=.34):
    """x: float32 HxWx3 in 0..1 (display-referred). returns float32 0..1"""
    sh, hi, sat, con = GR[name]
    x = np.power(x, 2.2) * expo
    # Luma as a plain weighted sum. `x @ LUMA` goes through the BLAS library, and Apple's Accelerate raises spurious
    # "divide by zero / overflow / invalid value in matmul" warnings for large float32 arrays on some numpy builds.
    l = (x[..., 0] * LUMA[0] + x[..., 1] * LUMA[1] + x[..., 2] * LUMA[2])[..., None]
    x = np.maximum(l + (x - l) * sat, 0)
    ll = np.sqrt(np.clip(l, 0, 1))
    x = x * (np.array(sh, np.float32) * (1 - ll) + np.array(hi, np.float32) * ll)
    x = np.power(np.clip(x, 0, 4), 1 / 2.2)
    x = np.clip((x - .42) * con + .42 - .035, 0, 1)
    x = x * x * (3 - 2 * x) * .35 + x * .65
    h, w = x.shape[:2]
    # cheap highlight glow
    small = Image.fromarray((x * 255).astype(np.uint8)).resize((max(8, w // 8), max(8, h // 8)), Image.BILINEAR)
    g = np.asarray(small.filter(ImageFilter.GaussianBlur(max(2, w / 220))).resize((w, h), Image.BILINEAR), np.float32) / 255.
    x = 1 - (1 - x) * (1 - .22 * g * g)
    if vig:
        k = (h, w, vig)
        if k not in _vig:
            yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
            _vig[k] = (1 - vig * (((xx / w - .5) * 1.15) ** 2 + ((yy / h - .5) * 1.15) ** 2) * 2)[..., None]
        x = x * _vig[k]
    return np.clip(x, 0, 1)

_probe = {}
def probe(path):
    if path not in _probe:
        o = subprocess.run([FFPROBE, '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=width,height', '-of', 'csv=p=0', path], capture_output=True, text=True).stdout.strip().split(',')
        _probe[path] = (int(o[0]), int(o[1]))
    return _probe[path]

MARGIN = 1.16
class Reader:
    """Decodes a static super-region of the source (panel aspect, with zoom/pan slack) at panel resolution."""
    def __init__(self, p, t=None):
        path = os.path.join(ROOT, p['src']); sw, sh = probe(path)
        x, y, w, h = sc(p['rect']); cx, cy, wf = p['region']
        rw = wf * sw * MARGIN; rh = rw * h / w
        s = min(1., sw / rw, sh / rh); rw *= s; rh *= s
        x0 = clamp(cx * sw - rw / 2, 0, sw - rw); y0 = clamp(cy * sh - rh / 2, 0, sh - rh)
        self.dw = int(w * MARGIN * p.get('q', 1.0)) // 2 * 2; self.dh = int(self.dw * rh / rw) // 2 * 2
        speed = p.get('speed', 1.0)
        vf = f"crop={int(rw)}:{int(rh)}:{int(x0)}:{int(y0)},scale={self.dw}:{self.dh}:flags=bicubic,setpts=PTS/{speed},framerate=fps={FPS}"
        if p.get('flipx'): vf += ',hflip'
        self.proc = subprocess.Popen([FFMPEG, '-v', 'error'] + _th() + (['-filter_threads', str(int(THREADS))] if THREADS else []) + ['-ss', str(p.get('ss', 0) + (max(0, (t if t is not None else p['t0']) - p['t0']) * speed)), '-i', path, '-vf', vf, '-an', '-pix_fmt', 'rgb24', '-f', 'rawvideo', '-'], stdout=subprocess.PIPE)
        self.last = None
    def read(self):
        n = self.dw * self.dh * 3; b = self.proc.stdout.read(n)
        if len(b) == n: self.last = Image.frombuffer('RGB', (self.dw, self.dh), b, 'raw', 'RGB', 0, 1)
        return self.last
    def close(self):
        try: self.proc.kill()
        except Exception: pass

def trans(kind, u):
    """returns (visible height frac, visible width frac, wipe dir, opacity) for progress u 0..1"""
    if kind == 'lid': return eout(u), 1, 0, 1
    if kind == 'wipeL': return 1, eout(u), -1, 1
    if kind == 'wipeR': return 1, eout(u), 1, 1
    if kind == 'fade': return 1, 1, 0, smooth(u)
    return 1, 1, 0, 1

def draw_panel(canvas, p, rd, t):
    u = clamp((t - p['t0']) / (p['t1'] - p['t0']))
    fr = rd.read()
    if fr is None: return
    x, y, w, h = sc(p['rect'])
    z0, z1 = p.get('zoom', (1.0, 1.08)); z = lerp(z0, z1, u)
    (a0, b0), (a1, b1) = p.get('pan', ((0, 0), (0, 0))); px, py = lerp(a0, a1, u), lerp(b0, b1, u)
    vw = rd.dw / (MARGIN * z) * 1.0 * (MARGIN / MARGIN); vw = min(rd.dw, rd.dw / z); vh = vw * h / w
    if vh > rd.dh: vh = rd.dh; vw = vh * w / h
    sx = (rd.dw - vw) / 2; sy = (rd.dh - vh) / 2
    bx = sx + px * sx; by = sy + py * sy
    if p.get('ghost'):
        im = fr.resize((max(8, w // 10), max(8, h // 10)), Image.BILINEAR, box=(bx, by, bx + vw, by + vh)).filter(ImageFilter.GaussianBlur(p.get('blur', 3))).resize((w, h), Image.BICUBIC)
    else:
        im = fr.resize((w, h), Image.LANCZOS, box=(bx, by, bx + vw, by + vh))
    a = grade(np.asarray(im, np.float32) / 255., p.get('grade', 'bw'), p.get('expo', 1.0), p.get('vig', .34))
    kin, din = p.get('tin', ('lid', .5)); kout, dout = p.get('tout', ('cut', 0))
    hv, wv, wd, op = 1, 1, 0, 1
    if din > 0 and t - p['t0'] < din: hv, wv, wd, op = trans(kin, (t - p['t0']) / din)
    if dout > 0 and p['t1'] - t < dout: hv, wv, wd, op = trans(kout, (p['t1'] - t) / dout)
    op *= p.get('op', 1.0)
    if callable(p.get('opf')): op *= p['opf'](t)
    vhh = max(0, int(round(h * hv))); vww = max(0, int(round(w * wv)))
    if vhh < 1 or vww < 1 or op <= 0: return
    yo = (h - vhh) // 2; xo = 0 if wd <= 0 else w - vww
    if wd == 0: xo = (w - vww) // 2
    a = a[yo:yo + vhh, xo:xo + vww]
    X0, Y0 = x + xo, y + yo
    cx0, cy0 = max(0, X0), max(0, Y0); cx1, cy1 = min(OW, X0 + vww), min(OH, Y0 + vhh)
    if cx1 <= cx0 or cy1 <= cy0: return
    a = a[cy0 - Y0:cy1 - Y0, cx0 - X0:cx1 - X0]
    dst = canvas[cy0:cy1, cx0:cx1]
    if p.get('blend') == 'screen': dst[:] = 1 - (1 - dst) * (1 - a * op)
    else: dst[:] = a * op + dst * (1 - op)

_fonts = {}
# kind -> candidates (path, face index), first that loads wins; every kind finally falls back to 'sans', then PIL's default.
# A kind that is not listed is tried as a font file path (absolute, ~ or project-relative) before the sans fallback.
_SERIF = [('/System/Library/Fonts/Supplemental/Didot.ttc', 0), ('/System/Library/Fonts/Supplemental/Bodoni 72.ttc', 0), ('/System/Library/Fonts/Supplemental/Baskerville.ttc', 0),
          ('/System/Library/Fonts/Supplemental/Times New Roman.ttf', 0), ('/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf', 0), ('/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf', 0),
          ('C:/Windows/Fonts/times.ttf', 0), ('DejaVuSerif.ttf', 0)]
_SERIF_I = [('/System/Library/Fonts/Supplemental/Didot.ttc', 1), ('/System/Library/Fonts/Supplemental/Bodoni 72.ttc', 1), ('/System/Library/Fonts/Supplemental/Baskerville.ttc', 2),
            ('/System/Library/Fonts/Supplemental/Times New Roman Italic.ttf', 0), ('/usr/share/fonts/truetype/dejavu/DejaVuSerif-Italic.ttf', 0), ('/usr/share/fonts/truetype/liberation/LiberationSerif-Italic.ttf', 0),
            ('C:/Windows/Fonts/timesi.ttf', 0)]
FONTS = {
    'didot': _SERIF, 'didot_i': _SERIF_I + _SERIF,
    'sans': [('~/Library/Fonts/Satoshi-Medium.otf', 0), ('/System/Library/Fonts/Helvetica.ttc', 0), ('/System/Library/Fonts/Supplemental/Arial.ttf', 0),
             ('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 0), ('/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf', 0), ('C:/Windows/Fonts/arial.ttf', 0), ('DejaVuSans.ttf', 0)],
}
def font(kind, size):
    k = (kind, size)
    if k not in _fonts:
        f = None
        for path, index in (FONTS[kind] if kind in FONTS else [(kind, 0), (os.path.join(ROOT, kind), 0)]) + FONTS['sans']:
            try: f = ImageFont.truetype(os.path.expanduser(path), size, index=index); break
            except (OSError, ValueError): continue
        if f is None:
            try: f = ImageFont.load_default(size)
            except TypeError: f = ImageFont.load_default()
        _fonts[k] = f
    return _fonts[k]

def draw_text(ov, d, tx, t):
    """tx: text, font, size, pos, anchor(l/c/r), t0, t1, fade, track(px), stagger"""
    fin = tx.get('fade', .6); fo = tx.get('fadeout', fin)
    f = font(tx.get('font', 'didot'), max(6, int(round(tx['size'] * S)))); tr = tx.get('track', 0) * S; s = tx['text']
    # tracking eases in from wider
    grow = (1 - eout((t - tx['t0']) / max(fin * 2.2, 1e-3))) * tx.get('spread', 0) * S
    trk = tr + grow
    ws = [d.textlength(c, font=f) for c in s]; total = sum(ws) + trk * (len(s) - 1)
    x, y = tx['pos'][0] * S, tx['pos'][1] * S
    if tx.get('anchor', 'l') == 'c': x -= total / 2
    if tx.get('anchor') == 'r': x -= total
    col = tuple(tx.get('color', BONE)); st = tx.get('stagger', 0.0)
    for i, c in enumerate(s):
        a = smooth((t - tx['t0'] - i * st) / fin) * smooth((tx['t1'] - t) / fo)
        if a > 0.004 and c != ' ':
            d.text((x, y + (1 - eout((t - tx['t0'] - i * st) / (fin * 1.4))) * tx.get('rise', 0) * S), c, font=f, fill=col + (int(255 * a),))
        x += ws[i] + trk

def render(panels, texts, fx, dur, out, audio=None, t_start=0.0, preview=None, quiet=False, crf=None):
    """fx: dict of callables t-> value: 'flash', 'shred', 'fade' (global brightness). crf=None: quality() for this scale."""
    rng = np.random.default_rng(7)
    q_crf, q_preset = quality()
    if crf is None: crf = q_crf
    enc = subprocess.Popen([FFMPEG, '-v', 'error', '-y', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', f'{OW}x{OH}', '-r', str(FPS), '-i', '-',
                            '-c:v', 'libx264', '-preset', q_preset, '-crf', str(crf)] + _th() + ['-pix_fmt', 'yuv420p', '-colorspace', 'bt709', '-color_primaries', 'bt709', '-color_trc', 'bt709', out + '.v.mp4'], stdin=subprocess.PIPE)
    panels = sorted(panels, key=lambda p: p.get('z', 0)); readers = {}
    n0 = int(round(t_start * FPS)); n1 = int(round(dur * FPS))
    for n in range(n0, n1):
        t = n / FPS
        canvas = np.zeros((OH, OW, 3), np.float32)
        for i, p in enumerate(panels):
            if p['t0'] <= t < p['t1']:
                if i not in readers: readers[i] = Reader(p, t)
                draw_panel(canvas, p, readers[i], t)
            elif i in readers and t >= p['t1']:
                readers.pop(i).close()
        act = [tx for tx in texts if tx['t0'] <= t < tx['t1']]
        if act:
            ov = Image.new('RGBA', (OW, OH), (0, 0, 0, 0)); d = ImageDraw.Draw(ov)
            for tx in act: draw_text(ov, d, tx, t)
            o = np.asarray(ov, np.float32) / 255.; a = o[..., 3:4]
            canvas = o[..., :3] * a + canvas * (1 - a)
        sh = fx['shred'](t) if 'shred' in fx else 0
        if sh > 0.5:
            r2 = np.random.default_rng(int(t * 6)); y = 0
            while y < OH:
                bh = max(4, int(r2.integers(14, 90) * S)); canvas[y:y + bh] = np.roll(canvas[y:y + bh], int(r2.normal(0, sh) * S), axis=1); y += bh
        fl = fx['flash'](t) if 'flash' in fx else 0
        if fl > 0: canvas = canvas + fl
        if 'fade' in fx: canvas = canvas * fx['fade'](t)
        g = rng.standard_normal((OH // 2, OW // 2, 1), np.float32).repeat(2, 0).repeat(2, 1) * .012 + rng.standard_normal((OH, OW, 1), np.float32) * .022
        canvas = canvas + g * (.35 + .65 * np.clip(1.2 - canvas.mean(2, keepdims=True), 0, 1))
        canvas *= 1 + .012 * math.sin(t * 37.) * math.sin(t * 11.3)
        enc.stdin.write((np.clip(canvas, 0, 1) * 255).astype(np.uint8).tobytes())
        if preview and n in preview:
            Image.fromarray((np.clip(canvas, 0, 1) * 255).astype(np.uint8)).save(preview[n], quality=88)
        if n % 48 == 0 and not quiet: print(f'{t:6.1f}s / {dur:.0f}s', flush=True)
    for r in readers.values(): r.close()
    enc.stdin.close(); enc.wait()
    if audio:
        subprocess.run([FFMPEG, '-v', 'error', '-y', '-i', out + '.v.mp4', '-i', audio, '-c:v', 'copy', '-c:a', 'aac', '-b:a', '320k', '-shortest', '-movflags', '+faststart', out], check=True)
        os.remove(out + '.v.mp4')
    else: os.replace(out + '.v.mp4', out)
