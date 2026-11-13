# Security

cutvelvet is a local, single-user tool. This page says what it does and does not do, where the
trust boundaries are, and which risks remain yours.

## What it does and does not do

- It runs on your machine only. The server listens on `127.0.0.1` and nowhere else.
- No accounts, no telemetry, no update checks. cutvelvet's own code makes no network requests, and
  the pages load nothing from other hosts (no CDN scripts, fonts or images).
- It never reads `.env` files and never holds an API key or a login. The agent you start is one you
  installed and signed in to yourself.
- It does start other programs: `ffmpeg` and `ffprobe`, the operating system's file chooser, and,
  when you press **Run notes** or **Build first cut**, your coding agent.

## Trust boundaries

### Your browser

Other web pages open in the same browser must not be able to use the server. So the server:

- answers only requests addressed to `127.0.0.1` or `localhost` on its own port (this stops DNS
  rebinding);
- refuses requests a browser marks as coming from another site, apart from a plain visit to the page
  itself. Another site cannot read the API, embed your footage, or put the page in a frame;
- requires a token on every request that changes something (saving, publishing, starting an agent,
  adding a folder, opening a file chooser, changing settings). The token is random, made new each
  time the server starts, and written into the pages the server serves;
- sends a Content-Security-Policy with every page: only the scripts shipped with cutvelvet run, and
  nothing is fetched from or sent to another host. File names, notes, rule text, metadata and agent
  output are shown as text.

Anything running as you on the same machine can still read the token from the page. cutvelvet does
not defend against other programs under your own user account.

### Projects you open

A project folder can come from someone else. Opening one with `cutvelvet serve` does not let it
choose what runs:

- **Agent command.** A `project.json` may ask for its own agent command. It is shown in Settings and
  not used until you approve that exact command on this machine. If it changes, approval lapses.
- **Agent instructions.** A project may carry its own prompt (`.cutvelvet/run_prompt.md`,
  `.cutvelvet/newcut_prompt.md`). It is shown to you and ignored until you approve that exact text.
- **Folders.** `project.json` can name where timelines, reviews, notes, music, cache and renders
  live, but only inside the project. Library folders outside the project are not scanned or served
  until you add them yourself on this machine.
- **Files served.** The page gets renders and shot lists from the review folder, clips inside your
  library folders, songs in the music folder, and thumbnails. Symbolic links that lead out of those
  folders are not followed.
- **Programs.** `cutvelvet`, `ffmpeg` and `ffprobe` are never taken from the project folder, and
  Python modules lying in a project folder are not imported.

What a project can still do:

- A timeline can reference any video or audio file your account can read. Rendering it puts those
  frames in the project's review folder. Nothing is sent anywhere, but look before you share a
  render of a project you did not make.
- Rules, notes, briefs, file names and clip metadata are text the agent reads. See below.
- Started as `python -m cutvelvet` from inside a project folder that contains its own `cutvelvet/`
  folder, Python imports that folder. Use the installed `cutvelvet` command for projects you did
  not make.

### The agent you run

**Run notes** and **Build first cut** start your agent in the project folder. With the default
(Claude Code) it runs without asking questions, and is allowed to:

- read and search files in the project folder and in one folder that holds only cutvelvet's agent
  guide, and nowhere else;
- edit and create files inside the project folder, except `project.json`, `.env` files and anything
  under `.claude/`;
- run `cutvelvet`, and otherwise only the handful of read-only commands Claude Code always permits
  (such as `ls`, `cat`, `grep`, `uname`), which are held to the same two folders when they name a
  file. There is no direct `ffmpeg` or `ffprobe`: the agent
  gets source frames with `cutvelvet grid` and clip details with `cutvelvet probe`, which only read
  clips in the project's library folders. During a run `cutvelvet` itself refuses another project
  folder, any `--out` path outside the project, and the `serve` and `new` commands;
- not use web fetch, web search, MCP servers, skills or slash commands.

It is started with only your own Claude Code settings (`--setting-sources user`), so a
`.claude/settings.json` that arrives with a project — hooks, extra permissions — is not loaded.

This list is the agent's own permission system, not a sandbox, and you should know its limits:

- The rules are matched by Claude Code against what the agent asks to do. We checked them once with
  a real run: editing a project file and running `cutvelvet` worked; reading a file outside the
  project (with the file tool and with `cat`), editing `project.json` and reading `.env` were
  refused; `uname -a` ran, because Claude Code treats it as read-only. That is one check, not an
  attack on the confinement. Treat it as a strong default, not a guarantee.
- Anything your own Claude Code settings allow (`~/.claude/settings.json`) is allowed here too.
- The agent inherits your environment variables, as it does when you start it in a terminal.
- A project's `CLAUDE.md` is still read by Claude Code as instructions.
- The agent reads your notes and rules, and also file names, clip metadata and anything else in the
  project. Text in a project you did not write can try to steer it (prompt injection). The
  permission list bounds what it can then do; it does not stop it being misled.
- Library folders outside the project that you have approved can be read through `cutvelvet grid`
  and `cutvelvet probe` (frames and clip details of video files only).
- Other agents (Codex, Gemini, a custom command) are untested templates and do not have these
  limits. What they may do is whatever their command line says. Read it in Settings before you use
  one, and prefer the agent's own sandbox mode.

So: run the agent on your own projects and footage, or on ones you have looked through. If you want
a hard boundary, run cutvelvet inside a container, a VM or your agent's own sandbox mode.

Each run writes its prompt and the agent's output to `.cutvelvet/runs/` in the project. cutvelvet
does not write environment variables there, but whatever the agent prints is kept. `.cutvelvet/` is
a cache and is safe to delete. Do not publish it with a project.

## Checking it yourself

```
python tests/security_test.py
```

starts the real server on a throw-away project and checks the properties above: cross-site and
token-less requests refused, path traversal and escaping links refused, a project's own command and
prompt not used unapproved, nothing from the project folder executed.

## Reporting a problem

Please use GitHub's private vulnerability reporting for this repository (the **Security** tab, then
**Report a vulnerability**). For anything that is not sensitive, open an issue.
