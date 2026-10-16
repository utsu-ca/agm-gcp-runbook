import json
import base64
import os
import time
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from typing import Literal
from pydantic import BaseModel, Field
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from google.cloud import firestore
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
from datetime import datetime
from fastapi import BackgroundTasks # NEW IMPORT
import google.auth
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import os 

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

app = FastAPI(title="Student Check-in API")

# Ensures request.client.host resolves to the real IP behind GCP Load Balancers
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")

# Automatically uses the service account attached to your Cloud Run instance
db = firestore.Client()

# --- Models & Crypto ---
class EncryptedRequest(BaseModel):
    user_id: str
    iv_b64: str
    ciphertext_b64: str

# --- Strict Validation Models for Decrypted Data ---
class StatusPayload(BaseModel):
    # Ensure student number is strictly numeric
    student_number: str = Field(pattern=r'^\d+$')
    timestamp: int

class CheckInPayload(BaseModel):
    student_number: str = Field(pattern=r'^\d+$')
    # Restrict location to alphanumeric, spaces, and basic punctuation, max 20 chars
    location: str = Field(max_length=20, pattern=r'^[\w\s\-\.,]*$') 
    attendance_mode: Literal["in-person", "virtual"]
    timestamp: int

def decrypt_payload(req: EncryptedRequest) -> dict:
    user_ref = db.collection("users").document(req.user_id)
    user_doc = user_ref.get()
    
    if not user_doc.exists:
        raise HTTPException(status_code=401, detail="Invalid user_id")
    
    psk_hex = user_doc.to_dict().get("psk_hex")
    key = bytes.fromhex(psk_hex)
    
    try:
        iv = base64.b64decode(req.iv_b64)
        ciphertext = base64.b64decode(req.ciphertext_b64)
        aesgcm = AESGCM(key)
        decrypted_bytes = aesgcm.decrypt(iv, ciphertext, None)
        return json.loads(decrypted_bytes.decode('utf-8'))
    except Exception:
        raise HTTPException(status_code=400, detail="Decryption failed. Invalid PSK or corrupted payload.")

def validate_timestamp(timestamp_ms: int):
    client_time = timestamp_ms / 1000.0
    if abs(time.time() - client_time) > 60:
        raise HTTPException(status_code=400, detail="Request expired.")

# --- Background Task Functions ---
def append_to_live_log(student_number: str, location: str, mode: str):
    """Appends a single row to the 'Live Log' tab instantly."""
    if not sheets_service: return
    
    timestamp_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    values = [[timestamp_str, student_number, mode, location]]
    body = {'values': values}
    
    try:
        sheets_service.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID,
            range="Live Log!A:D",
            valueInputOption="USER_ENTERED",
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
            valueInputOption="USER_ENTERED",
            body=body
        ).execute()
    except HttpError as e:
        print(f"Failed to export to Google Sheet: {e}")


# --- API Endpoints ---
@app.post("/api/is_checked_in")
def is_checked_in(req: EncryptedRequest):
    raw_payload = decrypt_payload(req)
    
    try:
        payload = StatusPayload(**raw_payload)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Validation failed: {e}")
        
    validate_timestamp(payload.timestamp)
    student_number = payload.student_number
    
    student_doc = db.collection("students").document(student_number).get()
    
    if not student_doc.exists:
        return {"error": "student not on list"}
        
    data = student_doc.to_dict()
    return {
        "student_number": student_number,
        "checked_in": data.get("checked_in", False),
        "preferred_name": data.get("preferred_name", ""),
        "name": data.get("name", ""),
        "expected_location": data.get("expected_location", "")
    }

@firestore.transactional
def atomic_check_in_transaction(transaction, student_ref, location, student_number):
    """Executes the check-in logic atomically."""
    snapshot = student_ref.get(transaction=transaction)
    
    if not snapshot.exists:
        return {"student_number": student_number, "result": "error: student not on list"}
        
    data = snapshot.to_dict()
    if data.get("checked_in") is True:
        return {"student_number": student_number, "result": "error: student already checked in"}
        
    transaction.update(student_ref, {
        "checked_in": True,
        "current_location": location
    })
    return {"student_number": student_number, "result": "success: checked student in"}



# --- Updated Endpoint ---
@app.post("/api/check_in")
def check_in(req: EncryptedRequest, background_tasks: BackgroundTasks): # Add background_tasks here
    raw_payload = decrypt_payload(req)
    # ... (Keep your validation code) ...
    
    transaction = db.transaction()
    student_ref = db.collection("students").document(payload.student_number)
    
    result = atomic_check_in_transaction(
        transaction, 
        student_ref, 
        payload.location, 
        payload.attendance_mode, 
        payload.student_number
    )
    
    # If the transaction was successful (not a duplicate), trigger the sheet update!
    if "success" in result["result"]:
        # This returns the response to the user instantly, then runs the function in the background
        background_tasks.add_task(
            append_to_live_log, 
            payload.student_number, 
            payload.location, 
            payload.attendance_mode
        )
        
    return result

# --- New Secure Export Endpoint ---
@app.post("/api/trigger_export")
def trigger_export(req: EncryptedRequest, background_tasks: BackgroundTasks):
    """
    Allows an admin to trigger a full export to the spreadsheet. 
    It requires the same PSK encryption as a check-in for security.
    """
    raw_payload = decrypt_payload(req)
    validate_timestamp(raw_payload.get("timestamp", 0))
    
    # Run the heavy export in the background so the request doesn't timeout
    background_tasks.add_task(run_full_export)
    
    return {"status": "Full export started in the background."}

# --- Serve Static Web App ---
os.makedirs("static", exist_ok=True)
app.mount("/", StaticFiles(directory="static", html=True), name="static")
