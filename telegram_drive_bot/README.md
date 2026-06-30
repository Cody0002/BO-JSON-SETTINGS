# Telegram → Google Drive Bot

Watches a Telegram group for `.zip` and `.json` file uploads and automatically pushes them to a Google Drive folder using your own Google account.

---

## 1. Create the Telegram bot

1. Open Telegram → chat with `@BotFather`
2. Run `/newbot` → follow prompts → copy the **bot token**
3. In `@BotFather` run `/setprivacy` → choose your bot → select **Disable**
   *(Required so the bot can see file messages in groups)*
4. Add the bot to your Telegram group

---

## 2. Set up Google OAuth2 credentials

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a project (or use an existing one) → **Enable Google Drive API**
3. Go to **APIs & Services → Credentials → Create Credentials → OAuth 2.0 Client ID**
4. Choose **Desktop app** as the application type
5. Download the JSON → save it as **`credentials.json`** inside the `telegram_drive_bot/` folder
6. Copy the target **Google Drive folder ID** from the folder URL:
   `https://drive.google.com/drive/folders/<FOLDER_ID>`

> **First run** will open a browser window asking you to log in with your Google account and grant Drive access. A `token.json` is saved automatically — subsequent runs use it without prompting.

---

## 3. Install dependencies

```powershell
cd telegram_drive_bot
..\env\Scripts\pip install -r requirements.txt
```

---

## 4. Configure environment variables

```powershell
$env:TELEGRAM_BOT_TOKEN      = "123456:ABC-your-token"
$env:GOOGLE_CREDENTIALS_FILE = "credentials.json"
$env:GOOGLE_DRIVE_FOLDER_ID  = "1aBcDeFgHiJkLmNoPqRsTuVw"
```

Or copy `.env.example` → `.env`, fill in values, then load:

```powershell
Get-Content .env | ForEach-Object { $k,$v = $_ -split '=',2; if ($k -and -not $k.StartsWith('#')) { Set-Item "env:$k" $v } }
```

---

## 5. Run

```powershell
..\env\Scripts\python bot.py
```

**First run:** a browser tab opens → log in with your Google account → grant Drive access → browser shows "Authentication complete". The bot starts polling immediately after.

**Subsequent runs:** uses the saved `token.json` with no browser prompt.

---

## What happens

| Event | Bot behaviour |
|---|---|
| `.zip` or `.json` sent to the group | Downloaded → uploaded to your Drive folder → bot replies with a Drive link |
| Same file sent again (duplicate) | Skipped (tracked in `bot_state.json`) |
| Bot restarted | Resumes from last Telegram offset; already-uploaded files not re-uploaded |
| Token expired | Auto-refreshed silently |

---

## Optional settings

| Env var | Default | Description |
|---|---|---|
| `GOOGLE_TOKEN_FILE` | `token.json` | Where the OAuth token is saved after login |
| `TELEGRAM_DOWNLOAD_DIR` | `telegram_downloads` | Local staging folder (deleted after upload unless `--keep-local`) |
| `TELEGRAM_ALLOWED_CHAT_IDS` | *(all)* | Comma-separated chat IDs to restrict the bot to |
| `TELEGRAM_REPLY_ON_UPLOAD` | `true` | Send a Drive link reply in the group after each upload |
| `TELEGRAM_KEEP_LOCAL_COPY` | `false` | Keep the local file after uploading |
| `TELEGRAM_STATE_PATH` | `bot_state.json` | State file path |

Run `python bot.py --help` for all CLI flags.

---

## File structure

```
telegram_drive_bot/
├── bot.py              # Main entrypoint
├── drive_uploader.py   # Google Drive OAuth2 upload logic
├── requirements.txt    # Python dependencies
├── .env.example        # Environment variable template
├── credentials.json    # ← you provide this (do not commit)
├── token.json          # ← auto-created on first login (do not commit)
└── README.md
```

> Add `credentials.json` and `token.json` to `.gitignore` — they contain sensitive auth data.
