import csv
import os
from google.cloud import firestore

# Uses the credentials from your local gcloud CLI
db = firestore.Client()

def seed_db():
    print("Seeding students...")
    # Seed Students from CSV
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
