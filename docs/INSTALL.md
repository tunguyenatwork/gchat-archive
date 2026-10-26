# Installing, for a first-time `pipx` user

`pipx` installs Python *applications* the way Homebrew installs macOS apps: each one gets its own
private environment, its command lands on your `PATH`, and nothing it needs can break something else
you have installed. You install `pipx` once and then forget about it.

If you would rather not use `pipx`, the "plain Python" route is further down — the app runs the same.

---

## 1. Check you have Python 3.10 or newer

```bash
python3 --version        # macOS / Linux
py -3 --version          # Windows
```

Anything below `3.10` (CentOS 7, Ubuntu 20.04, older macOS) needs a newer Python first. Easiest:

```bash
# macOS
brew install python@3.12
# Linux (Debian/Ubuntu)
sudo apt install python3.12 python3.12-venv
# or, on any platform, without touching the system Python
curl -LsSf https://astral.sh/uv/install.sh | sh && uv python install 3.12
```

## 2. Install `pipx` once

| Platform | Command |
|---|---|
| macOS | `brew install pipx` |
| Debian / Ubuntu | `sudo apt install pipx` |
| Fedora | `sudo dnf install pipx` |
| Arch | `sudo pacman -S python-pipx` |
| Any, via pip | `python3 -m pip install --user pipx` |
| Windows | `py -m pip install --user pipx` |

Then put `pipx`'s directories on your `PATH` — this is the step people skip:

```bash
pipx ensurepath          # Windows: py -m pipx ensurepath
```

**Open a new terminal** (or run `exec zsh` / `source ~/.zshrc`) so the change takes effect, then check:

```bash
pipx --version
```

If that says *command not found*, see [TROUBLESHOOTING.md](TROUBLESHOOTING.md#pipx-command-not-found).

## 3. Install gchat-archive

```bash
pipx install "gchat-archive[text]"
```

The quotes matter: `[` and `]` are glob characters in zsh and bash, and an unquoted
`gchat-archive[text]` fails with `no matches found`.

`[text]` pulls the four document parsers (pypdf, python-docx, openpyxl, beautifulsoup4) so search can
look *inside* attached PDFs and sheets. Without it everything still works — you just can't find text
inside attachments. Add them later with:

```bash
pipx inject gchat-archive pypdf python-docx openpyxl beautifulsoup4
```

## 4. Check the install

```bash
gchat-archive --version     # gchat-archive 0.2.1 (python 3.12.4)
gchat-archive doctor
```

`doctor` prints the data directory, whether your OAuth client is present, which scheduler it found, and
which optional parsers are installed. It is read-only — safe to run any time.

```
python      : 3.12.4
data dir    : /Users/you/.gchat-archive (missing)      ← created on first run
client secret: /Users/you/.gchat-archive/client_secret.json MISSING
scheduler   : crontab available
accounts    : 0 | active: -
pypdf       : ok
```

## 5. First run

```bash
gchat-archive serve --open
```

A browser opens on `http://127.0.0.1:8765`. A fresh install shows the **Connect Google** page: upload
your `client_secret.json`, sign in, sync. See the README's *Set up (in the browser)* section, or the
longer [SETUP.md](SETUP.md).

---

## Upgrading

```bash
pipx upgrade gchat-archive        # or: pipx upgrade-all
```

Nothing in `~/.gchat-archive` is touched by an upgrade; your archive, rules and password stay.

## Plain Python instead of pipx

If `pipx` is unavailable, or you prefer a virtual environment you control:

```bash
python3 -m venv ~/.venvs/gchat
~/.venvs/gchat/bin/pip install "gchat-archive[text]"
~/.venvs/gchat/bin/gchat-archive serve --open
```

Add `alias gchat-archive=~/.venvs/gchat/bin/gchat-archive` to your shell profile if you want to type
the short form. On Windows the paths are `%USERPROFILE%\.venvs\gchat\Scripts\`.

`python3 -m pip install --user gchat-archive` also works, but on many systems it refuses with
`error: externally-managed-environment` (PEP 668) — that is the OS protecting its own Python. Use one
of the routes above instead.

---

## Removing it completely

Uninstall the command, then (only if you also want the data gone) the archive:

```bash
# 1. the program
pipx uninstall gchat-archive        # or: pip3 uninstall gchat-archive / py -m pip uninstall gchat-archive

# 2. scheduled jobs and the login service, if you installed them
gchat-archive schedule pause        # removes the crontab entry / Task Scheduler job
gchat-archive service uninstall     # removes the launchd / systemd / Task Scheduler entry

# 3. the archive itself — this deletes your local copy of everything
rm -rf ~/.gchat-archive             # Windows: rmdir /s %USERPROFILE%\.gchat-archive
```

Step 3 is irreversible unless you made a backup first (Settings → Backup & restore). Google Chat is
never modified by any of this — the archive is only ever a local copy.

Leftovers worth knowing about if something lingers:

| What | Where |
|---|---|
| launchd service (macOS) | `~/Library/LaunchAgents/com.gchat-archive.serve.plist` |
| systemd unit (Linux) | `~/.config/systemd/user/gchat-archive.service` |
| Windows task | `schtasks /Query /TN "gchat-archive serve"` |
| crontab line | `crontab -l` (our line ends with `# gchat-archive sync`) |
| pipx venvs | `pipx list`, removed by `pipx uninstall` |
| pip --user scripts | `~/Library/Python/*/bin`, `~/.local/bin`, `%APPDATA%\Python\Python*\Scripts` |

## Clean reinstall

```bash
gchat-archive backup --kind config --out ~/gca-config.tar.gz    # keep credentials + rules
pipx uninstall gchat-archive && rm -rf ~/.gchat-archive
pipx install "gchat-archive[text]"
gchat-archive serve --open                                      # upload a backup to restore
```
