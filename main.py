import json
import base64
import os
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from google.cloud import firestore
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

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

# --- API Endpoints ---
@app.post("/api/is_checked_in")
def is_checked_in(req: EncryptedRequest):
    payload = decrypt_payload(req)
    student_number = payload.get("student_number")
    
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

@app.post("/api/check_in")
def check_in(req: EncryptedRequest):
    payload = decrypt_payload(req)
    student_number = payload.get("student_number")
    location = payload.get("location")
    
    transaction = db.transaction()
    student_ref = db.collection("students").document(student_number)
    
    result = atomic_check_in_transaction(transaction, student_ref, location, student_number)
    return result

# --- Serve Static Web App ---
os.makedirs("static", exist_ok=True)
app.mount("/", StaticFiles(directory="static", html=True), name="static")