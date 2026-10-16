import csv
import os
import sys
from datetime import datetime, timezone
from google.cloud import firestore

# Uses the credentials from your local gcloud CLI
try:
    db = firestore.Client()
except Exception as e:
    print(f"Warning: Could not connect to Firestore Client: {e}")
    db = None

def seed_db():
    print("Initializing Firestore Database...")
    if db is None:
        print("❌ Cannot connect to Firestore. Verify Application Default Credentials.")
        return

    # 1. Seed Approved Google Workspace Users
    # Read approved emails from environment variables or command-line arguments
    approved_emails_env = os.environ.get("APPROVED_USER_EMAILS", "")
    admin_emails_env = os.environ.get("ADMIN_USER_EMAILS", "")

    approved_emails = [e.strip().lower() for e in approved_emails_env.split(",") if e.strip()]
    admin_emails = {e.strip().lower() for e in admin_emails_env.split(",") if e.strip()}

    if not approved_emails:
        print("ℹ No APPROVED_USER_EMAILS set. Prompting for initial admin user email...")
        if sys.stdin.isatty():
            prompt_email = input("Enter primary admin Google Workspace email: ").strip().lower()
            if prompt_email:
                approved_emails.append(prompt_email)
                admin_emails.add(prompt_email)
        else:
            print("Non-interactive mode: proceeding without interactive input.")

    if approved_emails:
        print(f"Seeding {len(approved_emails)} approved Google Workspace user(s)...")
        batch = db.batch()
        for email in approved_emails:
            role = "admin" if email in admin_emails else "staff"
            user_ref = db.collection('users').document(email)
            batch.set(user_ref, {
                'email': email,
                'role': role,
                'enabled': True,
                'updated_at': datetime.now(timezone.utc).isoformat()
            }, merge=True)
        batch.commit()
        print("✓ Approved users successfully committed to Firestore.")
    else:
        print("⚠ No approved users provided to seed.")

    # 2. Seed Students from CSV
    if not os.path.exists('students.csv'):
        print("⚠ students.csv not found, skipping student import.")
        return

    student_batch = db.batch()
    count = 0

    with open('students.csv', mode='r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            doc_ref = db.collection('students').document(row['student_number'])
            student_batch.set(doc_ref, {
                'name': row.get('name', ''),
                'preferred_name': row.get('preferred_name', ''),
                'expected_location': row.get('expected_location', ''),
                'checked_in': False
            }, merge=True)

            count += 1
            if count % 400 == 0:
                student_batch.commit()
                student_batch = db.batch()

    if count % 400 != 0:
        student_batch.commit()

    print(f"✓ Seeded {count} students from CSV.")

if __name__ == '__main__':
    seed_db()
