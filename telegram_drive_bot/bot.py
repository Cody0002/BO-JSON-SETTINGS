"""
Telegram → Google Drive bot.

Joins Telegram groups, records them when first seen, and watches for config.zip/config.json file uploads.
Each matching file is downloaded and immediately uploaded to a Google Drive folder.

What counts as a weekly upload:
    * any file named ``*config.zip`` / ``*config.json`` (e.g. 20260615_kz-group-config.zip)
    * any file whose name mentions the export (kz-group-config, kz_group_config, ...)
    * any .zip/.json whose caption names such a file or says "config export" /
      "group config" / "weekly config"
Files are picked up whether they are sent directly, forwarded, posted to a channel,
edited, quoted in a reply, or pinned. A .zip/.json that misses these rules gets a
reply saying it was skipped — the bot never ignores a plausible upload in silence,
and failures are reported in the group rather than only in the log.

IMPORTANT — group privacy mode:
    A bot only receives file uploads in a group when privacy mode is off there.
    BotFather's /setprivacy applies to groups joined *afterwards*; Telegram caches
    the setting per group at join time. For a group that was joined earlier, either
    promote the bot to administrator or remove and re-add it. Send /ping in a chat
    to check what the bot can see there.

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
from html import escape
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Set
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlencode


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


def load_pipeline():
    import pipeline as pipeline_module

    return pipeline_module


# ---------------------------------------------------------------------------
# File-type detection
# ---------------------------------------------------------------------------

# Telegram's Bot API refuses to serve files above 20 MB through getFile.
MAX_TELEGRAM_FILE_BYTES = 20 * 1024 * 1024

# "20260615_kz-group-config.zip", "kz_config.json", "BO-config.ZIP" ...
_CONFIG_NAME_RE = re.compile(r"config\.(?:zip|json)$", re.IGNORECASE)
# The weekly export's own name, in any spelling, anywhere in a name or caption.
_KZ_CONFIG_RE = re.compile(r"kz[-_ ]?group[-_ ]?config", re.IGNORECASE)
# A full filename mentioned inside a caption, e.g. "here is 20260615_kz-group-config.zip".
_CONFIG_TOKEN_RE = re.compile(r"[\w.\-]*config[\w.\-]*\.(?:zip|json)", re.IGNORECASE)
_CAPTION_HINTS = ("config export", "group config", "weekly config", "bo config", "bo settings")
_SUPPORTED_EXTENSIONS = (".zip", ".json")
# Clients label zips inconsistently; octet-stream is what most desktop uploads send.
_SUPPORTED_MIME = {
    "application/zip",
    "application/x-zip",
    "application/x-zip-compressed",
    "multipart/x-zip",
    "application/json",
    "text/json",
    "application/octet-stream",
}


def message_text(message: Optional[Dict[str, Any]]) -> str:
    """Every bit of text a message carries: caption, plain text, and any quote."""
    if not isinstance(message, dict):
        return ""
    quote = message.get("quote") or {}
    parts = (message.get("caption"), message.get("text"), quote.get("text"))
    return " ".join(str(p) for p in parts if p)


def target_reason(document: Dict[str, Any], message: Optional[Dict[str, Any]] = None) -> str:
    """Why this attachment counts as a weekly config upload — '' means it doesn't.

    The name alone decides it whenever the file is called ``*config.zip`` /
    ``*config.json`` or mentions the export by name. Anything else has to be a
    zip/json *and* be introduced as a config export by the caption, so an
    unrelated archive dropped in the group is left alone.
    """
    file_name = str(document.get("file_name") or "").strip()
    mime = str(document.get("mime_type") or "").strip().lower()
    text = message_text(message)

    if _CONFIG_NAME_RE.search(file_name) or _KZ_CONFIG_RE.search(file_name):
        return f"filename '{file_name}'"

    if file_name:
        supported = file_name.lower().endswith(_SUPPORTED_EXTENSIONS)
    else:
        # Telegram occasionally hands us a document with no name at all.
        supported = mime in _SUPPORTED_MIME

    if not supported:
        return ""

    if _KZ_CONFIG_RE.search(text):
        return "caption names the weekly config export"
    named = _CONFIG_TOKEN_RE.search(text)
    if named:
        return f"caption names '{named.group(0)}'"
    lowered = text.lower()
    for hint in _CAPTION_HINTS:
        if hint in lowered:
            return f"caption says '{hint}'"
    return ""


def is_target_document(document: Dict[str, Any], message: Optional[Dict[str, Any]] = None) -> bool:
    return bool(target_reason(document, message))


def looks_like_config_candidate(document: Dict[str, Any]) -> bool:
    """A zip/json that *might* have been meant as the weekly upload.

    Used to tell the group why a file was skipped instead of ignoring it silently.
    """
    file_name = str(document.get("file_name") or "").lower()
    if file_name:
        return file_name.endswith(_SUPPORTED_EXTENSIONS)
    return str(document.get("mime_type") or "").lower() in _SUPPORTED_MIME


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def load_state(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"next_update_id": None, "uploaded_file_unique_ids": [], "greeted_chat_ids": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("next_update_id", None)
            data.setdefault("uploaded_file_unique_ids", [])
            data.setdefault("greeted_chat_ids", [])
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"next_update_id": None, "uploaded_file_unique_ids": [], "greeted_chat_ids": []}


def save_state(path: Path, state: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(path)


_GROUP_TYPES = {"group", "supergroup"}
_MEMBER_STATUSES = {"member", "administrator", "creator"}
_LEFT_STATUSES = {"left", "kicked", None}


def record_known_chat(path: Path, chat: Dict[str, Any], bot_status: Optional[str] = None) -> None:
    """Add/update a chat in the known-chats registry.

    Telegram has no API to list the groups a bot belongs to, so we build the
    roster ourselves: every chat that sends the bot a message or membership
    update is recorded here (id, type, title, first/last seen). Useful for
    discovering group IDs to put in TELEGRAM_ALLOWED_CHAT_IDS.
    """
    chat_id = chat.get("id")
    if chat_id is None:
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(data, dict):
            data = {}
    except (OSError, json.JSONDecodeError):
        data = {}

    key = str(chat_id)
    now = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
    entry = data.get(key) or {}
    entry.update(
        {
            "id": chat_id,
            "type": chat.get("type"),
            "title": chat.get("title") or chat.get("username") or chat.get("first_name"),
            "last_seen": now,
        }
    )
    if bot_status:
        entry["bot_status"] = bot_status
        entry["is_member"] = bot_status in _MEMBER_STATUSES
    elif chat.get("type") in _GROUP_TYPES:
        entry.setdefault("is_member", True)
    entry.setdefault("first_seen", now)
    data[key] = entry

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def load_known_chats(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def known_group_entries(path: Path, *, current_only: bool = True) -> list[Dict[str, Any]]:
    entries = []
    for entry in load_known_chats(path).values():
        if not isinstance(entry, dict) or entry.get("type") not in _GROUP_TYPES:
            continue
        if current_only and entry.get("is_member") is False:
            continue
        entries.append(entry)
    return sorted(entries, key=lambda e: (str(e.get("title") or "").casefold(), str(e.get("id") or "")))


def format_known_groups_plain(path: Path) -> str:
    groups = known_group_entries(path)
    if not groups:
        return "No known groups yet. Telegram does not expose old group membership; the bot will record groups when it is added or when it receives an update there."

    lines = ["Known groups:"]
    for entry in groups:
        title = entry.get("title") or "unknown"
        chat_id = entry.get("id")
        status = entry.get("bot_status") or "seen"
        last_seen = entry.get("last_seen") or "unknown"
        lines.append(f"- {title} ({chat_id}) status={status} last_seen={last_seen}")
    return "\n".join(lines)


def format_known_groups_html(path: Path) -> str:
    groups = known_group_entries(path)
    if not groups:
        return (
            "No known groups yet.\n"
            "Telegram does not expose old group membership; I will record groups when I am added or receive an update there."
        )

    lines = ["<b>Known groups</b>"]
    for entry in groups[:50]:
        title = escape(str(entry.get("title") or "unknown"))
        chat_id = escape(str(entry.get("id") or "unknown"))
        status = escape(str(entry.get("bot_status") or "seen"))
        lines.append(f"- <code>{chat_id}</code> {title} ({status})")
    if len(groups) > 50:
        lines.append(f"...and {len(groups) - 50} more")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Telegram API helpers
# ---------------------------------------------------------------------------

# Connection-level errors worth retrying (TLS handshake timeouts, dropped
# sockets, transient DNS). ssl.SSLError and socket.timeout are OSError subclasses.
_TRANSIENT_ERRORS = (URLError, TimeoutError, OSError)
# HTTP statuses that are transient on Telegram's side (rate limit / gateway).
_RETRYABLE_HTTP = {429, 500, 502, 503, 504}

# A 50-second long poll held open around the clock will always collect dropped
# sockets, read timeouts and the occasional Telegram 502 — they are recovered by
# the retry below and mean nothing on their own. Logging each one buries the
# lines that matter, so they are counted here and reported once an hour instead.
_TRANSIENT_SUMMARY_SECONDS = 3600.0
_transient_events: Dict[str, int] = {}


def note_transient(method: str) -> None:
    _transient_events[method] = _transient_events.get(method, 0) + 1


def drain_transient_summary() -> Optional[str]:
    """One line covering every recovered network blip since the last call."""
    if not _transient_events:
        return None
    total = sum(_transient_events.values())
    detail = ", ".join(f"{name} x{count}" for name, count in sorted(_transient_events.items()))
    _transient_events.clear()
    return f"Network: recovered from {total} transient error(s) [{detail}] - no action needed."


def telegram_api(
    token: str,
    method: str,
    params: Optional[Dict[str, Any]] = None,
    *,
    retries: int = 3,
    backoff: float = 2.0,
    verbose: bool = False,
) -> Dict[str, Any]:
    query = f"?{urlencode(params)}" if params else ""
    url = f"https://api.telegram.org/bot{token}/{method}{query}"
    request = Request(url, headers={"User-Agent": "telegram-drive-bot/1.0"})

    last_exc: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            with urlopen(request, timeout=90) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if not payload.get("ok"):
                raise RuntimeError(f"Telegram API error for {method}: {payload}")
            result = payload.get("result")
            return result if isinstance(result, dict) else {"items": result}
        except HTTPError as exc:  # subclass of URLError — must be caught first
            if exc.code not in _RETRYABLE_HTTP or attempt >= retries:
                raise
            last_exc = exc
        except _TRANSIENT_ERRORS as exc:
            if attempt >= retries:
                raise
            last_exc = exc
        note_transient(method)
        if verbose:
            print(
                f"  Telegram {method}: transient error ({last_exc}); "
                f"retry {attempt}/{retries - 1} in {backoff * attempt:.0f}s...",
                file=sys.stderr,
            )
        time.sleep(backoff * attempt)

    raise last_exc if last_exc else RuntimeError(f"Telegram API call failed: {method}")


def iter_updates(
    token: str,
    offset: Optional[int],
    timeout_seconds: int,
    *,
    verbose: bool = False,
) -> Iterable[Dict[str, Any]]:
    params: Dict[str, Any] = {
        "timeout": timeout_seconds,
        "allowed_updates": json.dumps(list(_MESSAGE_UPDATE_KEYS) + ["my_chat_member"]),
    }
    if offset is not None:
        params["offset"] = offset

    # Generous retries: a 502 burst from Telegram should be ridden out quietly
    # rather than escalating into a "Polling error" line every time.
    result = telegram_api(token, "getUpdates", params, retries=6, verbose=verbose)
    items = result.get("items", [])
    return items if isinstance(items, list) else []


def download_telegram_file(
    token: str,
    file_path: str,
    output_path: Path,
    *,
    retries: int = 3,
    backoff: float = 2.0,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://api.telegram.org/file/bot{token}/{file_path}"
    request = Request(url, headers={"User-Agent": "telegram-drive-bot/1.0"})
    tmp = output_path.with_suffix(output_path.suffix + ".part")

    for attempt in range(1, retries + 1):
        try:
            with urlopen(request, timeout=300) as response, tmp.open("wb") as out:
                while chunk := response.read(1024 * 1024):
                    out.write(chunk)
            tmp.replace(output_path)
            return
        except _TRANSIENT_ERRORS as exc:
            tmp.unlink(missing_ok=True)  # discard the partial file before retrying
            note_transient("download")
            if attempt >= retries:
                raise
            # Kept loud: a download stalls a specific upload someone is waiting on.
            print(
                f"  Download {output_path.name}: transient error ({exc}); "
                f"retry {attempt}/{retries - 1} in {backoff * attempt:.0f}s...",
                file=sys.stderr,
            )
            time.sleep(backoff * attempt)


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


def send_message(token: str, chat_id: int, text: str) -> None:
    telegram_api(
        token,
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Every update kind that can carry a document, edits included.
_MESSAGE_UPDATE_KEYS = ("message", "edited_message", "channel_post", "edited_channel_post")
# Places a document can hide inside a message besides the attachment itself.
_NESTED_MESSAGE_KEYS = ("reply_to_message", "external_reply", "pinned_message")


def get_message(update: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    for key in _MESSAGE_UPDATE_KEYS:
        msg = update.get(key)
        if isinstance(msg, dict):
            return msg
    return None


def is_edit_update(update: Dict[str, Any]) -> bool:
    return any(key.startswith("edited_") and isinstance(update.get(key), dict) for key in _MESSAGE_UPDATE_KEYS)


@dataclass
class Attachment:
    """A document found in an update, plus where to reply about it."""

    document: Dict[str, Any]
    carrier: Dict[str, Any]        # message the file is attached to (caption, date)
    reply_message_id: int          # message the bot replies to
    nested: bool = False           # found via a quote/pin rather than sent directly

    @property
    def unique_id(self) -> str:
        return str(self.document.get("file_unique_id") or self.document.get("file_id") or "")

    @property
    def file_name(self) -> str:
        return str(self.document.get("file_name") or "")


def documents_in_message(
    message: Dict[str, Any],
    seen_ids: Set[str],
    *,
    is_edit: bool = False,
) -> list[Attachment]:
    """Every attachment in one update that is worth inspecting.

    The directly attached file always counts, so a re-upload still gets an
    answer. Files reached indirectly — a reply quoting the upload, a pinned
    upload, a caption edit — only count while we have never fetched them, so
    quoting an already-processed zip doesn't restart the flow.
    """
    chat_id = (message.get("chat") or {}).get("id")
    outer_id = message.get("message_id")
    found: list[Attachment] = []
    seen_here: Set[str] = set()

    def add(carrier: Any, *, nested: bool) -> None:
        if not isinstance(carrier, dict):
            return
        document = carrier.get("document")
        if not isinstance(document, dict) or not document.get("file_id"):
            return
        unique_id = str(document.get("file_unique_id") or document.get("file_id"))
        if unique_id in seen_here or (nested and unique_id in seen_ids):
            return
        seen_here.add(unique_id)

        # Reply on the carrier itself when it lives in this chat; an
        # external_reply points at another chat, so answer the message we got.
        carrier_id = carrier.get("message_id")
        same_chat = (carrier.get("chat") or {}).get("id") in (None, chat_id)
        reply_id = carrier_id if isinstance(carrier_id, int) and same_chat else outer_id
        if not isinstance(reply_id, int):
            return
        found.append(Attachment(document=document, carrier=carrier, reply_message_id=reply_id, nested=nested))

    add(message, nested=is_edit)
    for key in _NESTED_MESSAGE_KEYS:
        add(message.get(key), nested=True)
    return found


def build_greeting(bot_name: str) -> str:
    """Short, friendly intro the bot posts when it joins a group."""
    return (
        f"👋 Hi, I'm <b>{bot_name}</b>! 🤖\n"
        f"Just drop the weekly <b>*config.zip</b> or <b>*config.json</b> here and I'll auto-refresh the dashboard for you. ✨📊\n"
        f"Send <code>/ping</code> any time to check I can see this chat."
    )


def bot_group_membership_update(my_chat_member: Dict[str, Any], bot_id: int) -> Optional[tuple[Dict[str, Any], str]]:
    """Return the group chat and new bot status from a bot membership update."""
    new = my_chat_member.get("new_chat_member") or {}
    if (new.get("user") or {}).get("id") != bot_id:
        return None

    chat = my_chat_member.get("chat") or {}
    if chat.get("type") not in _GROUP_TYPES:
        return None

    status = new.get("status")
    return (chat, str(status)) if status else None


def bot_added_to_group(my_chat_member: Dict[str, Any], bot_id: int) -> Optional[Dict[str, Any]]:
    """Return the chat if this my_chat_member update means the bot was just added to a group.

    Fires on the transition from left/kicked (or first sight) into member/admin,
    so a plain add and a later promotion don't both count as a fresh join.
    """
    membership = bot_group_membership_update(my_chat_member, bot_id)
    if membership is None:
        return None

    chat, status = membership
    old = my_chat_member.get("old_chat_member") or {}
    if status in _MEMBER_STATUSES and old.get("status") in _LEFT_STATUSES:
        return chat
    return None


def is_command(message: Dict[str, Any], command: str, bot_username: str = "") -> bool:
    text = str(message.get("text") or "").strip()
    if not text.startswith("/"):
        return False

    first = text.split()[0].lower()
    expected = f"/{command.lower()}"
    if first == expected:
        return True
    return bool(bot_username) and first == f"{expected}@{bot_username.lower()}"


def sanitize_filename(name: str) -> str:
    name = Path(name).name.strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = re.sub(r"\s+", " ", name)
    return name or "telegram_upload"


_DATE_SEPARATORS_RE = re.compile(r"(\d{4})[-/._](\d{2})[-/._](\d{2})")


def source_label_for_snapshot(message: Dict[str, Any], document: Dict[str, Any], fallback_path: Path) -> str:
    """The string the pipeline reads the snapshot date out of.

    The filename wins when it carries a date (``20260615_kz-group-config.zip``).
    Otherwise the caption is appended so an undated file can still be dated by
    whoever posted it — ``2026-06-15`` there is normalised to ``20260615``.
    """
    file_name = sanitize_filename(str(document.get("file_name") or fallback_path.name))
    if re.search(r"\d{8}", file_name):
        return file_name

    caption = _DATE_SEPARATORS_RE.sub(r"\1\2\3", message_text(message)).strip()
    return f"{file_name} {caption}".strip() if caption else file_name


def staging_path(
    download_dir: Path,
    message: Dict[str, Any],
    document: Dict[str, Any],
    chat_id: Optional[int] = None,
) -> Path:
    if chat_id is None:
        chat_id = (message.get("chat") or {}).get("id", "unknown")
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


def format_snapshot(week: str) -> str:
    """Format a 'YYYY-MM-DD' week string as '30 Jun 2026'; pass through if unparseable."""
    try:
        return datetime.strptime(week, "%Y-%m-%d").strftime("%d %b %Y")
    except (ValueError, TypeError):
        return week


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


class TooLargeError(Exception):
    """The file exceeds the Bot API's 20 MB getFile ceiling."""


def process_document(
    token: str,
    attachment: Attachment,
    chat_id: int,
    download_dir: Path,
    seen_ids: Set[str],
    drive_service,
) -> PipelineResult:
    pipeline = load_pipeline()
    document = attachment.document
    message = attachment.carrier

    file_size = document.get("file_size")
    if isinstance(file_size, int) and file_size > MAX_TELEGRAM_FILE_BYTES:
        raise TooLargeError(f"{file_size / 1024 / 1024:.1f} MB")

    # Always download and run the full pipeline, even for a file we've seen
    # before: the pipeline dedups by snapshot week and the bot replies
    # "already processed" instead of staying silent on re-uploads.
    file_unique_id = attachment.unique_id

    file_id = str(document["file_id"])
    file_info = telegram_api(token, "getFile", {"file_id": file_id})
    remote_path = str(file_info["file_path"])

    local_path = staging_path(download_dir, message, document, chat_id=chat_id)
    download_telegram_file(token, remote_path, local_path)

    original_name = sanitize_filename(str(document.get("file_name") or local_path.name))
    snapshot_source = source_label_for_snapshot(message, document, local_path)

    try:
        result = pipeline.run(drive_service, local_path, original_name=snapshot_source)
    except pipeline.DuplicateWeekError as exc:
        seen_ids.add(file_unique_id)
        local_path.unlink(missing_ok=True)
        week_fmt = f"{exc.week[:4]}-{exc.week[4:6]}-{exc.week[6:]}" if len(exc.week) == 8 else exc.week
        return PipelineResult(
            chat_id=chat_id,
            message_id=attachment.reply_message_id,
            original_name=original_name,
            drive_link="",
            weeks_count=0,
            latest_week=week_fmt,
            duplicate=True,
        )

    seen_ids.add(file_unique_id)

    return PipelineResult(
        chat_id=chat_id,
        message_id=attachment.reply_message_id,
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
    known_chats_path: Optional[Path] = None,
    allowed_chat_ids: Optional[Set[int]] = None,
    poll_timeout_seconds: int = 50,
    reply_on_upload: bool = True,
    debug: bool = False,
) -> None:
    state = load_state(state_path)
    seen_ids: Set[str] = {str(x) for x in state.get("uploaded_file_unique_ids", [])}
    greeted_ids: Set[str] = {str(x) for x in state.get("greeted_chat_ids", [])}
    next_update_id: Optional[int] = state.get("next_update_id")
    pipeline = load_pipeline()

    can_read_all = False
    try:
        me = telegram_api(token, "getMe")
        bot_id: Optional[int] = int(me["id"])
        bot_name = me.get("first_name") or me.get("username") or "KZG BO Bot"
        bot_username = str(me.get("username") or "")
        can_read_all = bool(me.get("can_read_all_group_messages"))
    except (HTTPError, URLError, TimeoutError, RuntimeError, OSError, KeyError, ValueError) as exc:
        print(f"Warning: getMe failed ({exc}); group greetings disabled.", file=sys.stderr)
        bot_id, bot_name, bot_username = None, "KZG BO Bot", ""

    print("Telegram Drive bot is running. Waiting for *config.zip / *config.json uploads...")
    print(f"Staging directory : {download_dir.resolve()}")
    print(f"Dashboard file ID : {pipeline.DASHBOARD_DRIVE_FILE_ID}")
    if bot_id is not None:
        print(f"Bot identity      : {bot_name} (id={bot_id})")
    print(f"Allowed chats     : {'all' if allowed_chat_ids is None else sorted(allowed_chat_ids)}")
    print(f"Reads all group messages: {can_read_all}")
    if not can_read_all:
        print(
            "WARNING: privacy mode is ON — in groups the bot only receives commands and\n"
            "         replies to itself, so file uploads never arrive. Disable it via\n"
            "         BotFather (/setprivacy -> Disable), then REMOVE AND RE-ADD the bot to\n"
            "         each existing group: Telegram caches the mode per group at join time.",
            file=sys.stderr,
        )
    if debug:
        print("[debug] Debug mode ON — all incoming updates will be printed.")

    next_summary_at = time.monotonic() + _TRANSIENT_SUMMARY_SECONDS
    consecutive_failures = 0

    while True:
        try:
            updates = list(iter_updates(token, next_update_id, poll_timeout_seconds, verbose=debug))

            if time.monotonic() >= next_summary_at:
                summary = drain_transient_summary()
                if summary:
                    print(summary)
                next_summary_at = time.monotonic() + _TRANSIENT_SUMMARY_SECONDS

            for update in updates:
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    next_update_id = update_id + 1
                    state["next_update_id"] = next_update_id

                if debug:
                    print(f"[debug] update: {json.dumps(update)}")

                my_chat_member = update.get("my_chat_member")
                if isinstance(my_chat_member, dict):
                    if bot_id is not None:
                        membership = bot_group_membership_update(my_chat_member, bot_id)
                        if membership is not None and known_chats_path is not None:
                            member_chat, status = membership
                            record_known_chat(known_chats_path, member_chat, bot_status=status)
                            if debug:
                                print(f"[debug] bot status in {member_chat.get('title')} ({member_chat.get('id')}): {status}")

                        joined_chat = bot_added_to_group(my_chat_member, bot_id)
                        if joined_chat is not None:
                            cid = joined_chat.get("id")
                            allowed = allowed_chat_ids is None or cid in allowed_chat_ids
                            if allowed and str(cid) not in greeted_ids:
                                try:
                                    send_message(token, cid, build_greeting(bot_name))
                                    greeted_ids.add(str(cid))
                                    state["greeted_chat_ids"] = sorted(greeted_ids)
                                    save_state(state_path, state)
                                    print(f"Greeted new group: {joined_chat.get('title')} ({cid})")
                                except Exception as exc:
                                    print(f"Failed to greet chat {cid}: {exc}", file=sys.stderr)
                    continue

                message = get_message(update)
                if message is None:
                    continue

                chat = message.get("chat") or {}
                chat_id = chat.get("id")
                chat_title = chat.get("title") or chat.get("username") or "unknown"

                if known_chats_path is not None:
                    record_known_chat(known_chats_path, chat)

                if debug:
                    print(f"[debug] message from chat_id={chat_id} ({chat_title}) type={chat.get('type')}")

                if is_command(message, "groups", bot_username):
                    if chat.get("type") == "private":
                        text = (
                            format_known_groups_html(known_chats_path)
                            if known_chats_path is not None
                            else "Known chat registry is disabled."
                        )
                        send_message(token, int(chat_id), text)
                    elif debug:
                        print(f"[debug] /groups ignored outside private chat: chat_id={chat_id}")
                    continue

                if is_command(message, "ping", bot_username):
                    allowed = allowed_chat_ids is None or chat_id in allowed_chat_ids
                    send_reply(
                        token,
                        int(chat_id),
                        int(message["message_id"]),
                        "✅ I can see this chat.\n"
                        f"- chat_id: <code>{chat_id}</code> ({escape(str(chat.get('type')))})\n"
                        f"- accepting uploads here: <b>{'yes' if allowed else 'no'}</b>\n"
                        f"- reads all group messages: <b>{'yes' if can_read_all else 'no'}</b>",
                    )
                    continue

                # Only react to messages carrying a file. Plain text, photos,
                # stickers, joins/leaves, etc. cost nothing beyond this check.
                attachments = documents_in_message(message, seen_ids, is_edit=is_edit_update(update))
                if not attachments:
                    continue

                for attachment in attachments:
                    document = attachment.document
                    size = document.get("file_size")
                    where = "quoted/pinned" if attachment.nested else "attached"
                    print(
                        f"Document seen ({where}): chat={chat_id} ({chat_title}) "
                        f"file={attachment.file_name or '<unnamed>'} "
                        f"size={size} mime={document.get('mime_type')} "
                        f"caption={message_text(attachment.carrier)[:120]!r}"
                    )

                    if allowed_chat_ids is not None and chat_id not in allowed_chat_ids:
                        print(f"  Skipped: chat_id={chat_id} is not in TELEGRAM_ALLOWED_CHAT_IDS.")
                        continue

                    reason = target_reason(document, attachment.carrier)
                    if not reason:
                        print(f"  Skipped: not a weekly config upload ({attachment.file_name or '<unnamed>'}).")
                        # A zip/json that just missed the naming rule is worth
                        # saying out loud — silence here is what hides mistakes.
                        if reply_on_upload and looks_like_config_candidate(document):
                            send_reply(
                                token,
                                int(chat_id),
                                attachment.reply_message_id,
                                f"I saw <b>{escape(attachment.file_name or 'this file')}</b> but skipped it — "
                                "I only process the weekly export, named like "
                                "<code>20260615_kz-group-config.zip</code> "
                                "(or any <code>*config.zip</code> / <code>*config.json</code>).",
                            )
                        continue

                    print(f"  Matched: {reason} — running pipeline...")

                    try:
                        result = process_document(
                            token=token,
                            attachment=attachment,
                            chat_id=int(chat_id),
                            download_dir=download_dir,
                            seen_ids=seen_ids,
                            drive_service=drive_service,
                        )
                    except TooLargeError as exc:
                        print(f"  Error: file is {exc} — over the 20 MB Bot API limit.", file=sys.stderr)
                        if reply_on_upload:
                            send_reply(
                                token,
                                int(chat_id),
                                attachment.reply_message_id,
                                f"❌ <b>{escape(attachment.file_name or 'That file')}</b> is {escape(str(exc))} — "
                                "Telegram only lets me download files up to 20 MB. "
                                "Please send a smaller archive or share it via Drive.",
                            )
                        continue
                    except Exception as exc:
                        print(f"Error processing document: {exc}", file=sys.stderr)
                        if reply_on_upload:
                            send_reply(
                                token,
                                int(chat_id),
                                attachment.reply_message_id,
                                f"❌ I received <b>{escape(attachment.file_name or 'the file')}</b> but the update failed:\n"
                                f"<code>{escape(str(exc)[:400])}</code>",
                            )
                        continue

                    state["uploaded_file_unique_ids"] = sorted(seen_ids)
                    save_state(state_path, state)

                    if result.duplicate:
                        print(f"Duplicate week: {result.original_name} -> week {result.latest_week} already available, skipped.")
                    else:
                        print(f"Pipeline done: {result.original_name} -> dashboard updated ({result.weeks_count} weeks, latest {result.latest_week})")

                    if reply_on_upload:
                        snapshot = format_snapshot(result.latest_week)
                        dashboard_link = f'<a href="{pipeline.DASHBOARD_URL}">Open Dashboard</a>'
                        if result.duplicate:
                            send_reply(
                                token,
                                result.chat_id,
                                result.message_id,
                                f"This data is already available — snapshot <b>{snapshot}</b> was already processed. No update needed.",
                            )
                        else:
                            send_reply(
                                token,
                                result.chat_id,
                                result.message_id,
                                f"KZG BO Settings dashboard updated from <b>{escape(result.original_name)}</b>\n"
                                f"- Latest Snapshot: <b>{snapshot}</b>\n"
                                f"- {dashboard_link}",
                            )

            save_state(state_path, state)

            consecutive_failures = 0

        except KeyboardInterrupt:
            save_state(state_path, state)
            print("\nStopped.")
            return
        except (HTTPError, URLError, TimeoutError, RuntimeError, OSError) as exc:
            # Only reached once the retries inside iter_updates are exhausted, so
            # this is a real outage. Back off instead of hammering, and log the
            # first failure plus every tenth so a long outage stays one screenful.
            consecutive_failures += 1
            delay = min(10 * consecutive_failures, 300)
            if consecutive_failures == 1 or consecutive_failures % 10 == 0:
                print(
                    f"Polling error (#{consecutive_failures}): {exc}. Retrying in {delay} s...",
                    file=sys.stderr,
                )
            time.sleep(delay)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Watch Telegram groups for config.zip/config.json uploads and push them to Google Drive."
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
        "--known-chats-path",
        default=os.getenv("TELEGRAM_KNOWN_CHATS_PATH", "known_chats.json"),
        help="Path to a JSON registry of every chat the bot has seen (for discovering group IDs).",
    )
    p.add_argument(
        "--list-known-groups",
        action="store_true",
        help="Print groups currently known from known_chats.json and exit.",
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
    known_chats_path = Path(args.known_chats_path)

    if args.list_known_groups:
        print(format_known_groups_plain(known_chats_path))
        return 0

    if not args.token:
        print("Missing Telegram bot token. Set TELEGRAM_BOT_TOKEN or pass --token.", file=sys.stderr)
        return 2

    if not Path(args.credentials_file).exists():
        print(f"credentials.json not found: {args.credentials_file}", file=sys.stderr)
        print("Download it from: Google Cloud Console -> APIs & Services -> Credentials -> OAuth 2.0 Client ID -> Desktop app", file=sys.stderr)
        return 2

    from drive_uploader import build_drive_service

    drive_service = build_drive_service(args.credentials_file, args.token_file)
    allowed_chat_ids = parse_allowed_chat_ids(args.allowed_chat_ids)

    run_bot(
        token=args.token,
        drive_service=drive_service,
        download_dir=Path(args.download_dir),
        state_path=Path(args.state_path),
        known_chats_path=known_chats_path,
        allowed_chat_ids=allowed_chat_ids,
        reply_on_upload=args.reply_on_upload,
        debug=args.debug,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
