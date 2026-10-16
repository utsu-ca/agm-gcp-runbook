import csv
import os
import secrets
from google.cloud import firestore

# Uses the credentials from your local gcloud CLI
db = firestore.Client()

# Set how many admin users you want to generate
NUM_ADMIN_USERS = 5 

def seed_db():
    print("Initializing Firestore Database...")
    
    # 1. Generate and Seed Users (PSKs)
    print(f"Generating {NUM_ADMIN_USERS} admin users...")
    batch = db.batch()
    credentials = []
    
    for i in range(1, NUM_ADMIN_USERS + 1):
        user_id = f"admin{i}"
        # Generate a cryptographically secure 64-character hex string (256-bit key)
        psk_hex = secrets.token_hex(32) 
        
        user_ref = db.collection('users').document(user_id)
        batch.set(user_ref, {'psk_hex': psk_hex})
        
        credentials.append({'user_id': user_id, 'psk_hex': psk_hex})
        
    batch.commit()
    print(f"✓ {NUM_ADMIN_USERS} users committed to Firestore.")
    
    # Save the generated keys locally so you can distribute them
    with open('admin_credentials.csv', mode='w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=['user_id', 'psk_hex'])
        writer.writeheader()
        writer.writerows(credentials)
        
    print("✓ Saved generated PSKs to 'admin_credentials.csv'. KEEP THIS FILE SAFE.")

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
                'name': row['name'],
                'preferred_name': row['preferred_name'],
                'expected_location': row['expected_location'],
                'checked_in': False
            })
            
            count += 1
            if count % 400 == 0:
                student_batch.commit()
                student_batch = db.batch()
    
    if count % 400 != 0:
        student_batch.commit()
    
    print(f"✓ Seeded {count} students from CSV.")

if __name__ == '__main__':
    seed_db()
