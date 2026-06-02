"""Google Sheets as the apartment database.

The sheet is the human-facing "база": you and your husband can open it on a
phone and edit it directly, while the bot reads and writes the same rows.
"""
import json
from typing import Optional

import gspread
from google.oauth2.service_account import Credentials

import config

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Fixed column order. The header row is created automatically on first run.
COLUMNS = [
    "id",
    "date_added",
    "added_by",
    "status",
    "url",
    "source",
    "address",
    "suburb",
    "price",
    "bedrooms",
    "bathrooms",
    "parking",
    "property_type",
    "land_size",
    "inspection",
    "agent",
    "features",
    "our_rating",
    "viewed",
    "viewed_date",
    "notes",
    "image_url",
]

# Fields the conversational interface is allowed to change. Everything
# except the bookkeeping columns (id, date_added, added_by, url, source).
EDITABLE = {
    "status",
    "our_rating",
    "viewed",
    "viewed_date",
    "notes",
    "address",
    "suburb",
    "price",
    "bedrooms",
    "bathrooms",
    "parking",
    "property_type",
    "land_size",
    "inspection",
    "agent",
    "features",
    "image_url",
}


def _client() -> gspread.Client:
    if config.GOOGLE_CREDENTIALS_JSON:
        info = json.loads(config.GOOGLE_CREDENTIALS_JSON)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    else:
        creds = Credentials.from_service_account_file(
            config.GOOGLE_CREDENTIALS_FILE, scopes=SCOPES
        )
    return gspread.authorize(creds)


def _worksheet():
    sh = _client().open_by_key(config.SHEET_ID)
    try:
        ws = sh.worksheet(config.WORKSHEET_NAME)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(config.WORKSHEET_NAME, rows=1000, cols=len(COLUMNS))
    # Ensure header row exists / is correct.
    header = ws.row_values(1)
    if header != COLUMNS:
        ws.update([COLUMNS], "A1")
    return ws


def get_all() -> list[dict]:
    """Return every listing as a list of dicts keyed by column name."""
    return _worksheet().get_all_records()


def find_by_url(url: str) -> Optional[dict]:
    norm = url.split("?")[0].rstrip("/")
    for row in get_all():
        if str(row.get("url", "")).split("?")[0].rstrip("/") == norm:
            return row
    return None


def _next_id(rows: list[dict]) -> int:
    ids = [int(r["id"]) for r in rows if str(r.get("id", "")).isdigit()]
    return (max(ids) + 1) if ids else 1


def add(record: dict) -> dict:
    """Append a new listing. Returns the stored record (with assigned id)."""
    ws = _worksheet()
    rows = ws.get_all_records()
    record = {**record, "id": _next_id(rows)}
    ordered = [str(record.get(col, "")) for col in COLUMNS]
    ws.append_row(ordered, value_input_option="USER_ENTERED")
    return record


def append_note(listing_id: int, text: str) -> bool:
    """Append a line to a listing's notes, preserving what's already there."""
    for row in get_all():
        if str(row.get("id")) == str(listing_id):
            existing = str(row.get("notes", "")).strip()
            combined = f"{existing}\n• {text}".strip() if existing else f"• {text}"
            return update(listing_id, {"notes": combined})
    return False


def update(listing_id: int, fields: dict) -> bool:
    """Update editable fields of a listing by id. Returns True if found."""
    ws = _worksheet()
    rows = ws.get_all_records()
    for idx, row in enumerate(rows):
        if str(row.get("id")) == str(listing_id):
            sheet_row = idx + 2  # +1 header, +1 to 1-based
            for key, value in fields.items():
                if key not in EDITABLE or key not in COLUMNS:
                    continue
                col = COLUMNS.index(key) + 1
                ws.update_cell(sheet_row, col, str(value))
            return True
    return False
