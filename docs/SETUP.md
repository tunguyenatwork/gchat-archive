# Google Cloud setup (once, ~5 minutes)

You create your own OAuth client so the archive talks to Google as *you*. Nothing is shared with anyone else, and access can be revoked at any time.

Cost: **free**. No billing account is required for these APIs at personal volumes.

---

## 1. Create a project

1. Open <https://console.cloud.google.com/>
2. Top bar → project selector → **New project**
3. Name it e.g. `chat-archive` → **Create**, then make sure it is selected

## 2. Enable the two APIs

Go to **APIs & Services → Library** and enable, one at a time:

| API | Why |
|---|---|
| **Google Chat API** | reading spaces and messages |
| **Google Drive API** | exporting Google Docs / Sheets / Slides shared in chats |

Each: search the name → open it → **Enable**.

> Google Chat API may ask you to configure a Chat app. You do not need one — the
> API works for reading your own messages without configuring an app.

## 3. OAuth consent screen

**APIs & Services → OAuth consent screen**

1. User type: **External** → **Create**
2. App name: `chat-archive`; support email: your address; developer contact: your address → **Save and continue**
3. **Scopes** → **Add or remove scopes** → add these four:

   ```
   https://www.googleapis.com/auth/chat.messages
   https://www.googleapis.com/auth/chat.spaces.readonly
   https://www.googleapis.com/auth/drive
   https://www.googleapis.com/auth/gmail.readonly
   ```

   (`drive` is needed to export Google Docs/Sheets; `gmail.readonly` only to read your own email address.)
4. **Test users** → **Add users** → add the Google account whose chats you want to archive
5. Save

> While the app is in **Testing** mode, refresh tokens expire after **7 days**.
> If the sync starts failing with `invalid_grant`, run `gchat-archive login`
> again — or publish the app (**Publish app**) to keep the token alive
> indefinitely. Publishing an unverified app for personal use is fine; Google
> will show an "unverified app" warning at consent, which is expected.

## 4. Create the OAuth client

**APIs & Services → Credentials → Create credentials → OAuth client ID**

- Application type: **Desktop app**
- Name: `chat-archive`
- **Create**, then **Download JSON**

Save it as:

```bash
# macOS / Linux
mkdir -p ~/.gchat-archive
mv ~/Downloads/client_secret_*.json ~/.gchat-archive/client_secret.json
```

```powershell
# Windows
New-Item -ItemType Directory -Force "$HOME\.gchat-archive" | Out-Null
Move-Item "$HOME\Downloads\client_secret_*.json" "$HOME\.gchat-archive\client_secret.json"
```

Or point at it wherever it lives:

```bash
export GCHAT_CLIENT_SECRET=/path/to/client_secret.json        # macOS/Linux
setx  GCHAT_CLIENT_SECRET "C:\path\to\client_secret.json"     # Windows
```

## 5. Sign in

```bash
gchat-archive login
```

A browser opens Google's consent screen. Choose your account, accept the scopes.
The token lands in `~/.gchat-archive/tokens/<account>.json` (mode `0600`) and you
are returned to a local page. Add more accounts by running `login` again and
picking a different Google account.

Then:

```bash
gchat-archive sync
gchat-archive serve
```

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `access_denied` at consent | Your account isn't listed under **Test users** |
| `invalid_grant` after ~7 days | Testing-mode token expiry → `gchat-archive login` again, or publish the app |
| `redirect_uri_mismatch` | The client is not type **Desktop app**; recreate it as one |
| `403 Chat API has not been used in project…` | Enable the Google Chat API (step 2) — it can take a minute to propagate |
| `no OAuth client secret found at …` | `client_secret.json` is not where the app expects; see step 4 |
| Sync finds few/no conversations | You signed in with a different account than the one in the chats |
| Google Docs return no text | Drive API not enabled, or the `drive` scope was not granted → `login` again |

Revoke access at any time: <https://myaccount.google.com/permissions> → *chat-archive* → Remove access.
