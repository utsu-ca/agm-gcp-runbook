"""Imports students from a CSV (default: students.csv). Seed data is trusted and not sanitized."""
import csv
import sys
import uuid

from google.cloud import firestore

db = firestore.Client()  # uses local gcloud Application Default Credentials

def last_name_of(row):
    """Uses the optional last_name column; otherwise derives it from 'Last, First' or 'First Last'."""
    if row.get("last_name"):
        return row["last_name"].strip()
    name = row["name"].strip()
    return name.split(",")[0].strip() if "," in name else name.split()[-1]

def seed_db(path):
    writer = db.bulk_writer()
    count = 0
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            writer.set(db.collection("students").document(uuid.uuid4().hex), {
                "student_number": row["student_number"].strip(),
                "card_id": row["student_number"].strip()[-5:-1],
                "name": row["name"],
                "last_name": last_name_of(row).lower(),
                "preferred_name": row["preferred_name"],
                "expected_location": row["expected_location"],
                "checked_in": False,
            })
            count += 1
    writer.close()
    print(f"Seeded {count} students from {path}.")

if __name__ == "__main__":
    seed_db(sys.argv[1] if len(sys.argv) > 1 else "students.csv")
