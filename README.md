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
4. Load students: `python seed.py students.csv` (columns `name,preferred_name,expected_location` plus `truncated_student_number` (4 digits) and/or `student_number` (truncated_student_number is derived from it if absent), optional `last_name`; seed data is trusted).

If a Spreadsheet ID is given, a Cloud Scheduler-triggered Cloud Run Job refreshes the "Full Export" tab (default every 5 minutes, 08:00-18:55 Mon-Fri; override with `EXPORT_SCHEDULE`/`EXPORT_TZ`) and the service appends each check-in (timestamp, student number, name, location, staff, mismatch) to "Live Log" (times in `TIMEZONE`, default `America/Toronto`).

## Cost control
- The service scales to zero (`--min-instances=0`, `--max-instances` default 3, override with `MAX_INSTANCES`); nothing runs between requests or export runs.
- Old container images are pruned (newest 3 kept) and load balancer logging is sampled at 10%.
- The load balancer, Cloud Armor and static IP are fixed costs that do not sleep. For off-season, run `./teardown.sh` (keeps Firestore data and the static IP; `DELETE_STATIC_IP=1` also releases the IP) and `./deploy.sh` to restore.

If the chosen check-in location (in-person/virtual) differs from the student's `expected_location`, the client asks staff to acknowledge the different location before checking in. Such check-ins are flagged `MISMATCH` and highlighted red in the "Full Export" (column I) and "Live Log" (column F) tabs.

Set `TEST_MODE=true` (env var) to let the card reader accept standard bank cards (`%B<cardnum>^LAST/FIRST^...`) for testing; the last-4-before-check-digit rule, i.e. digits -5..-1 of the card number, is used as the truncated student number. Leave it unset in production.
