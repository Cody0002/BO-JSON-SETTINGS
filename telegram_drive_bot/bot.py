"""
Telegram → Google Drive bot.

Joins a Telegram group and watches for .zip or .json file uploads.
Each matching file is downloaded and immediately uploaded to a Google Drive folder.

Setup:
    pip install -r requirements.txt

    # Authenticate once with your Google account:
    gcloud auth application-default login --scopes="openid,https://www.googleapis.com/auth/userinfo.email,https://www.googleapis.com/auth/drive.file"

    set TELEGRAM_BOT_TOKEN=123456:ABC-your-token
    set GOOGLE_CREDENTIALS_FILE=credentials.json
    set GOOGLE_DRIVE_FOLDER_ID=1aBcDeFgHiJkLmNo...
    python bot.py
    # First run opens a browser for Google login; token.json is saved automatically after.

Optional env vars:
    TELEGRAM_DOWNLOAD_DIR       - local staging folder (default: telegram_downloads)
    TELEGRAM_ALLOWED_CHAT_IDS   - comma-separated chat IDs to restrict (default: all)
    TELEGRAM_REPLY_ON_UPLOAD    - true/false, reply in group after upload (default: true)
    TELEGRAM_KEEP_LOCAL_COPY    - true/false, keep the downloaded file (default: false)
    TELEGRAM_STATE_PATH         - path to progress JSON (default: bot_state.json)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Set
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlencode

from drive_uploader import build_drive_service
import pipeline as _pipeline


def _load_env_file(path: Path = Path(".env")) -> None:
    """Load key=value pairs from a .env file into os.environ (does not override existing vars)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.split("#")[0].strip()  # strip inline comments
        if key and key not in os.environ:
            os.environ[key] = value


_load_env_file(Path(__file__).parent / ".env")


# ---------------------------------------------------------------------------
# File-type detection
# ---------------------------------------------------------------------------

def is_target_document(document: Dict[str, Any]) -> bool:
    file_name = str(document.get("file_name") or "").lower()
    return file_name.endswith("config.zip") or file_name.endswith("config.json")


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def load_state(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"next_update_id": None, "uploaded_file_unique_ids": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("next_update_id", None)
            data.setdefault("uploaded_file_unique_ids", [])
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"next_update_id": None, "uploaded_file_unique_ids": []}


def save_state(path: Path, state: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Telegram API helpers
# ---------------------------------------------------------------------------

def telegram_api(token: str, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    query = f"?{urlencode(params)}" if params else ""
    url = f"https://api.telegram.org/bot{token}/{method}{query}"
    request = Request(url, headers={"User-Agent": "telegram-drive-bot/1.0"})

    with urlopen(request, timeout=90) as response:
        payload = json.loads(response.read().decode("utf-8"))

    if not payload.get("ok"):
        raise RuntimeError(f"Telegram API error for {method}: {payload}")

    result = payload.get("result")
    return result if isinstance(result, dict) else {"items": result}


def iter_updates(token: str, offset: Optional[int], timeout_seconds: int) -> Iterable[Dict[str, Any]]:
    params: Dict[str, Any] = {
        "timeout": timeout_seconds,
        "allowed_updates": json.dumps(["message", "channel_post"]),
    }
    if offset is not None:
        params["offset"] = offset

    result = telegram_api(token, "getUpdates", params)
    items = result.get("items", [])
    return items if isinstance(items, list) else []


def download_telegram_file(token: str, file_path: str, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://api.telegram.org/file/bot{token}/{file_path}"
    request = Request(url, headers={"User-Agent": "telegram-drive-bot/1.0"})
    tmp = output_path.with_suffix(output_path.suffix + ".part")

    with urlopen(request, timeout=300) as response, tmp.open("wb") as out:
        while chunk := response.read(1024 * 1024):
            out.write(chunk)

    tmp.replace(output_path)


def send_reply(token: str, chat_id: int, message_id: int, text: str) -> None:
    telegram_api(
        token,
        "sendMessage",
        {
            "chat_id": chat_id,
            "reply_to_message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
        },
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_message(update: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    for key in ("message", "channel_post"):
        msg = update.get(key)
        if isinstance(msg, dict):
            return msg
    return None


def sanitize_filename(name: str) -> str:
    name = Path(name).name.strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = re.sub(r"\s+", " ", name)
    return name or "telegram_upload"


def staging_path(download_dir: Path, message: Dict[str, Any], document: Dict[str, Any]) -> Path:
    chat = message.get("chat") or {}
    chat_id = chat.get("id", "unknown")
    raw_date = message.get("date")
    dt = datetime.fromtimestamp(raw_date, tz=timezone.utc) if isinstance(raw_date, int) else datetime.now(tz=timezone.utc)
    timestamp = dt.strftime("%Y%m%d_%H%M%S")
    unique_id = str(document.get("file_unique_id") or document.get("file_id") or "file")
    file_name = sanitize_filename(str(document.get("file_name") or "upload"))
    return download_dir / str(chat_id) / f"{timestamp}_{unique_id}_{file_name}"


def parse_allowed_chat_ids(raw: str) -> Optional[Set[int]]:
    if not raw.strip():
        return None
    return {int(x.strip()) for x in raw.split(",") if x.strip()}


# ---------------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------------

@dataclass
class PipelineResult:
    chat_id: int
    message_id: int
    original_name: str
    drive_link: str
    weeks_count: int
    latest_week: str
    duplicate: bool = False


def process_document(
    token: str,
    message: Dict[str, Any],
    download_dir: Path,
    seen_ids: Set[str],
    drive_service,
) -> Optional[PipelineResult]:
    document = message.get("document")
    if not isinstance(document, dict) or not is_target_document(document):
        return None

    file_unique_id = str(document.get("file_unique_id") or document.get("file_id") or "")
    if file_unique_id in seen_ids:
        return None

    file_id = str(document["file_id"])
    file_info = telegram_api(token, "getFile", {"file_id": file_id})
    remote_path = str(file_info["file_path"])

    local_path = staging_path(download_dir, message, document)
    download_telegram_file(token, remote_path, local_path)

    original_name = sanitize_filename(str(document.get("file_name") or local_path.name))
    chat = message.get("chat") or {}

    try:
        result = _pipeline.run(drive_service, local_path)
    except _pipeline.DuplicateWeekError as exc:
        seen_ids.add(file_unique_id)
        local_path.unlink(missing_ok=True)
        week_fmt = f"{exc.week[:4]}-{exc.week[4:6]}-{exc.week[6:]}" if len(exc.week) == 8 else exc.week
        return PipelineResult(
            chat_id=int(chat.get("id")),
            message_id=int(message.get("message_id")),
            original_name=original_name,
            drive_link="",
            weeks_count=0,
            latest_week=week_fmt,
            duplicate=True,
        )

    seen_ids.add(file_unique_id)

    return PipelineResult(
        chat_id=int(chat.get("id")),
        message_id=int(message.get("message_id")),
        original_name=original_name,
        drive_link=result.get("drive_link", ""),
        weeks_count=result.get("weeks_count", 0),
        latest_week=result.get("latest_week", ""),
    )


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_bot(
    token: str,
    drive_service,
    download_dir: Path,
    state_path: Path,
    allowed_chat_ids: Optional[Set[int]] = None,
    poll_timeout_seconds: int = 50,
    reply_on_upload: bool = True,
    debug: bool = False,
) -> None:
    state = load_state(state_path)
    seen_ids: Set[str] = {str(x) for x in state.get("uploaded_file_unique_ids", [])}
    next_update_id: Optional[int] = state.get("next_update_id")

    print("Telegram Drive bot is running. Waiting for *config.zip / *config.json uploads...")
    print(f"Staging directory : {download_dir.resolve()}")
    print(f"Dashboard file ID : {_pipeline.DASHBOARD_DRIVE_FILE_ID}")
    if debug:
        print("[debug] Debug mode ON — all incoming updates will be printed.")

    while True:
        try:
            updates = list(iter_updates(token, next_update_id, poll_timeout_seconds))

            for update in updates:
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    next_update_id = update_id + 1
                    state["next_update_id"] = next_update_id

                if debug:
                    print(f"[debug] update: {json.dumps(update)}")

                message = get_message(update)
                if message is None:
                    continue

                chat = message.get("chat") or {}
                chat_id = chat.get("id")
                chat_title = chat.get("title") or chat.get("username") or "unknown"

                if debug:
                    doc = message.get("document")
                    fname = doc.get("file_name") if doc else None
                    print(f"[debug] message from chat_id={chat_id} ({chat_title})  file={fname}")

                if allowed_chat_ids is not None and chat_id not in allowed_chat_ids:
                    if debug:
                        print(f"[debug] chat_id={chat_id} not in allowed list, skipping.")
                    continue

                try:
                    result = process_document(
                        token=token,
                        message=message,
                        download_dir=download_dir,
                        seen_ids=seen_ids,
                        drive_service=drive_service,
                    )
                except Exception as exc:
                    print(f"Error processing document: {exc}", file=sys.stderr)
                    continue

                if result is None:
                    continue

                state["uploaded_file_unique_ids"] = sorted(seen_ids)
                save_state(state_path, state)

                if result.duplicate:
                    print(f"Duplicate week: {result.original_name} → week {result.latest_week} already available, skipped.")
                else:
                    print(f"Pipeline done: {result.original_name} → dashboard updated ({result.weeks_count} weeks, latest {result.latest_week})")

                if reply_on_upload:
                    if result.duplicate:
                        send_reply(
                            token,
                            result.chat_id,
                            result.message_id,
                            f"This data is already available — week <b>{result.latest_week}</b> was already processed. No update needed.",
                        )
                    else:
                        link_part = f'\n<a href="{result.drive_link}">Open Dashboard</a>' if result.drive_link else ""
                        send_reply(
                            token,
                            result.chat_id,
                            result.message_id,
                            f"OpsAnalyst dashboard updated from <b>{result.original_name}</b>\n"
                            f"Latest snapshot: <b>{result.latest_week}</b> ({result.weeks_count} weeks){link_part}",
                        )

            save_state(state_path, state)

        except KeyboardInterrupt:
            save_state(state_path, state)
            print("\nStopped.")
            return
        except (HTTPError, URLError, TimeoutError, RuntimeError, OSError) as exc:
            print(f"Polling error: {exc}. Retrying in 10 s...", file=sys.stderr)
            time.sleep(10)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Watch a Telegram group for .zip/.json uploads and push them to Google Drive."
    )
    p.add_argument(
        "--token",
        default=os.getenv("TELEGRAM_BOT_TOKEN"),
        help="Telegram bot token (env: TELEGRAM_BOT_TOKEN).",
    )
    p.add_argument(
        "--credentials-file",
        default=os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json"),
        help="Path to the OAuth2 Client ID JSON from Google Cloud Console.",
    )
    p.add_argument(
        "--token-file",
        default=os.getenv("GOOGLE_TOKEN_FILE", "token.json"),
        help="Path where the OAuth2 token is saved after first login.",
    )
    p.add_argument(
        "--download-dir",
        default=os.getenv("TELEGRAM_DOWNLOAD_DIR", "telegram_downloads"),
        help="Local staging directory for downloads.",
    )
    p.add_argument(
        "--state-path",
        default=os.getenv("TELEGRAM_STATE_PATH", "bot_state.json"),
        help="Path to the bot state JSON file.",
    )
    p.add_argument(
        "--allowed-chat-ids",
        default=os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", ""),
        help="Comma-separated Telegram chat IDs to accept (empty = all).",
    )
    p.add_argument(
        "--reply-on-upload",
        action="store_true",
        default=os.getenv("TELEGRAM_REPLY_ON_UPLOAD", "true").lower() in {"1", "true", "yes"},
        help="Reply in the group after each successful upload (default: on).",
    )
    p.add_argument(
        "--debug",
        action="store_true",
        default=False,
        help="Print every incoming Telegram update for troubleshooting.",
    )
    return p


def main() -> int:
    args = build_parser().parse_args()

    if not args.token:
        print("Missing Telegram bot token. Set TELEGRAM_BOT_TOKEN or pass --token.", file=sys.stderr)
        return 2

    if not Path(args.credentials_file).exists():
        print(f"credentials.json not found: {args.credentials_file}", file=sys.stderr)
        print("Download it from: Google Cloud Console → APIs & Services → Credentials → OAuth 2.0 Client ID → Desktop app", file=sys.stderr)
        return 2

    drive_service = build_drive_service(args.credentials_file, args.token_file)
    allowed_chat_ids = parse_allowed_chat_ids(args.allowed_chat_ids)

    run_bot(
        token=args.token,
        drive_service=drive_service,
        download_dir=Path(args.download_dir),
        state_path=Path(args.state_path),
        allowed_chat_ids=allowed_chat_ids,
        reply_on_upload=args.reply_on_upload,
        debug=args.debug,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
