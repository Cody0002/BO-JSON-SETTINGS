"""
Full pipeline: *config.zip / *config.json → weekly_jsons → dashboard_data.json → overwrite Drive.

Steps
-----
1. Extract zip (or copy json) → weekly_jsons/kz_config_YYYYMMDD.json
2. Prune weekly_jsons to MAX_WEEKS most recent files
3. Build dashboard_data.json from all remaining weekly JSONs
4. Overwrite the existing Drive file (keeps same ID, name, location)
5. Delete the source zip
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from googleapiclient.http import MediaFileUpload

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

ROOT = Path(__file__).parent.parent
WEEKLY_JSONS_DIR = ROOT / "weekly_jsons"
DASHBOARD_JSON = ROOT / "dashboard_data.json"

# Google Drive file to overwrite (the shared dashboard file)
DASHBOARD_DRIVE_FILE_ID = "1GbojsFtJ2DZuC9yGDWIMWpN1T-qOIcie"

MAX_WEEKS = 6

# ---------------------------------------------------------------------------
# Import pipeline scripts from the project root
# ---------------------------------------------------------------------------

sys.path.insert(0, str(ROOT))
from extract_kz_config import extract          # noqa: E402
from prep_kz_dashboard import process_week, sanitize  # noqa: E402


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

def _date_from_name(filename: str) -> str:
    m = re.search(r"(\d{8})", filename)
    return m.group(1) if m else datetime.today().strftime("%Y%m%d")


class DuplicateWeekError(Exception):
    """Raised when the upload's snapshot date already exists in weekly_jsons/."""

    def __init__(self, week: str, existing_path: Path):
        self.week = week
        self.existing_path = existing_path
        super().__init__(f"Week {week} already exists ({existing_path.name})")


def step_extract(local_path: Path) -> Path:
    """Extract zip → weekly JSON, or copy JSON straight in."""
    WEEKLY_JSONS_DIR.mkdir(exist_ok=True)
    snap = _date_from_name(local_path.name)
    out = WEEKLY_JSONS_DIR / f"kz_config_{snap}.json"

    if local_path.suffix.lower() == ".zip":
        print(f"  Extracting {local_path.name} → {out.name} ...")
        extract(zip_path=str(local_path), out_path=str(out))
    else:
        print(f"  Copying {local_path.name} → {out.name} ...")
        shutil.copy2(local_path, out)

    return out


def step_prune(max_weeks: int = MAX_WEEKS) -> None:
    """Keep only the N most recent files in weekly_jsons/."""
    files = sorted(WEEKLY_JSONS_DIR.glob("*.json"))
    stale = files[:-max_weeks] if len(files) > max_weeks else []
    for f in stale:
        f.unlink()
        print(f"  Pruned old weekly JSON: {f.name}")


def step_build_dashboard() -> tuple[Path, list]:
    """Read all weekly JSONs and write dashboard_data.json."""
    files = sorted(WEEKLY_JSONS_DIR.glob("*.json"))
    if not files:
        raise RuntimeError("No weekly JSONs found — cannot build dashboard.")

    weeks = []
    for fp in files:
        print(f"  Processing {fp.name} ...")
        weeks.append(process_week(str(fp)))

    weeks.sort(key=lambda w: w["week"])
    weeks = sanitize(weeks)

    DASHBOARD_JSON.write_text(
        json.dumps(weeks, separators=(",", ":"), allow_nan=False),
        encoding="utf-8",
    )
    size_kb = DASHBOARD_JSON.stat().st_size / 1024
    print(f"  dashboard_data.json → {size_kb:.0f} KB, {len(weeks)} week(s)")
    return DASHBOARD_JSON, weeks


def step_overwrite_drive(service, local_path: Path, file_id: str = DASHBOARD_DRIVE_FILE_ID) -> dict:
    """Overwrite an existing Drive file's content (same ID, permissions, sharing)."""
    media = MediaFileUpload(str(local_path), mimetype="application/json", resumable=True)
    result = (
        service.files()
        .update(
            fileId=file_id,
            media_body=media,
            fields="id, name, webViewLink",
        )
        .execute()
    )
    return result


def step_cleanup_zip(local_path: Path) -> None:
    """Delete the source zip after processing."""
    try:
        local_path.unlink()
        print(f"  Deleted source zip: {local_path.name}")
    except OSError as exc:
        print(f"  Warning: could not delete {local_path.name}: {exc}")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run(service, local_path: Path) -> dict:
    """
    Run the full pipeline for a downloaded *config.zip or *config.json.

    Returns a dict with:
        drive_link   — webViewLink of the overwritten dashboard file
        weeks_count  — number of data points in the dashboard
        latest_week  — most recent week string (YYYY-MM-DD)
    """
    print(f"\n=== Pipeline: {local_path.name} ===")

    snap = _date_from_name(local_path.name)
    existing = WEEKLY_JSONS_DIR / f"kz_config_{snap}.json"
    if existing.exists():
        print(f"  Week {snap} already present ({existing.name}) — skipping.")
        raise DuplicateWeekError(snap, existing)

    out_json = step_extract(local_path)
    step_prune(MAX_WEEKS)
    dashboard_path, weeks = step_build_dashboard()
    drive_result = step_overwrite_drive(service, dashboard_path)

    if local_path.suffix.lower() == ".zip":
        step_cleanup_zip(local_path)

    latest_week = weeks[-1]["week"] if weeks else "unknown"
    print(f"=== Done. Dashboard updated: {len(weeks)} weeks, latest {latest_week} ===\n")

    return {
        "drive_link": drive_result.get("webViewLink", ""),
        "drive_id": drive_result.get("id", ""),
        "weeks_count": len(weeks),
        "latest_week": latest_week,
    }
