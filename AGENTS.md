# cutvelvet — guide for agents

cutvelvet is a local tool for cutting short films out of existing footage, where an agent does the
editing and a human gives notes. The look it is built for: black canvas, a few cropped strips of
video stacked or set side by side, cut to a song.

Read this whole file before touching a project. Then read the project's standing rules: the enabled
entries of `<project>/rules.json` (edited in the page's Rules panel), or `<project>/RULES.md` if only
that exists. They are the editor's own preferences and they override your taste. The tool itself has
no house style: a project with no rules is valid, and nothing below is a rule unless the project says so.

## Setup

- Python 3.9+, `numpy`, `pillow`, and `ffmpeg` + `ffprobe` on PATH.
- `pip install <this repo>`, or run without installing: `PYTHONPATH=<this repo> python -m cutvelvet …`.
- On Apple Silicon use a native arm64 Python and ffmpeg; under Rosetta renders are several times slower.
- Everything is local. The server binds to 127.0.0.1 and has no accounts. Do not send project
  content to outside services. Never print API keys from `.env*` files.

## What a project is

A project is any folder with a `project.json`:

```
project.json            name, canvas [w,h], fps, library folders, folder names
timelines/<id>.json     one timeline per chapter / film — THE SOURCE OF TRUTH FOR THE CUT
timelines/.history/     snapshot of the timeline for every published version, plus undo copies
footage/…               source clips (any folders listed in project.json "library")
music/…                 audio
review/<id>_v<N>.mp4    published versions the human watches, with <id>_v<N>_shots.json
renders/                output of `cutvelvet render` when no --out is given (`publish` writes to review/ only)
feedback/<id>_v<N>.json the human's notes on that version
feedback/<id>_v<N>_changes.md   what the agent did in response (you write this)
rules.json              the editor's standing rules (optional; RULES.md is the older free-text form)
.cutvelvet/               caches: shot renders, thumbnails, proxies, run logs. Safe to delete.
```

## The timeline file

`cutvelvet schema` prints the full file format (it is the docstring at the top of
`cutvelvet/timeline.py`). The short version:

- A timeline has `duration`, `audio`, optional `fx`, and `shots` (ordered, contiguous, first starts
  at 0, last ends at `duration`).
- A shot has `id`, `label`, `t0`, `t1`, `panels`, `texts`, optional `fx`. Shots are the unit of
  review (notes are per shot) and of render caching.
- All times are absolute timeline seconds.

A panel is one rectangle of video:

| key | meaning |
|---|---|
| `src` | project-relative path of the clip |
| `ss` | second in the source where this panel starts reading |
| `t0`, `t1` | when the panel is on screen (timeline seconds) |
| `speed` | playback rate; source time read = `ss … ss + (t1 - t0) * speed` |
| `rect` | `[x, y, w, h]` on the canvas, in canvas pixels (default canvas 1920×1080) |
| `region` | `[cx, cy, wf]`: the part of the source frame to look at. Centre at (`cx`,`cy`) as fractions of the frame, `wf` = width as a fraction of the frame width. The panel then takes the largest crop of that region with the rect's aspect ratio. A wide, short rect over a face therefore shows a sliver. |
| `grade` | `bw`, `vivid`, `amber`, `cold`, `blue`, `green`, `violet` |
| `expo` | exposure multiplier (1.0 neutral; dark clips often need 1.5–2.5) |
| `zoom` | `[z0, z1]` slow push from z0 to z1 over the panel's life (default `[1, 1.08]`) |
| `pan` | `[[x0, y0], [x1, y1]]`, each −1…1, drift inside the slack left by zoom/aspect |
| `tin`, `tout` | `[kind, seconds]`, kind = `lid` (opens vertically like an eyelid), `wipeL`, `wipeR`, `fade`, `cut` |
| `blend`, `op`, `z` | `"screen"` for light overlays (rain, bokeh); opacity; z-order (higher on top) |
| `ghost`, `blur`, `flipx` | soft blurred underlay and its blur; mirror |
| `vig` | vignette, default 0.34: the panel's edges darken to about 78% at mid-edge and 55% in the corners. Use 0 for overlays, and lower it (0–0.15) on slivers and small panels, where the falloff eats most of the picture; on dark footage the default can make a strip's edges vanish |

Texts: `text, t0, t1, pos [x,y], size, font ("didot", "didot_i", "sans" or a font path), anchor,
fade, fadeout, track, stagger, rise, color`.
`pos` is in canvas pixels and `y` is the TOP of the text, not its baseline. `anchor` is `l` (default,
`x` is the left edge), `c` (centred on `x`) or `r` (`x` is the right edge). `track` is extra pixels
between characters and is included when centring.

### Time and the frame grid

A panel or text is drawn on frame `n` when `t0 <= n/fps < t1`. Shot boundaries snap to the nearest
frame, and a panel time that falls between two frames takes effect at the next frame. Put cuts on
the frame grid (multiples of 1/fps; at 24 fps, multiples of 0.125 s are always safe) so that what
you wrote is what plays.

### Two layouts to start from

`rect` is `[x, y, w, h]` on the canvas; panels may overlap and are drawn in `z` order. On a
1920×1080 canvas:

```
stacked strips (two wide slivers)          side by side (two tall panels)
"rect": [0, 300, 1660, 226]                "rect": [200, 140, 620, 800]
"rect": [260, 546, 1660, 226]              "rect": [1100, 140, 620, 800]
```

Offsetting stacked strips sideways, and giving the second panel a later `t0` (0.4–0.8 s) with a
`wipeL`/`wipeR` in, keeps a shot from looking like a grid. `lid` reads best on the first panel of a
shot and on eyes; `cut` is right for fast sequences. The example project's `timelines/demo.json`
shows all of these in use.

### Audio

A cut's `audio.src` can be any file ffmpeg decodes (wav, mp3, m4a, aac, flac, ogg, opus, aiff). A cut
may also have no audio at all: leave `audio` out. The New cut page can play wav, mp3, m4a, aac, flac and
opus in the browser; aif/aiff cuts fine but has no in-page preview, and ogg preview is untested.

Edit timelines by editing the JSON. Keep numbers literal. Do not reorder keys or reformat the file
more than your change needs; load/save with `cutvelvet.timeline.load_timeline` / `save_timeline` if
you script an edit.

## Commands

```
cutvelvet audit   <project> [timeline]                 validate + report repeated footage
cutvelvet stills  <project> <timeline> 12.5,40,88.2    single frames -> .cutvelvet/stills/<timeline>/
cutvelvet grid    <project> <src> 8.5,11.2             source frames with a 10x10 grid -> .cutvelvet/grid/
cutvelvet probe   <project> <src>                      duration, size, fps of a clip (one JSON line)
cutvelvet schema                                       the timeline file format
cutvelvet analyze <project> <audio> [--lyrics]         beats, sections (and lyric lines) -> prints `markers: <path>`
cutvelvet render  <project> <timeline> --scale 0.5 [--jobs N] [--shots S1a,S2b]
cutvelvet publish <project> <timeline> --scale 0.5     audit -> render -> review/<id>_v<N+1>.mp4 + shots json + history snapshot
cutvelvet serve   <project> [--port 8771]              review page, library, live preview, Run button
```

- `audit` and `publish` take `--strict` (repeats always fail) or `--allow-repeats` (repeats are only a
  warning) to override the project's repeat rule for one command. Do not use them to get past a rule
  the project has switched on.
- When started by "Run notes", the prompt you were given already contains the project's enabled rules
  as text; you do not need to open `rules.json` again.
- Without `--jobs`, `render` and `publish` use this machine's performance setting
  (`~/.cutvelvet/settings.json`); leave `--jobs` off unless the human asks for a number.
- When you were started by the page ("Run notes" or "Build first cut"), `cutvelvet` is already on
  PATH and is the only program your permissions let you run: no `ffmpeg`, `ffprobe` or
  `python -m cutvelvet` (plain read-only commands such as `ls` still work inside the project). Use `cutvelvet grid` for source frames and `cutvelvet probe` for a clip's
  length. During such a run every `cutvelvet` command is confined to this project: it refuses another
  project folder and any `--out` outside it. You can read the project folder and this guide, and edit
  files inside the project folder, but not `project.json`.
- `<src>` for `grid` and `probe` is the clip as written in the timeline (`footage/px/123.mp4`); it
  must be in one of the project's library folders.
- `analyze` finds the tempo, beat times and coarse section changes of a song (not bars or downbeats;
  count beats yourself if cuts must land on bar lines). The result is cached as JSON under
  `.cutvelvet/analysis/`; the last line it prints is the path. Lyric lines need `--lyrics`, are slow,
  and their words are unreliable.
- `--scale 0.5` is the draft size for review. Full resolution (`--scale 1`) only when the human
  approves a version.
- Renders are cached per shot, keyed on that shot's content. Changing one shot re-renders one shot.
- A two-minute timeline takes minutes to render. Stills take seconds. Check with stills first.

## The review loop

1. The human watches `review/<id>_v<N>.mp4` in the page and writes notes. They are saved to
   `feedback/<id>_v<N>.json`:

   ```json
   {"version": "ch2_v1", "overall": "…",
    "shots": {"S3b": {"verdict": "", "note": "[0:46.2] crop is on his shoulder, want the mouth",
                      "pins": [{"src": "footage/px/123.mp4", "ss": 8.5, "comment": ""}]}}}
   ```

   A note may hold several lines, each optionally starting with a `[m:ss.s]` timestamp in timeline
   time. A pin means: use this clip, starting at this source second, in this shot.
2. You apply every note to `timelines/<id>.json`, and only what the notes ask for. Leave approved
   shots alone. If a note is ambiguous, make the most conservative reading and say so in the summary.
3. Verify (below), `cutvelvet audit`, then `cutvelvet publish`.
4. Write `feedback/<new version>_changes.md`: one line per note saying what you did, and plainly
   list anything you could not do or are unsure about. Do not claim a shot is fixed if you have not
   looked at a frame of it.

Notes that state a general preference ("no repeated cuts anywhere", "skin should be black and
white") are rules: apply them to the whole timeline and add them to the project's rules as a custom rule.

## Placing a crop — the method that works

Guessing `region` from a thumbnail fails about half the time: the strip lands on a forehead or a
shirt. Do this instead:

1. Extract the exact source frame with a coordinate grid:
   `cutvelvet grid <project> <src> <ss>` — it prints the path of a jpg in `.cutvelvet/grid/`. Give
   several times at once (`8.5,11.2`) to see the start and end of a take.
   (For humans working by hand, that is this ffmpeg line:
   `ffmpeg -ss <ss> -i <clip> -frames:v 1 -vf "scale=1280:-2,drawgrid=w=iw/10:h=ih/10:t=1:c=yellow@0.6" grid.jpg`)
2. Look at it. Read off where the subject is in tenths of the frame (eyes at x≈0.47, y≈0.33).
   That is your `cx`, `cy`. Choose `wf` from how much should be visible (eye pair ≈ 0.25–0.45 of a
   close-up; a whole face in a tall panel ≈ 0.5–0.9).
3. Subjects move. Check a frame near the end of the panel's source range too
   (`ss + (t1 - t0) * speed`), and remember `zoom` and `pan` shift the crop over time.
4. Render stills at two or three times inside the shot with `cutvelvet stills` and look at them.
   Only then render.

Other things that go wrong:

- Eyes closed or a blink across the take: scrub for a better `ss`.
- Dark clips: raise `expo`, check a still; do not trust the thumbnail.
- A strip whose content is unreadable at strip height (hands, objects): give it a taller rect or
  choose a different moment.
- Different actors standing in for one character: keep the cast consistent within a timeline.

## Finding footage

- The library index is `.cutvelvet/library.json` (or `GET /api/library` when the server is running):
  each clip's path, duration, resolution, and any description metadata. Posters and ten-frame
  sprites are in `.cutvelvet/thumbs/` — look at the sprite before choosing a clip.
- `GET /api/used`, or `cutvelvet.timeline.used_ranges(project)`, lists which source seconds every
  clip already contributes to any timeline.
- Honour pins first. Then prefer unused clips, then unused ranges of used clips.
- If the library has nothing suitable, say so in the changes file and suggest what to look for.
  Do not download footage unless the project's rules or the human say you may.

## Repeats

`cutvelvet audit` reports any moment of footage that appears in two different shots. Whether that is
an error is the project's choice (the "never reuse footage" rule); with the rule off it is only a
warning. It compares `[ss, ss + (t1 - t0) * speed]` per source across shots. Two panels of
the same shot showing the same moment (a wide and a sliver of it) are fine.

## Do not

- Do not edit `review/` videos or existing `feedback/*.json` files; they are the record.
- Do not delete footage, music, or history snapshots.
- Do not render at full resolution or re-render untouched shots unless asked.
- Do not put text on screen that the timeline did not already have unless a note asks for it.
