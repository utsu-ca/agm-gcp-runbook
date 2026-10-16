import os
from datetime import datetime
from typing import Literal

import google.auth
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException
from fastapi.staticfiles import StaticFiles
from google.cloud import firestore
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from pydantic import BaseModel, Field

# --- Google Sheets Setup ---
# Pull the Spreadsheet ID from the Cloud Run container environment
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")

# Initialize Sheets API only if the ID is provided
if SPREADSHEET_ID:
    try:
        credentials, _ = google.auth.default(scopes=['https://www.googleapis.com/auth/spreadsheets'])
        sheets_service = build('sheets', 'v4', credentials=credentials)
    except Exception as e:
        print(f"Warning: Could not initialize Sheets API. {e}")
        sheets_service = None
else:
    print("Warning: SPREADSHEET_ID environment variable not set. Dashboard exports are disabled.")
    sheets_service = None

app = FastAPI(title="Student Check-in API", docs_url=None, redoc_url=None, openapi_url=None)

@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Strict-Transport-Security"] = "max-age=31536000"
    response.headers["Cache-Control"] = "no-store"
    return response

# Automatically uses the service account attached to your Cloud Run instance
db = firestore.Client()

# --- Auth (Google Workspace domain) ---
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
ALLOWED_DOMAIN = os.environ.get("ALLOWED_DOMAIN", "").lower()

if not GOOGLE_CLIENT_ID or not ALLOWED_DOMAIN:
    raise RuntimeError("GOOGLE_CLIENT_ID and ALLOWED_DOMAIN environment variables are required.")

class User(BaseModel):
    email: str

def current_user(authorization: str = Header(default="")) -> User:
    """Verifies a Google ID token and requires an employee of the allowed Workspace domain."""
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="Authentication required.")
    try:
        claims = id_token.verify_oauth2_token(token, google_requests.Request(), GOOGLE_CLIENT_ID)
    except Exception:
        raise HTTPException(status_code=401, detail="Authentication failed.")
    email = str(claims.get("email", "")).lower()
    # 'hd' is set by Google only for Workspace accounts; also check the email suffix.
    if (claims.get("email_verified") is not True
            or str(claims.get("hd", "")).lower() != ALLOWED_DOMAIN
            or not email.endswith("@" + ALLOWED_DOMAIN)):
        raise HTTPException(status_code=403, detail="Forbidden.")
    return User(email=email)

@app.get("/api/config")
def public_config():
    return {"client_id": GOOGLE_CLIENT_ID, "domain": ALLOWED_DOMAIN}

# --- Request Models ---
class StatusRequest(BaseModel):
    student_number: str = Field(pattern=r'^\d+$', max_length=20)

class CheckInRequest(BaseModel):
    student_number: str = Field(pattern=r'^\d+$', max_length=20)
    # Must not start with a formula character
    location: str = Field(max_length=20, pattern=r'^(?:[\w][\w\s\-\.,]*)?$')
    attendance_mode: Literal["in-person", "virtual"]

# --- Background Task Functions ---
def append_to_live_log(student_number: str, location: str, mode: str, staff_email: str):
    """Appends a single row to the 'Live Log' tab instantly."""
    if not sheets_service: return
    
    timestamp_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    values = [[timestamp_str, student_number, mode, location, staff_email]]
    body = {'values': values}
    
    try:
        sheets_service.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID,
            range="Live Log!A:E",
            valueInputOption="RAW",
            body=body
        ).execute()
    except HttpError as e:
        print(f"Failed to append to Google Sheet: {e}")

def run_full_export():
    """Overwrites the 'Full Export' tab with the latest Firestore data."""
    if not sheets_service: return
    
    # 1. Fetch all students from Firestore
    students = db.collection("students").stream()
    
    # 2. Format the data for Google Sheets
    # Start with headers
    values = [["Student Number", "Name", "Expected Location", "Actual Location", "Attendance Mode", "Checked In"]]
    
    for doc in students:
        data = doc.to_dict()
        values.append([
            doc.id,
            data.get("name", ""),
            data.get("expected_location", ""),
            data.get("current_location", ""),
            data.get("attendance_mode", ""),
            "Yes" if data.get("checked_in") else "No"
        ])
        
    body = {'values': values}
    
    try:
        # Clear the old data first
        sheets_service.spreadsheets().values().clear(
            spreadsheetId=SPREADSHEET_ID,
            range="Full Export!A:F"
        ).execute()
        
        # Write the new data
        sheets_service.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID,
            range="Full Export!A1",
            valueInputOption="RAW",
            body=body
        ).execute()
    except HttpError as e:
        print(f"Failed to export to Google Sheet: {e}")


# --- API Endpoints ---
@app.post("/api/is_checked_in")
def is_checked_in(payload: StatusRequest, user: User = Depends(current_user)):
    student_number = payload.student_number
    student_doc = db.collection("students").document(student_number).get()
    if not student_doc.exists:
        return {"error": "student not on list"}

    data = student_doc.to_dict()
    return {
        "student_number": student_number,
        "checked_in": data.get("checked_in", False),
        "name": data.get("name", ""),
        "preferred_name": data.get("preferred_name", ""),
        "expected_location": data.get("expected_location", "")
    }

@firestore.transactional
def atomic_check_in_transaction(transaction, student_ref, location, attendance_mode, student_number, staff_email):
    """Executes the check-in logic atomically."""
    snapshot = student_ref.get(transaction=transaction)
    
    if not snapshot.exists:
        return {"student_number": student_number, "result": "error: student not on list"}
        
    data = snapshot.to_dict()
    if data.get("checked_in") is True:
        return {"student_number": student_number, "result": "error: student already checked in"}
        
    transaction.update(student_ref, {
        "checked_in": True,
        "current_location": location,
        "attendance_mode": attendance_mode,
        "checked_in_by": staff_email
    })
    return {"student_number": student_number, "result": "success: checked student in"}



@app.post("/api/check_in")
def check_in(payload: CheckInRequest, background_tasks: BackgroundTasks, user: User = Depends(current_user)):
    transaction = db.transaction()
    student_ref = db.collection("students").document(payload.student_number)
    result = atomic_check_in_transaction(
        transaction,
        student_ref,
        payload.location,
        payload.attendance_mode,
        payload.student_number,
        user.email
    )

    if "success" in result["result"]:
        background_tasks.add_task(
            append_to_live_log,
            payload.student_number,
            payload.location,
            payload.attendance_mode,
            user.email
        )
    return result

@app.post("/api/trigger_export")
def trigger_export(background_tasks: BackgroundTasks, user: User = Depends(current_user)):
    """Export all students to the spreadsheet."""
    background_tasks.add_task(run_full_export)
    return {"status": "Full export started in the background."}

# --- Serve Static Web App ---
app.mount("/", StaticFiles(directory="static", html=True), name="static")
