# Browser mode — no Google Cloud project

If you would rather not create an OAuth client, you can harvest Chat through the
Chrome profile you are already signed into. Same archive, same reader, same
search — only the collection method changes.

**Status: experimental.** It reads the rendered page, so it depends on Google's
markup. Calibration tells you immediately whether the current markup is
understood. The API source remains the reliable route.

| | API source | Browser source |
|---|---|---|
| Setup | Google Cloud project + OAuth client (~5 min) | none |
| Needs | token with 4 scopes | your signed-in Chrome |
| Speed | ~40 min for 580 conversations | slower; page-by-page |
| Coverage | every message the API returns | whatever the client renders while scrolling |
| Attachments | downloaded + text extracted | not collected |
| Stability | stable API | breaks when Google changes the DOM |

## Steps

```bash
pip install "gchat-archive[browser]"
```

### 1. Start Chrome with a debug port

```bash
gchat-archive browser launch --profile "Profile 21"
```

**Why a copy:** Chrome 136+ refuses `--remote-debugging-port` on the default
user-data directory. `browser launch` therefore clones that one profile
(~200 MB, once) into `<data>/chrome-profile` and starts Chrome from the copy —
your session cookies come along, so Chat is already signed in. Your normal
Chrome windows are untouched, and the copy is disposable.

Find the profile name in `chrome://version` (look for *Profile Path*), or list
the directories:

```bash
ls ~/Library/Application\ Support/Google/Chrome/Profile*        # macOS
ls "$LOCALAPPDATA/Google/Chrome/User Data/Profile*"             # Windows
ls ~/.config/google-chrome/Profile*                             # Linux
```

Leave the Chat window open; sign in if a copy ever comes up signed out.

### 2. Calibrate

Open a conversation with messages, then:

```bash
gchat-archive browser calibrate
```

It reads the page and reports what it found:

```
pane: {'tag': 'div', 'scrollHeight': 1480, 'clientHeight': 469}
rows: 8
conversations: ['Project Room', 'Dana Reed', 'Build bot']
sample: [{'tag': 'div', 'cls': '', 'text': 'Dana Reed | 9:14 AM | Kickoff…'}]
```

`pane` and `rows` are the two things that must be non-zero. The full report is
saved to `<data>/browser_report.json` — attach it to an issue if extraction looks
wrong, and the selectors can be adjusted against real markup instead of guesses.

### 3. Harvest a conversation

With the conversation open in Chrome:

```bash
gchat-archive browser harvest --conversation "project-room"
gchat-archive build
gchat-archive serve
```

It scrolls upward in steps, collecting every rendered message, deduplicating by
content, and writing `raw/<conversation>.jsonl` in the same format the API source
writes. Running it again is idempotent — already-harvested messages are skipped,
so you can re-run as you scroll further back.

## How it reads the page

Verified against the live client (Oct 2026). Messages are `<c-wiz>` elements and
the useful data lives in attributes, not the obfuscated class names:

| What | Where |
|---|---|
| message id | `data-multiplat-item-id="MSG_LIST/At/<id>"` |
| timestamp | `data-local-sort-time-msec` (epoch **ms**, so dates are exact) |
| space id | `data-p='%.@.null,["space/<SPACE_ID>",…]'` |
| sender | `[aria-label="Message <Sender>"]` inside the row |
| body | row `innerText`, stripped of the sender header, timestamps, `Add reaction` and `Read by` chrome |

Because the ids and timestamps are real, harvested rows behave exactly like
API rows: date filters work, and re-harvesting dedupes on the message id.

## What it does not do (yet)

- **Attachments** — the page's attachment links are not downloaded.
- **Automatic conversation switching** — v1 harvests the conversation you have
  open. The list is readable (`[role="listitem"]`, 96 entries on a real account),
  so walking it automatically is the next step.
- **Rows without a timestamp** — system rows like "HISTORY IS ON" carry no
  `data-local-sort-time-msec`, so their `createTime` is empty and date filters
  skip them.
- **Reactions and read receipts** — a `+1` chip or "Message viewed by…" can still
  appear in a body; reactions are not modeled.

For a complete archive, use the API source; use browser mode for a quick
no-setup copy, or as a fallback if the OAuth flow is blocked in your org.
