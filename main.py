import os
import time
import json
import threading
import secrets
from datetime import datetime, timezone
from typing import Literal, Optional, Set, Dict

from fastapi import FastAPI, HTTPException, Request, Depends, BackgroundTasks, Security, status
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

import google.auth
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.cloud import firestore

# --- Environment & Configuration ---
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")
OAUTH_CLIENT_ID = os.environ.get("OAUTH_CLIENT_ID")
APPROVED_USER_EMAILS_ENV = os.environ.get("APPROVED_USER_EMAILS", "")
ADMIN_USER_EMAILS_ENV = os.environ.get("ADMIN_USER_EMAILS", "")
TRUSTED_PROXIES_ENV = os.environ.get("TRUSTED_PROXIES", "127.0.0.1,130.211.0.0/22,35.191.0.0/16")
DEV_MODE = os.environ.get("DEV_MODE", "false").lower() in ("true", "1", "yes")

# Initialize Firestore Client
try:
    db = firestore.Client()
except Exception as e:
    print(f"Warning: Could not initialize Firestore Client: {e}")
    db = None

# Initialize Sheets API only if SPREADSHEET_ID is provided
if SPREADSHEET_ID:
    try:
        credentials, _ = google.auth.default(scopes=['https://www.googleapis.com/auth/spreadsheets'])
        sheets_service = build('sheets', 'v4', credentials=credentials)
    except Exception as e:
        print(f"Warning: Could not initialize Sheets API: {e}")
        sheets_service = None
else:
    sheets_service = None

# --- Application Setup ---
app = FastAPI(title="Student Check-in API")

# Harden Proxy Headers: restrict to known reverse proxies / GCP load balancer ranges
trusted_hosts = [h.strip() for h in TRUSTED_PROXIES_ENV.split(",") if h.strip()]
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=trusted_hosts)

# Security Response Headers Middleware
@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://accounts.google.com; "
        "style-src 'self' 'unsafe-inline'; "
        "frame-src https://accounts.google.com; "
        "connect-src 'self' https://accounts.google.com; "
        "img-src 'self' data: https:;"
    )
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    return response

# --- User & Authentication Models ---
class AuthenticatedUser(BaseModel):
    email: str
    role: str = "staff"

security_bearer = HTTPBearer(auto_error=False)

def get_env_approved_emails() -> Set[str]:
    return {e.strip().lower() for e in APPROVED_USER_EMAILS_ENV.split(",") if e.strip()}

def get_env_admin_emails() -> Set[str]:
    return {e.strip().lower() for e in ADMIN_USER_EMAILS_ENV.split(",") if e.strip()}

def check_user_authorization(email: str) -> Optional[AuthenticatedUser]:
    """
    Checks whether an email is authorized against the approved user list (environment or Firestore).
    Returns AuthenticatedUser if approved, None otherwise.
    """
    email_clean = email.strip().lower()
    env_approved = get_env_approved_emails()
    env_admins = get_env_admin_emails()

    # 1. Check environment variables
    if email_clean in env_admins:
        return AuthenticatedUser(email=email_clean, role="admin")
    if email_clean in env_approved:
        return AuthenticatedUser(email=email_clean, role="staff")

    # 2. Check Firestore users collection
    if db is not None:
        try:
            user_doc = db.collection("users").document(email_clean).get()
            if user_doc.exists:
                data = user_doc.to_dict() or {}
                if data.get("enabled", True):
                    role = data.get("role", "staff")
                    return AuthenticatedUser(email=email_clean, role=role)
        except Exception as e:
            print(f"Error checking Firestore user authorization: {e}")

    return None

async def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Security(security_bearer)
) -> AuthenticatedUser:
    """
    Authenticates requests via Google Workspace credentials:
    1. Google ID token in Authorization: ******
    2. Google Cloud IAP header: X-Goog-Authenticated-User-Email
    3. DEV_MODE fallback for local testing
    """
    email: Optional[str] = None

    # Option A: Check Google Cloud IAP Header
    iap_user_header = request.headers.get("x-goog-authenticated-user-email")
    if iap_user_header:
        # Header value format is typically 'accounts.google.com:user@domain.ca'
        email = iap_user_header.split(":")[-1].strip().lower()

    # Option B: Check ****** (Google ID Token)
    elif credentials and credentials.credentials:
        token = credentials.credentials
        try:
            # Verify Google OAuth2 / OpenID Connect ID token
            transport_request = google_requests.Request()
            id_info = id_token.verify_oauth2_token(
                token,
                transport_request,
                audience=OAUTH_CLIENT_ID if OAUTH_CLIENT_ID else None
            )
            token_email = id_info.get("email")
            if token_email and id_info.get("email_verified", True):
                email = token_email.strip().lower()
        except Exception as e:
            if DEV_MODE:
                # In development mode, allow bearer token to represent email directly
                if "@" in token:
                    email = token.strip().lower()
            if not email:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid or expired authentication credentials."
                )

    # Option C: Development mode fallback
    elif DEV_MODE:
        dev_header = request.headers.get("x-dev-user-email")
        if dev_header and "@" in dev_header:
            email = dev_header.strip().lower()

    if not email:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Please sign in with your Google Workspace account."
        )

    # Authorize against approved user list
    auth_user = check_user_authorization(email)
    if not auth_user:
        # Generic error message to prevent user enumeration
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied. User account is not authorized."
        )

    return auth_user

def require_admin(user: AuthenticatedUser = Depends(get_current_user)) -> AuthenticatedUser:
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied. Administrator privileges required."
        )
    return user

# --- Anti-Replay Protection ---
_seen_nonces: Dict[str, float] = {}
_nonce_lock = threading.Lock()

def validate_replay_protection(timestamp_ms: int, nonce: str):
    """
    Validates request timestamp freshness and checks for replayed nonces.
    """
    client_time = timestamp_ms / 1000.0
    now = time.time()

    # Allow at most 60 seconds clock skew / request expiry
    if abs(now - client_time) > 60:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Request expired or invalid timestamp."
        )

    with _nonce_lock:
        # Prune expired nonces from memory
        expired_keys = [k for k, exp in _seen_nonces.items() if exp < now]
        for k in expired_keys:
            del _seen_nonces[k]

        if nonce in _seen_nonces:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Duplicate request detected (replay protection)."
            )

        # Store nonce with expiry (client window + margin)
        _seen_nonces[nonce] = now + 70.0

# --- Request Validation Models ---
class StatusRequest(BaseModel):
    student_number: str = Field(..., pattern=r'^\d{1,20}$')
    timestamp: int
    nonce: str = Field(..., min_length=8, max_length=64)

class CheckInRequest(BaseModel):
    student_number: str = Field(..., pattern=r'^\d{1,20}$')
    location: str = Field(default="", max_length=50, pattern=r'^[\w\s\-\.,]*$')
    attendance_mode: Literal["in-person", "virtual"]
    timestamp: int
    nonce: str = Field(..., min_length=8, max_length=64)

class ExportRequest(BaseModel):
    timestamp: int
    nonce: str = Field(..., min_length=8, max_length=64)

# --- Spreadsheet Formula Injection Protection ---
def sanitize_sheet_cell(value: Optional[str]) -> str:
    """
    Escapes characters that could trigger formula execution in spreadsheet software.
    Characters like =, +, -, @, Tab, Carriage Return are prepended with an apostrophe.
    """
    if value is None:
        return ""
    val_str = str(value)
    stripped = val_str.lstrip()
    if stripped.startswith(('=', '+', '-', '@', '\t', '\r')):
        return f"'{val_str}"
    return val_str

# --- Background Task Functions ---
def append_to_live_log(student_number: str, location: str, mode: str):
    """Appends a single sanitized row to the 'Live Log' tab using RAW value input."""
    if not sheets_service or not SPREADSHEET_ID:
        return

    timestamp_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    values = [[
        timestamp_str,
        sanitize_sheet_cell(student_number),
        sanitize_sheet_cell(mode),
        sanitize_sheet_cell(location)
    ]]
    body = {'values': values}

    try:
        sheets_service.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID,
            range="Live Log!A:D",
            valueInputOption="RAW",  # Use RAW to prevent formula interpretation
            body=body
        ).execute()
    except HttpError as e:
        print(f"Failed to append to Google Sheet: {e}")

# Export Debounce and Lock
export_lock = threading.Lock()
last_export_timestamp: float = 0.0
EXPORT_COOLDOWN_SECONDS: float = 60.0

def run_full_export():
    """Overwrites the 'Full Export' tab with the latest sanitized Firestore data."""
    if not sheets_service or not SPREADSHEET_ID or db is None:
        return

    try:
        students = db.collection("students").stream()
        values = [["Student Number", "Name", "Expected Location", "Actual Location", "Attendance Mode", "Checked In"]]

        for doc in students:
            data = doc.to_dict() or {}
            values.append([
                sanitize_sheet_cell(doc.id),
                sanitize_sheet_cell(data.get("name", "")),
                sanitize_sheet_cell(data.get("expected_location", "")),
                sanitize_sheet_cell(data.get("current_location", "")),
                sanitize_sheet_cell(data.get("attendance_mode", "")),
                "Yes" if data.get("checked_in") else "No"
            ])

        body = {'values': values}

        # Clear old data
        sheets_service.spreadsheets().values().clear(
            spreadsheetId=SPREADSHEET_ID,
            range="Full Export!A:F"
        ).execute()

        # Write new sanitized data using RAW input option
        sheets_service.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID,
            range="Full Export!A1",
            valueInputOption="RAW",
            body=body
        ).execute()
    except HttpError as e:
        print(f"Failed to export to Google Sheet: {e}")
    except Exception as e:
        print(f"Unexpected error during full export: {e}")

# --- Firestore Transaction Helper ---
@firestore.transactional
def atomic_check_in_transaction(
    transaction,
    student_ref,
    location: str,
    attendance_mode: str,
    student_number: str,
    checked_in_by: str
) -> dict:
    """Executes atomic check-in transaction preventing duplicate check-in race conditions."""
    snapshot = student_ref.get(transaction=transaction)

    if not snapshot.exists:
        return {"student_number": student_number, "result": "error: student not on list"}

    data = snapshot.to_dict() or {}
    if data.get("checked_in") is True:
        return {"student_number": student_number, "result": "error: student already checked in"}

    transaction.update(student_ref, {
        "checked_in": True,
        "current_location": location,
        "attendance_mode": attendance_mode,
        "checked_in_by": checked_in_by,
        "checked_in_at": datetime.now(timezone.utc).isoformat()
    })
    return {"student_number": student_number, "result": "success: checked student in"}

# --- API Endpoints ---
@app.get("/api/auth/me")
def get_auth_me(user: AuthenticatedUser = Depends(get_current_user)):
    """Returns the authenticated user's email and role."""
    return {"email": user.email, "role": user.role}

@app.post("/api/is_checked_in")
def is_checked_in(
    payload: StatusRequest,
    user: AuthenticatedUser = Depends(get_current_user)
):
    validate_replay_protection(payload.timestamp, payload.nonce)
    student_number = payload.student_number

    if db is None:
        raise HTTPException(status_code=500, detail="Database service unavailable.")

    student_doc = db.collection("students").document(student_number).get()

    if not student_doc.exists:
        return {"error": "student not on list"}

    data = student_doc.to_dict() or {}
    # Minimize PII exposure: return only essential check-in fields
    return {
        "student_number": student_number,
        "checked_in": data.get("checked_in", False),
        "preferred_name": data.get("preferred_name", ""),
        "expected_location": data.get("expected_location", "")
    }

@app.post("/api/check_in")
def check_in(
    payload: CheckInRequest,
    background_tasks: BackgroundTasks,
    user: AuthenticatedUser = Depends(get_current_user)
):
    validate_replay_protection(payload.timestamp, payload.nonce)

    if db is None:
        raise HTTPException(status_code=500, detail="Database service unavailable.")

    student_ref = db.collection("students").document(payload.student_number)
    transaction = db.transaction()

    result = atomic_check_in_transaction(
        transaction,
        student_ref,
        payload.location,
        payload.attendance_mode,
        payload.student_number,
        user.email
    )

    if "success" in result.get("result", ""):
        background_tasks.add_task(
            append_to_live_log,
            payload.student_number,
            payload.location,
            payload.attendance_mode
        )

    return result

@app.post("/api/trigger_export")
def trigger_export(
    payload: ExportRequest,
    background_tasks: BackgroundTasks,
    user: AuthenticatedUser = Depends(require_admin)
):
    """
    Triggers a full export to the spreadsheet.
    Requires admin privileges and enforces debounce rate limiting.
    """
    global last_export_timestamp
    validate_replay_protection(payload.timestamp, payload.nonce)

    now = time.time()
    with export_lock:
        if now - last_export_timestamp < EXPORT_COOLDOWN_SECONDS:
            cooldown_remaining = int(EXPORT_COOLDOWN_SECONDS - (now - last_export_timestamp))
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Export rate limited. Please wait {cooldown_remaining} seconds before re-triggering."
            )
        last_export_timestamp = now

    background_tasks.add_task(run_full_export)
    return {"status": "Full export started in the background."}

# --- Serve Static Web App ---
os.makedirs("static", exist_ok=True)
app.mount("/", StaticFiles(directory="static", html=True), name="static")
