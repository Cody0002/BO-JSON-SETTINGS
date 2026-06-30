"""
Google Drive upload helper — OAuth2 user account (InstalledAppFlow).

First run opens a browser for Google login and saves a token to token.json.
Subsequent runs reuse the saved token (auto-refreshed when expired).

Required files:
    credentials.json  - OAuth2 Client ID downloaded from Google Cloud Console
                        (APIs & Services → Credentials → Create → OAuth 2.0 Client ID → Desktop app)
    token.json        - auto-created after first login (do not commit to git)
"""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

_SCOPES = ["https://www.googleapis.com/auth/drive.file"]
_MIME_FALLBACK = "application/octet-stream"


def build_drive_service(
    credentials_file: str = "credentials.json",
    token_file: str = "token.json",
):
    creds: Optional[Credentials] = None

    if Path(token_file).exists():
        creds = Credentials.from_authorized_user_file(token_file, _SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(credentials_file, _SCOPES)
            creds = flow.run_local_server(port=0)
        Path(token_file).write_text(creds.to_json(), encoding="utf-8")

    return build("drive", "v3", credentials=creds, cache_discovery=False)


def upload_file(
    service,
    local_path: Path,
    folder_id: str,
    drive_filename: Optional[str] = None,
    share_with: Optional[list] = None,
) -> dict:
    name = drive_filename or local_path.name
    mime_type = mimetypes.guess_type(name)[0] or _MIME_FALLBACK

    file_metadata = {"name": name, "parents": [folder_id]}
    media = MediaFileUpload(str(local_path), mimetype=mime_type, resumable=True)

    result = (
        service.files()
        .create(
            body=file_metadata,
            media_body=media,
            fields="id, name, webViewLink",
            supportsAllDrives=True,
        )
        .execute()
    )

    for email in (share_with or []):
        service.permissions().create(
            fileId=result["id"],
            body={"type": "user", "role": "reader", "emailAddress": email},
            sendNotificationEmail=False,
        ).execute()

    return result
