# Serverless Student Check-in

A secure, serverless application for atomic student check-ins. Built with Python (FastAPI), Google Cloud Firestore, and Vanilla JS.

## Features
- **Workspace Sign-In**: Google ID tokens verified server-side; only your organization's domain may use the API.
- **Atomic Transactions**: Prevents duplicate check-in race conditions via Firestore transactions.
- **Serverless & Zero-Scale**: Runs on Google Cloud Run (scales to 0 when unused).
- **Edge Security**: Google Cloud Armor blocks non-whitelisted IPs before they reach the compute container.

## Local Development
1. Install dependencies: `pip install -r requirements.txt`
2. Set up your Google Cloud Application Default Credentials locally: `gcloud auth application-default login`
3. Start the server: `uvicorn main:app --reload`
4. Open `http://localhost:8000`

## Local Firestore data
1. Use a Firestore Native Mode database (`deploy.sh` creates one).
2. Seed with `python seed.py`.
3. Authentication uses Google Sign-In restricted to your Workspace domain; all signed-in domain users have the same access. Set `GOOGLE_CLIENT_ID` and `ALLOWED_DOMAIN` as environment variables.

## Production Deployment
Designed to be run from Google Cloud Shell in a project with billing enabled:

1. `git clone` this repo and `cd` into it.
2. Run `./deploy.sh` and answer the prompts (site address, Workspace domain, office IP, OAuth client ID, optional Sheet ID). Any answer can be preset via env var (`DOMAIN_NAME`, `ALLOWED_DOMAIN`, `GOOGLE_CLIENT_ID`, `ALLOWED_IP`, `SPREADSHEET_ID`). The script is safe to re-run.
3. Create the A record the script prints at your DNS provider.
4. Load students: `python seed.py students.csv` (columns `name,preferred_name,expected_location` plus `card_id` (4 digits) and/or `student_number` (card_id is derived from it if absent), optional `last_name`; seed data is trusted).

If a Spreadsheet ID is given, the service refreshes the "Full Export" tab every minute (`EXPORT_INTERVAL_SEC`) and appends each check-in to "Live Log" (times in `TIMEZONE`, default `America/Toronto`).
