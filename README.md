# KZG BO Config — Weekly Dashboard Pipeline

Automated pipeline that receives a KZG group config export from Telegram, transforms it into a dashboard JSON, and overwrites the live OpsAnalyst Google Drive file — all in one step.

---

## How it works

```
Telegram group
     │
     │  *config.zip or *config.json
     ▼
telegram_drive_bot/bot.py          ← long-polls Telegram
     │
     ▼
telegram_drive_bot/pipeline.py
     ├── extract_kz_config.py      ← CSV → kz_config_YYYYMMDD.json
     ├── weekly_jsons/             ← retains 6 most recent snapshots
     ├── prep_kz_dashboard.py      ← all weekly JSONs → dashboard_data.json
     └── Google Drive (overwrite)  ← pushes dashboard_data.json to shared file
```

The bot replies in the Telegram group with the latest snapshot date and a link to the dashboard once the update is complete.

---

## Project structure

```
BO Json Weekly/
├── telegram_drive_bot/
│   ├── bot.py                 Telegram polling & orchestration
│   ├── pipeline.py            Full extract → build → upload pipeline
│   ├── drive_uploader.py      Google Drive OAuth2 helpers
│   ├── credentials.json       ← you provide (OAuth2 Desktop app, not committed)
│   ├── token.json             ← auto-created on first login (not committed)
│   ├── .env                   ← secrets (not committed)
│   ├── .env.example           Template for .env
│   └── requirements.txt       Python dependencies
├── extract_kz_config.py       ZIP / CSVs → single JSON (also usable standalone)
├── prep_kz_dashboard.py       Weekly JSONs → dashboard_data.json (also usable standalone)
├── weekly_jsons/              6 most recent kz_config_YYYYMMDD.json files
├── dashboard_data.json        Generated — uploaded to Drive
├── read_data.ipynb            Ad-hoc analysis notebook
├── DASHBOARD SCRIPT/          Google Apps Script (dashboard front-end)
├── DASHBOARD SCRIPT - BACKUP/ Backup of the Apps Script
└── .gitignore
```

---

## Setup

### 1. Telegram bot

1. Chat with `@BotFather` → `/newbot` → copy the **bot token**
2. `/setprivacy` → your bot → **Disable** *(so it can see files in groups)*
3. Add the bot to your Telegram group

### 2. Google OAuth2 credentials

1. [console.cloud.google.com](https://console.cloud.google.com) → enable **Google Drive API**
2. **Credentials → Create → OAuth 2.0 Client ID → Desktop app** → Download JSON
3. Rename to `credentials.json` and place inside `telegram_drive_bot/`

> **First run** opens a browser for Google login. After approving, `token.json` is saved and reused on every subsequent run.

### 3. Install dependencies

```powershell
cd "telegram_drive_bot"
..\env\Scripts\pip install -r requirements.txt
```

### 4. Configure `.env`

```powershell
cd telegram_drive_bot
copy .env.example .env
# Edit .env — fill in TELEGRAM_BOT_TOKEN
```

### 5. Run

```powershell
cd telegram_drive_bot
..\env\Scripts\python bot.py
```

---

## Standalone pipeline scripts

Both scripts are independently usable from the project root:

```powershell
# Extract a zip to a dated JSON
.\env\Scripts\python extract_kz_config.py --zip 20260629_kz-group-config.zip --out weekly_jsons\kz_config_20260629.json

# Build dashboard from all weekly JSONs
.\env\Scripts\python prep_kz_dashboard.py --folder weekly_jsons --out dashboard_data.json
```

---

## Configuration reference

| Env var | Required | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Yes | Bot token from `@BotFather` |
| `GOOGLE_CREDENTIALS_FILE` | Yes | Path to OAuth2 credentials JSON |
| `GOOGLE_TOKEN_FILE` | No | OAuth token save path (default: `token.json`) |
| `TELEGRAM_DOWNLOAD_DIR` | No | Staging folder for downloads (default: `telegram_downloads`) |
| `TELEGRAM_ALLOWED_CHAT_IDS` | No | Restrict to specific group IDs (empty = all groups) |
| `TELEGRAM_REPLY_ON_UPLOAD` | No | Reply in group after update (default: `true`) |
| `TELEGRAM_STATE_PATH` | No | State file path (default: `bot_state.json`) |

Run `python bot.py --help` for all CLI flags including `--debug`.

---

## Dashboard file

The pipeline overwrites Drive file ID `1GbojsFtJ2DZuC9yGDWIMWpN1T-qOIcie`.
To change the target file, update `DASHBOARD_DRIVE_FILE_ID` in `telegram_drive_bot/pipeline.py`.
