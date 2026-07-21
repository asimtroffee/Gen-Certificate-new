"""
Google Sheets & Drive integration for automated certificate processing.

Setup:
1. Go to https://console.cloud.google.com → create project → enable Sheets API + Drive API
2. Create a service account → download JSON key
3. Place the JSON key at backend/credentials.json
4. Share your Google Sheet (form responses) with the service account email (Editor)
5. Share the Google Drive upload folder with the service account email (Reader)
"""

import io
import os
import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CREDENTIALS_PATH = BASE_DIR / "credentials.json"

SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
SCOPES = [SHEETS_SCOPE, DRIVE_SCOPE]


def _get_credentials():
    if not CREDENTIALS_PATH.exists():
        raise FileNotFoundError(
            f"Credentials not found at {CREDENTIALS_PATH}. "
            "Create a Google Cloud service account and place the JSON key here."
        )
    from google.oauth2 import service_account
    return service_account.Credentials.from_service_account_file(
        str(CREDENTIALS_PATH), scopes=SCOPES
    )


def _get_sheets_service():
    from googleapiclient.discovery import build
    return build("sheets", "v4", credentials=_get_credentials())


def _get_drive_service():
    from googleapiclient.discovery import build
    return build("drive", "v3", credentials=_get_credentials())


def fetch_names_from_sheet(sheet_id: str, range_name: str = "Sheet1!A2:B") -> list[dict]:
    """Fetch a list of {name, email} from a Google Sheet."""
    creds = _get_credentials()
    from googleapiclient.discovery import build
    service = build("sheets", "v4", credentials=creds)
    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=sheet_id, range=range_name)
        .execute()
    )
    values = result.get("values", [])
    people = []
    for row in values:
        if not row or not row[0].strip():
            continue
        name = row[0].strip()
        email = row[1].strip() if len(row) > 1 else ""
        people.append({"name": name, "email": email})
    return people


def extract_file_id(cell_value: str) -> str | None:
    """Extract Google Drive file ID from a form response cell value.

    Handles:
      - =HYPERLINK("https://drive.google.com/open?id=FILE_ID", "name")
      - https://drive.google.com/open?id=FILE_ID
      - https://drive.google.com/file/d/FILE_ID/view
      - Plain FILE_ID string
    """
    if not cell_value:
        return None

    if "HYPERLINK" in cell_value:
        m = re.search(r'HYPERLINK\("([^"]+)"', cell_value)
        if m:
            cell_value = m.group(1)

    m = re.search(r'[?&]id=([^&\s"]+)', cell_value)
    if m:
        return m.group(1)

    m = re.search(r'/d/([^/&\s"]+)', cell_value)
    if m:
        return m.group(1)

    if re.match(r'^[a-zA-Z0-9_-]{20,}$', cell_value):
        return cell_value

    return None


def get_new_responses(
    sheet_id: str,
    sheet_name: str = "Form Responses 1",
    status_col: str = "E", # Defaulting to E for backward compatibility, but we can search for it too.
) -> list[dict]:
    """Read unprocessed form responses.
    """
    service = _get_sheets_service()
    
    # We fetch a wide range to ensure we capture Name, Email, and Status columns
    range_all = f"{sheet_name}!A:Z"

    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=sheet_id, range=range_all)
        .execute()
    )
    rows = result.get("values", [])
    if len(rows) < 2:
        return []

    headers = [h.strip().lower() for h in rows[0]]
    
    name_idx = -1
    email_idx = -1
    status_idx = ord(status_col.upper()) - ord("A") # default E
    
    for i, h in enumerate(headers):
        if "name" in h or "nama" in h:
            name_idx = i
        elif "email" in h or "mel" in h or "e-mail" in h:
            email_idx = i
        elif "status" in h:
            status_idx = i
            
    # Fallback if not found
    if name_idx == -1: name_idx = 1
    if email_idx == -1: email_idx = 2

    new_responses = []
    for i, row in enumerate(rows):
        if i == 0:
            continue
        row_num = i + 1
        status = row[status_idx].strip() if len(row) > status_idx else ""
        if status:
            continue

        teacher_name = row[name_idx].strip() if len(row) > name_idx else ""
        teacher_email = row[email_idx].strip() if len(row) > email_idx else ""

        if not teacher_name or not teacher_email:
            continue

        new_responses.append({
            "row_number": row_num,
            "teacher_name": teacher_name,
            "teacher_email": teacher_email,
        })

    return new_responses



def mark_processed(
    sheet_id: str,
    row_number: int,
    status_text: str,
    sheet_name: str = "Form Responses 1",
    status_col: str = "E",
):
    """Write status text to the Status column for a given row."""
    service = _get_sheets_service()
    range_cell = f"{sheet_name}!{status_col}{row_number}"
    body = {"values": [[status_text]]}
    service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=range_cell,
        valueInputOption="USER_ENTERED",
        body=body,
    ).execute()


def download_form_file(file_id: str) -> bytes | None:
    """Download a file from Google Drive by its file ID."""
    if not file_id:
        return None
    service = _get_drive_service()
    request = service.files().get_media(fileId=file_id)
    fh = io.BytesIO()
    from googleapiclient.http import MediaIoBaseDownload
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return fh.getvalue()


def get_file_name(file_id: str) -> str | None:
    """Get the filename from Google Drive by file ID."""
    if not file_id:
        return None
    service = _get_drive_service()
    f = service.files().get(fileId=file_id, fields="name").execute()
    return f.get("name")
