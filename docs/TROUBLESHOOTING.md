# Troubleshooting

Start with `gchat-archive doctor` — it reports the data directory, the OAuth client, the scheduler and
the optional parsers, and it changes nothing. Then find your symptom below.

| Symptom | Jump to |
|---|---|
| `command not found: gchat-archive` | [PATH after pip install](#command-not-found-after-pip-install) |
| `command not found: pipx` | [pipx not found](#pipx-command-not-found) |
| `no matches found: gchat-archive[text]` | [Quote the brackets](#no-matches-found) |
| `error: externally-managed-environment` | [System Python is protected](#externally-managed-environment) |
| `No matching distribution found` | [Wrong index or Python version](#no-matching-distribution-found) |
| `Address already in use` / page won't load | [Port busy](#address-already-in-use) |
| Search finds nothing inside attachments | [Missing parsers](#search-misses-text-inside-attachments) |
| Google says `access_denied` / `redirect_uri_mismatch` | [OAuth errors](#google--oauth-problems) |
| Sync stops with `invalid_grant` after a week | [Testing-mode tokens](#invalid_grant-after-about-7-days) |
| Restore does nothing, or says `needs_confirm` | [Restore confirmation](#restore-asks-before-replacing) |
| Nightly sync never runs | [Scheduler](#the-nightly-sync-never-runs) |
| Service installs but the page doesn't load | [Service conflicts](#service-installed-but-nothing-answers) |

---

## Install problems

### `command not found` after `pip install`

`pip install --user` puts the command in a directory that macOS and Linux usually don't search:

| Platform | Where the script goes |
|---|---|
| macOS, system Python | `~/Library/Python/<version>/bin` |
| macOS/Linux, `uv` or many pips | `~/.local/bin` |
| Windows | `%APPDATA%\Python\Python<ver>\Scripts` |

Either call it by full path — `~/Library/Python/3.13/bin/gchat-archive serve --open` — or add it to
your `PATH`:

```bash
# macOS, adjust 3.13 to your python version (see: python3 --version)
echo 'export PATH="$HOME/Library/Python/3.13/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc

# Linux
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc

# Windows (PowerShell)
[Environment]::SetEnvironmentVariable("Path", "$env:APPDATA\Python\Python313\Scripts;" + [Environment]::GetEnvironmentVariable("Path", "User"), "User")
```

The durable fix is `pipx`, which manages this for you: [INSTALL.md](INSTALL.md).

### `pipx` command not found

`pipx` installed, but its own directory isn't on `PATH` yet:

```bash
pipx ensurepath        # or: py -m pipx ensurepath
exec zsh               # or open a new terminal
```

If `pipx ensurepath` itself is missing, `pipx` isn't installed — see the table in
[INSTALL.md](INSTALL.md#2-install-pipx-once). Check with `python3 -m pipx --version`; if that works,
run `python3 -m pipx ensurepath`.

### `no matches found`

```
zsh: no matches found: gchat-archive[text]
```

zsh tries to expand `[text]` as a glob. Quote it:

```bash
pipx install "gchat-archive[text]"
```

Use the quotes in bash too — it is correct in both.

### `externally-managed-environment`

```
error: externally-managed-environment
```

Your distribution protects its own Python (PEP 668). Don't fight it with
`--break-system-packages`; use a tool that owns its environment:

```bash
pipx install "gchat-archive[text]"                        # recommended
# or
python3 -m venv ~/.venvs/gchat && ~/.venvs/gchat/bin/pip install "gchat-archive[text]"
```

### `No matching distribution found`

Three causes, in order of likelihood:

1. **Python too old.** 0.2.x needs Python ≥ 3.10: `python3 --version`. On 3.8/3.9 you get
   "no matching distribution" rather than a clear message. Install a newer Python
   (`brew install python@3.12`, `apt install python3.12`, or `uv python install 3.12`).
2. **A private index.** If your organisation sets `PIP_INDEX_URL` or a `pip.conf` pointing at an
   internal mirror, PyPI isn't consulted: `python3 -m pip config list`. Override for one command with
   `--index-url https://pypi.org/simple`.
3. **Off the internet or behind a proxy** — then nothing downloads; use a backup file instead.

### It installed but `gchat-archive --version` prints nothing

An old release or a broken path entry. Confirm which binary you're running:

```bash
which -a gchat-archive     # macOS/Linux
where gchat-archive        # Windows
pipx list                  # shows the version pipx installed
```

Remove duplicates (`pipx uninstall gchat-archive`, `pip3 uninstall gchat-archive`) and keep one.

---

## Running problems

### `Address already in use`

Something already holds port 8765 — often a previous run, or the login service:

```bash
lsof -nP -iTCP:8765 -sTCP:LISTEN     # macOS/Linux: who has it
gchat-archive serve --port 8770      # or just use another port
```

If the login service owns it, stop the manual copy first (the service is meant to be the only one).

### The browser doesn't open

`--open` is best-effort, especially over SSH or in a container. Open the printed URL yourself —
`http://127.0.0.1:8765`. Over SSH:

```bash
ssh -L 8765:127.0.0.1:8765 you@host     # then browse http://127.0.0.1:8765 locally
```

### Blank page / the UI looks unstyled

Usually a stale cached asset or a proxy rewriting requests. Hard-reload (`Cmd-Shift-R` /
`Ctrl-Shift-R`). The UI ships inside the package, so a reinstall fixes a missing file:

```bash
pipx reinstall gchat-archive
```

### Search misses text inside attachments

The parsers are optional. `gchat-archive doctor` prints `pypdf : missing (pip install
'gchat-archive[text]')`. Add them and re-extract:

```bash
pipx inject gchat-archive pypdf python-docx openpyxl beautifulsoup4
gchat-archive attachments extract      # then
gchat-archive build
```

Scanned PDFs with no text layer need OCR, which this tool does not do — you will find the file, not
its contents.

---

## Google / OAuth problems

### `access_denied` on the consent screen

Your Google account isn't a **test user** of the OAuth consent screen. Add it: Cloud Console →
*APIs & Services → OAuth consent screen → Test users → Add users*.

### `redirect_uri_mismatch`

The OAuth client isn't type **Desktop app**. Recreation is faster than repair: *Credentials → Create
credentials → OAuth client ID → Desktop app*, download, then upload the new file in Settings →
Google accounts → *Replace client_secret.json*.

### `403 Chat API has not been used in project …`

Enable the API: [Google Chat API](https://console.cloud.google.com/apis/library/chat.googleapis.com)
(and Drive, for attachments). Propagation takes a minute or two.

### `invalid_grant` after about 7 days

An OAuth app left in **Testing** mode expires refresh tokens weekly. Either sign in again
(`gchat-archive login`) or publish the app (*OAuth consent screen → Publish app*). Publishing an
unverified personal app is fine; Google shows a warning at consent.

### Sign-in finishes but no account appears

The consent page was completed for a different Google identity than expected (a second account in the
same browser), or the callback was blocked. Check `gchat-archive accounts list`, then try again in a
private window so only one identity is offered.

---

## Data problems

### Restore asks before replacing

That is the guardrail, not a failure. An existing archive is never overwritten silently: the panel
shows the backup's date, kind and message count, and copies the current settings to
`pre-restore-<timestamp>/`. Click **Replace this archive** to continue, or `--force` on the CLI.

### Restored, but the archive looks empty

Check which data directory the app is using — `gchat-archive doctor` prints it. A restore into
`~/.gchat-archive` is invisible to an instance started with `--data-dir` pointing elsewhere.

```bash
gchat-archive doctor | grep "data dir"
```

### The database is missing messages after a rebuild

The build refuses to replace a larger database with a smaller one (append-only guard) and prints
`Refusing to rebuild`. That means `raw/` lost files. Restore from a backup; use `--allow-shrink` only
when you intend the loss.

---

## Schedule and service

### The nightly sync never runs

- **Laptop asleep at 05:00** — it runs on the next wake; that's expected.
- **Not installed** — `gchat-archive schedule status` shows `installed: false`.
- **Paused** — Settings → Sync schedule, or `gchat-archive schedule resume`.
- **cron has no environment** — the crontab line uses the full path to the interpreter, so this is
  usually fine; if not, check `crontab -l` for the `# gchat-archive sync` line.
- **Windows** — `schtasks /Query /TN "gchat-archive sync"`.

### Service installed but nothing answers

Two servers can't share a port. Stop the hand-started one, then restart the service:

```bash
gchat-archive service status
gchat-archive service uninstall && gchat-archive service install
tail -20 ~/.gchat-archive/service.err.log
```

On macOS, launchd needs the plist to be *loaded*, not merely present:
`launchctl list | grep gchat`. On Linux, check `systemctl --user status gchat-archive`, and remember
`loginctl enable-linger $USER` if it should run while you're logged out.

---

## Still stuck

Collect these before asking for help — they answer most questions immediately:

```bash
gchat-archive --version
gchat-archive doctor
gchat-archive service status
gchat-archive schedule status
uname -a                       # Windows: ver
```

Then open an issue: <https://github.com/tunguyenatwork/gchat-archive/issues>. **Never paste tokens,
`client_secret.json` or a backup file** — the URLs and error text are enough.
