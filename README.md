# gchat-archive

Keep a **local, searchable copy of your Google Chat history** — every space, group chat and DM — in SQLite with full-text search, a browser UI that works offline, and a nightly sync.

Google Chat has no export button. This gives you one:

- **Every message** in every conversation, incremental sync that resumes where it stopped
- **Full-text search** over message bodies, sender names, and the text inside shared documents (PDF, DOCX, XLSX, CSV, Google Docs/Sheets/Slides)
- **Browsable offline** — a local web UI with conversation list, date filters, attachment previews and download
- **Yours only** — listens on `127.0.0.1`, never uploads anything, optional password
- **Multi-account** — several Google accounts, each with an isolated archive
- **Prune without losing anything upstream** — delete or exclude conversations and messages locally; Google Chat is never modified

macOS · Windows · Linux.

Two ways to collect, one archive:

| | **API source** (default) | **Browser source** (no setup) |
|---|---|---|
| Requires | Google Cloud OAuth client, ~5 min | only your signed-in Chrome |
| Gets | everything incl. attachments + document text | rendered messages |
| Stability | stable API | depends on Google's markup |

See [docs/BROWSER.md](docs/BROWSER.md) for the browser source.

---

## Install

Requires **Python 3.10+**.

### macOS / Linux

```bash
pipx install "gchat-archive[text]"      # recommended: isolated, on your PATH
# or
python3 -m pip install --user "gchat-archive[text]"
```

### Windows (PowerShell)

```powershell
py -m pip install --user "pipx"
py -m pipx install "gchat-archive[text]"
```

`[text]` adds the document parsers (pypdf, python-docx, openpyxl). Without it everything still works — search just won't look inside attachments.

### From a checkout

```bash
git clone https://github.com/yourname/gchat-archive.git
cd gchat-archive
python3 -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[text,dev]"
pytest
```

---

## Set up Google access (once)

You need your own OAuth client — it is free and takes about five minutes. Full walkthrough with screenshots-free steps: **[docs/SETUP.md](docs/SETUP.md)**.

Short version:

1. <https://console.cloud.google.com/> → create a project
2. Enable **Google Chat API** and **Google Drive API**
3. OAuth consent screen → External → add yourself as a test user
4. Credentials → Create credentials → **OAuth client ID** → **Desktop app**
5. Save the downloaded JSON as `~/.gchat-archive/client_secret.json`

Then:

```bash
gchat-archive login      # opens the Google consent page, stores a token
gchat-archive sync       # download everything (first run takes a while)
gchat-archive serve      # http://127.0.0.1:8765
```

---

## Commands

| Command | What it does |
|---|---|
| `gchat-archive login` | Add a Google account (OAuth loopback flow) |
| `gchat-archive sync` | Fetch new messages → attachments → rebuild the database |
| `gchat-archive sync --full` | Re-pull every conversation from scratch |
| `gchat-archive sync --check-all` | Also re-check conversations that look idle (catches edits/deletes) |
| `gchat-archive serve` | Reader UI on <http://127.0.0.1:8765> (`--port`, `--open`) |
| `gchat-archive build` | Rebuild `chats.sqlite` from downloaded messages |
| `gchat-archive attachments download [N]` | Download attachment files |
| `gchat-archive attachments stats` | What has been downloaded and extracted |
| `gchat-archive accounts list\|use\|remove` | Manage accounts |
| `gchat-archive schedule set "0 5 * * *"` | Install the nightly sync (cron on macOS/Linux, Task Scheduler on Windows) |
| `gchat-archive schedule pause\|resume\|status` | Inspect or pause the schedule |
| `gchat-archive doctor` | Check the install: paths, token, optional parsers |
| `gchat-archive browser launch` | Start Chrome with a debug port on your existing profile |
| `gchat-archive browser calibrate` | Check that the page markup is understood |
| `gchat-archive browser harvest --conversation NAME` | Harvest the open conversation, no OAuth |

`--data-dir PATH` (or `GCHAT_ARCHIVE_HOME`) moves the archive anywhere.

---

## What gets stored

```
~/.gchat-archive/
├── client_secret.json          your OAuth client (never commit)
├── accounts.json               which accounts exist, which is active
├── tokens/<slug>.json          OAuth tokens, mode 0600
└── accounts/<slug>/
    ├── raw/<space>.jsonl       append-only API responses, one JSON per line
    ├── raw/<space>.meta.json   conversation metadata
    ├── state.json              per-conversation watermarks
    ├── chats.sqlite            the searchable database (a projection of raw)
    ├── attachments/            downloaded files
    ├── attachments.jsonl       what was downloaded
    ├── attachments_text.jsonl  text extracted from those files
    └── archive_manage.json     your delete/exclude rules
```

The database is **disposable** — it is rebuilt from `raw/` minus `archive_manage.json` on every sync. Deleting a message or conversation in the UI records it in the rules file, so a rebuild cannot resurrect it, and removing the rule brings it back.

Everything stays on your machine. The only network traffic is to Google's APIs.

---

## Notes and limits

- **Scopes**: `chat.messages`, `chat.spaces.readonly`, `drive`, `gmail.readonly`. `drive` is needed to export Google Docs/Sheets; `gmail.readonly` only to learn your own address.
- **Edits and deletions upstream** are picked up when a conversation is re-read. Idle conversations are skipped for speed — run `sync --check-all` weekly if you care about old edits.
- **Images** are opt-in (Settings per account): ~1.7 GB for a 240k-message archive. They are stored, not OCR'd.
- **Video and other binaries** are skipped; their filenames are still indexed.
- **The nightly sync** needs the machine awake; a laptop that sleeps through 05:00 simply syncs on the next run.
- This is a personal archive tool, not a compliance/retention system.

---

## Security

- Binds to `127.0.0.1` by default — not reachable from your network
- Optional password (Settings → Security): PBKDF2 hash, in-memory sessions, password reset by email to the linked account
- Tokens are stored with owner-only permissions
- `client_secret.json`, tokens and the archive are in `.gitignore` — never commit them

---

## Browser source (no OAuth)

```bash
pip install "gchat-archive[browser]"
# quit Chrome, then:
gchat-archive browser launch --profile "Profile 21"
gchat-archive browser calibrate                 # open a conversation first
gchat-archive browser harvest --conversation "project-room"
gchat-archive build && gchat-archive serve
```

It drives your signed-in Chrome over the DevTools protocol, scrolls the open
conversation, and writes the same `raw/*.jsonl` the API source writes. No Google
Cloud project, no scopes, no token. Experimental: it depends on the rendered DOM,
which is why `calibrate` exists — it reports whether the current markup is
understood. Attachments are not collected in this mode. Details and limits:
[docs/BROWSER.md](docs/BROWSER.md).

## Development

```bash
pip install -e ".[text,dev]"
pytest                     # offline tests: build, FTS search, rules, shrink guard, CLI
```

Layout: `src/gchat_archive/` — `api` (Chat REST), `fetch` (incremental sync), `build` (raw → SQLite/FTS5), `attachments`, `textract`, `manage` (rules), `accounts`, `auth`, `serve` (stdlib HTTP + UI), `schedule`, `cli`. The browser app is plain HTML/CSS/JS in `src/gchat_archive/web/`, no build step.

## License

MIT
