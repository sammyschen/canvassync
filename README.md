<h1 align="center">canvassync</h1>

Automatically downloads your lecture files from Canvas and organises them
into a clean local folder tree. It's the Canvas version of
[bbsync](https://github.com/sammyschen/bbsync) and has the same features. It
defaults to Imperial College London (`canvas.imperial.ac.uk`) but works with
**any Canvas instance**: see [Using a different institution](#using-a-different-institution).
Works on **macOS and Windows**.

- Mirrors each course's **Modules** under `Documents/CanvasNotes/` (one folder per module, with subfolders for module text headers)
- Downloads lecture slides, notes and problem sheets, plus files linked from Canvas pages and assignment descriptions
- Puts course files that no module links to under `Other files/`, keeping their Canvas folder layout (only when the course's Files tab is visible to you)
- Collects Panopto / video links into a `videos.md` per course (videos aren't downloaded)
- Never re-downloads unchanged files and picks up updated versions automatically
- Runs in the background every 4 hours (launchd on macOS, Task Scheduler on Windows)
- **Full-text search** across every PDF, slide deck, Word doc and notebook:
  `canvassync search "divergence theorem"` tells you the course, file and page
- **Web dashboard**: `canvassync dashboard` opens a local page that does
  everything above with buttons: sign in, sync, search, browse files, toggle courses,
  manage the schedule, and watch live progress. You don't need the terminal after setup.

## Setup (once)

Requires Python 3.11+ ([python.org](https://www.python.org/downloads/)).

**macOS** (Terminal):

```bash
git clone https://github.com/sammyschen/canvassync
cd canvassync
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/playwright install chromium

.venv/bin/canvassync login      # opens a browser — sign in with your institution's SSO + MFA
.venv/bin/canvassync sync       # first download (may take a while)
.venv/bin/canvassync schedule install   # auto-sync every 4h from now on
```

Optional alias so you can just type `canvassync`:

```bash
echo "alias canvassync=\"$PWD/.venv/bin/canvassync\"" >> ~/.zshrc
```

**Windows** (PowerShell):

```powershell
git clone https://github.com/sammyschen/canvassync
cd canvassync
py -m venv .venv
.venv\Scripts\pip install -e .
.venv\Scripts\playwright install chromium

.venv\Scripts\canvassync login
.venv\Scripts\canvassync sync
.venv\Scripts\canvassync schedule install
```

Optional: put canvassync on your PATH so you can just type `canvassync`
(run from the `canvassync` folder, then open a new PowerShell window):

```powershell
[Environment]::SetEnvironmentVariable("Path", [Environment]::GetEnvironmentVariable("Path", "User") + ";$PWD\.venv\Scripts", "User")
```

## Commands

| Command | What it does |
|---|---|
| `canvassync login` | Opens a browser so you can sign in once. `--url` sets the Canvas instance (first run only). The session is saved and reused headlessly for weeks. |
| `canvassync sync` | Downloads new files right now. Files you already have and pages that haven't been edited are skipped without asking Canvas, so a repeat sync takes seconds. `--full` re-checks everything. |
| `canvassync courses` | Lists courses. Use `--disable "Maths"` / `--enable "Maths"` to choose which ones sync. |
| `canvassync index` | Builds the search index. You only run it once; after that it updates itself after each sync. |
| `canvassync search TERMS` | Searches all notes. `--course "Quantum"` filters by course, `-n 20` shows more results. |
| `canvassync schedule install` | Installs the background job. Also `uninstall` / `status`. |
| `canvassync status` | Shows the destination folder, last sync time and schedule state. |
| `canvassync dashboard` | Opens the web dashboard, which has every command above. |

## Dashboard

```bash
canvassync dashboard      # serves http://127.0.0.1:8766 and opens your browser
```

The server binds to `127.0.0.1` only and rejects requests from any other host
or origin, so nothing is exposed to the network. It uses port 8766 so it can
run alongside bbsync's dashboard (8765). `--port` picks another port, and
`--no-browser` skips opening a tab.

## Folder layout

```
CanvasNotes/
  PHYS50004 - Quantum Physics/
    Week 1 - Postulates/          ← a module
      Lecture 1.pdf
      Problem sheets/             ← a text header inside the module
        Problem Sheet 1.pdf
    Assignments/
      Coursework 1/brief.pdf      ← files linked from the assignment description
    Other files/                  ← Files-tab files no module links to
    videos.md
```

Each Canvas file is downloaded once, into the first place it appears. If two
different files share a name in one folder, the second one gets its Canvas
file id appended, e.g. `notes (123456).pdf`.

## Using a different institution

canvassync talks to Canvas's own REST API, so it works on any Canvas
deployment, not just Imperial's. Point it at your institution's Canvas URL
the first time you sign in:

```bash
canvassync login --url https://canvas.your-university.edu
```

## Configuration

`~/.canvassync/config.toml` (macOS) / `C:\Users\<you>\.canvassync\config.toml` (Windows),
created after first login:

```toml
dest = "/Users/you/Documents/CanvasNotes"      # where files go
interval_hours = 4                              # background sync frequency
base_url = "https://canvas.imperial.ac.uk"      # your institution's Canvas
# anthropic_api_key = "sk-ant-..."              # optional: AI page summaries in the dashboard

[courses."9819"]
name = "Introduction to Machine Learning"
enabled = true
```

After changing `interval_hours`, run `canvassync schedule install` again to apply it.

## Searching your notes

After the one-off `canvassync index`, finding any concept takes about a second:

```
$ canvassync search perturbation theory --course Quantum
1. Week 5/Lecture 14.pdf  (p.3, p.7)
   PHYS50004 - Quantum Physics
   p.3: … the first-order perturbation correction to the energy …
```

Search terms are stemmed and implicitly ANDed. Use double quotes for exact
phrases and FTS5 operators (`OR`, `NEAR`) for advanced queries.

## When the session expires

SSO sessions usually last for weeks, and canvassync quietly refreshes them on
each run. When one finally expires, the background sync sends a desktop
notification. Run `canvassync login` again to sign back in.

## Troubleshooting

- **Logs**: background runs log to `~/.canvassync/logs/sync.log`.
- **"sync failed: ... ProcessSingleton"**: two syncs ran at once and the browser
  profile was locked. The next scheduled run will succeed.
- **A course downloads nothing**: its modules may be locked or unpublished, or
  its files may only be linked from places canvassync doesn't scan
  (quizzes, discussions).
- **Start fresh**: delete `~/.canvassync/manifest.json` to force re-downloading
  everything, or `~/.canvassync/browser-profile/` to force a fresh login.

## How it works

Playwright keeps a persistent Chromium profile in `~/.canvassync/browser-profile`,
so your SSO cookies survive between runs. Every Canvas call goes through that
browser context to Canvas's REST API (`/api/v1/...`, the same endpoints the
Canvas web app uses), so canvassync only ever sees content your account can
already access. For each course it reads the module list, the pages those
modules link to, the assignment descriptions, the course tabs (to find
Panopto) and, if visible, the Files area. `~/.canvassync/manifest.json`
records every downloaded file by its Canvas id, plus the file links found on
each page. A repeat sync doesn't look up files it already has, because Canvas
gives a re-uploaded file a new id. It also asks Canvas's GraphQL API once per
course for each page's last-edit time and only re-reads pages that changed.
`canvassync sync --full` skips both shortcuts.

## License

MIT — see [LICENSE](LICENSE).
