import asyncio
import functools
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

import google.auth
from google.auth.exceptions import GoogleAuthError
from google.auth.transport import requests as google_requests
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from google.oauth2 import id_token
from googleapiclient.discovery import build
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

log = logging.getLogger("checkin")
logging.basicConfig(level=logging.INFO)

# --- Configuration ---
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
ALLOWED_DOMAIN = os.environ.get("ALLOWED_DOMAIN", "").lower()
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")
TIMEZONE = ZoneInfo(os.environ.get("TIMEZONE", "America/Toronto"))
EXPORT_INTERVAL_SEC = max(10, int(os.environ.get("EXPORT_INTERVAL_SEC", "60")))

if not GOOGLE_CLIENT_ID or not ALLOWED_DOMAIN:
    raise RuntimeError("GOOGLE_CLIENT_ID and ALLOWED_DOMAIN environment variables are required.")

# Uses the service account attached to the Cloud Run instance
db = firestore.Client()

@functools.lru_cache(maxsize=1)
def sheets():
    """Lazily builds the Sheets client; returns None when exports are disabled or unavailable."""
    if not SPREADSHEET_ID:
        log.warning("SPREADSHEET_ID not set. Sheet logging and exports are disabled.")
        return None
    try:
        credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/spreadsheets"])
        return build("sheets", "v4", credentials=credentials)
    except Exception:
        log.exception("Could not initialize Sheets API.")
        return None

def sanitize_cell(value) -> str:
    """Prevents spreadsheet formula injection by neutralising a leading formula character."""
    text = str(value or "")
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text

def run_full_export():
    """Overwrites the 'Full Export' tab with the latest Firestore data."""
    service = sheets()
    if not service:
        return
    values = [["ID", "Student Number", "Name", "Expected Location", "Actual Location", "Attendance Mode", "Checked In"]]
    values += [
        [doc.id, d.get("student_number", ""), d.get("name", ""), d.get("expected_location", ""), d.get("current_location", ""),
         d.get("attendance_mode", ""), "Yes" if d.get("checked_in") else "No"]
        for doc, d in ((doc, doc.to_dict()) for doc in db.collection("students").stream())
    ]
    values = [[sanitize_cell(c) for c in row] for row in values]
    cells = service.spreadsheets().values()
    cells.clear(spreadsheetId=SPREADSHEET_ID, range="Full Export!A:G").execute()
    cells.update(spreadsheetId=SPREADSHEET_ID, range="Full Export!A1",
                 valueInputOption="RAW", body={"values": values}).execute()

async def export_loop():
    """Refreshes the delayed dashboard on a fixed interval."""
    while True:
        try:
            await asyncio.to_thread(run_full_export)
        except Exception:
            log.exception("Full export failed.")
        await asyncio.sleep(EXPORT_INTERVAL_SEC)

@asynccontextmanager
async def lifespan(_app):
    task = asyncio.create_task(export_loop()) if sheets() else None
    yield
    if task:
        task.cancel()

app = FastAPI(title="Student Check-in API", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Strict-Transport-Security": "max-age=31536000",
    "Cache-Control": "no-store",
    "Cross-Origin-Opener-Policy": "same-origin-allow-popups",  # Google Sign-In may use a popup
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    "Content-Security-Policy": (
        "default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; "
        "script-src 'self' https://accounts.google.com/gsi/client; "
        "style-src 'self' https://accounts.google.com/gsi/style; "
        "connect-src 'self' https://accounts.google.com/gsi/; "
        "frame-src https://accounts.google.com/gsi/; img-src 'self' data:"
    ),
}

@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers.update(SECURITY_HEADERS)
    return response

# --- Auth (Google Workspace domain) ---
_auth_request = google_requests.Request()  # shared so Google's signing certs are cached

def current_user(authorization: str = Header(default="")) -> str:
    """Verifies a Google ID token and returns the email of an allowed-domain Workspace user."""
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="Authentication required.")
    try:
        claims = id_token.verify_oauth2_token(token, _auth_request, GOOGLE_CLIENT_ID)
    except (ValueError, GoogleAuthError) as e:
        log.info("Token verification failed: %s", type(e).__name__)
        raise HTTPException(status_code=401, detail="Authentication failed.")
    email = str(claims.get("email", "")).lower()
    # 'hd' is set by Google only for Workspace accounts; also check the email suffix.
    if (claims.get("email_verified") is not True
            or str(claims.get("hd", "")).lower() != ALLOWED_DOMAIN
            or not email.endswith("@" + ALLOWED_DOMAIN)):
        raise HTTPException(status_code=403, detail="Forbidden.")
    return email

@app.get("/api/config")
def public_config():
    return {"client_id": GOOGLE_CLIENT_ID, "domain": ALLOWED_DOMAIN}

# --- Request Models ---
StudentNumber = Annotated[str, Field(pattern=r"^\d+$", max_length=20)]
TruncatedStudentNumber = Annotated[str, Field(pattern=r"^\d{4}$")]
LastName = Annotated[str, Field(min_length=1, max_length=60)]
RegistrantId = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]

class LookupRequest(BaseModel):
    """Either field, or both, may be supplied."""
    student_number: StudentNumber | None = None
    last_name: LastName | None = None
    truncated_student_number: TruncatedStudentNumber | None = None

class CheckInRequest(BaseModel):
    id: RegistrantId
    attendance_mode: Literal["in-person", "virtual"]

# --- Live log ---
def append_to_live_log(student_number: str, location: str, mode: str, staff_email: str):
    """Appends a single row to the 'Live Log' tab."""
    service = sheets()
    if not service:
        return
    timestamp = datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
    row = [timestamp, student_number, mode, location, staff_email]
    try:
        service.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID, range="Live Log!A:E",
            valueInputOption="RAW", body={"values": [[sanitize_cell(c) for c in row]]},
        ).execute()
    except Exception:
        log.exception("Failed to append to Google Sheet.")

# --- API Endpoints ---
def _hit(doc):
    d = doc.to_dict()
    return {
        "id": doc.id,
        "student_number": d.get("student_number", ""),
        "name": d.get("name", ""),
        "preferred_name": d.get("preferred_name", ""),
        "expected_location": d.get("expected_location", ""),
        "checked_in": d.get("checked_in", False),
    }

def find_students(student_number, last_name, truncated_student_number=None):
    """Number match wins if unique; duplicates are narrowed by last name; otherwise all hits are returned."""
    students = db.collection("students")
    last = (last_name or "").strip().lower()
    if student_number or truncated_student_number:
        field, value = ("student_number", student_number) if student_number else ("truncated_student_number", truncated_student_number)
        hits = list(students.where(filter=FieldFilter(field, "==", value)).stream())
        if len(hits) > 1 and last:
            hits = [h for h in hits if h.to_dict().get("last_name") == last]
        return hits
    return list(students.where(filter=FieldFilter("last_name", "==", last)).stream())

@app.post("/api/is_checked_in")
def is_checked_in(payload: LookupRequest, user: str = Depends(current_user)):
    if not payload.student_number and not payload.truncated_student_number and not (payload.last_name or "").strip():
        raise HTTPException(status_code=422, detail="Provide a student number and/or last name.")
    hits = find_students(payload.student_number, payload.last_name, payload.truncated_student_number)
    if not hits:
        raise HTTPException(status_code=404, detail="Student not on list.")
    log.info("status lookup (%d hits) by %s", len(hits), user)
    return {"matches": [_hit(h) for h in hits]}

@firestore.transactional
def check_in_transaction(transaction, student_ref, fields):
    """Marks the student checked in atomically; raises if missing or already checked in."""
    snapshot = student_ref.get(transaction=transaction)
    if not snapshot.exists:
        raise HTTPException(status_code=404, detail="Student not on list.")
    if snapshot.to_dict().get("checked_in") is True:
        raise HTTPException(status_code=409, detail="Student already checked in.")
    transaction.update(student_ref, {"checked_in": True, **fields})
    return snapshot.to_dict()

@app.post("/api/check_in")
def check_in(payload: CheckInRequest, background_tasks: BackgroundTasks, user: str = Depends(current_user)):
    location = payload.attendance_mode
    data = check_in_transaction(
        db.transaction(),
        db.collection("students").document(payload.id),
        {"current_location": location, "attendance_mode": payload.attendance_mode, "checked_in_by": user},
    )
    background_tasks.add_task(append_to_live_log, data.get("student_number", ""), location,
                              payload.attendance_mode, user)
    return {"id": payload.id, "student_number": data.get("student_number", ""),
            "name": data.get("name", ""), "preferred_name": data.get("preferred_name", ""),
            "result": "success: checked student in"}

# --- Frontend (must be mounted last so /api routes take precedence) ---
app.mount("/", StaticFiles(directory=os.path.join(os.path.dirname(os.path.abspath(__file__)), "static"), html=True), name="static")
