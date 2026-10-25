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

Chrome can only use a profile once, so **quit Chrome first**, then:

```bash
gchat-archive browser launch --profile "Profile 21"
```

Use the profile directory that holds the account whose chats you want
(`~/Library/Application Support/Google/Chrome/Profile 21` on macOS,
`%LOCALAPPDATA%\Google\Chrome\User Data\Profile 21` on Windows,
`~/.config/google-chrome/Profile 21` on Linux). Or start Chrome yourself:

```bash
google-chrome --remote-debugging-port=9222 --profile-directory="Profile 21"
```

Sign in if asked, and leave the Chat window open.

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

## What it does not do (yet)

- **Attachments** — the page's attachment links are not downloaded.
- **Automatic conversation switching** — v1 harvests the conversation you have
  open. Calibration lists the conversation panel entries so the next version can
  walk the list automatically.
- **Exact timestamps** — the row shows a clock time, not a date, so `createTime`
  holds what the page displayed. Day boundaries from date separators are not yet
  reconstructed into full dates.
- **Message ids** — Google's internal ids are not exposed in the DOM; ids are
  content hashes, which dedupe correctly but are not the API's ids.

For a complete archive, use the API source; use browser mode for a quick
no-setup copy, or as a fallback if the OAuth flow is blocked in your org.
