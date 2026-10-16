"""Imports students from a CSV (default: students.csv). Seed data is trusted and not sanitized."""
import csv
import sys

from google.cloud import firestore

db = firestore.Client()  # uses local gcloud Application Default Credentials

def seed_db(path):
    writer = db.bulk_writer()
    count = 0
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            writer.set(db.collection("students").document(row["student_number"]), {
                "name": row["name"],
                "preferred_name": row["preferred_name"],
                "expected_location": row["expected_location"],
                "checked_in": False,
            })
            count += 1
    writer.close()
    print(f"Seeded {count} students from {path}.")

if __name__ == "__main__":
    seed_db(sys.argv[1] if len(sys.argv) > 1 else "students.csv")
