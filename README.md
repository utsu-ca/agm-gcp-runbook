# Serverless Student Check-in

A secure, serverless application for atomic student check-ins. Built with Python (FastAPI), Google Cloud Firestore, and Vanilla JS.

## Features
- **Google Workspace Authentication & RBAC**: Authenticates users using Google Workspace credentials and enforces role-based access control against an approved email allowlist.
- **Anti-Replay Protection**: Strict timestamp freshness validation and nonce replay tracking.
- **Spreadsheet Formula Injection Defense**: Sanitizes all export and log data to prevent CSV/formula injection attacks.
- **Atomic Transactions**: Prevents duplicate check-in race conditions via Firestore transactions.
- **Serverless & Zero-Scale**: Runs on Google Cloud Run (scales to 0 when unused).
- **Edge Security & Cloud Armor**: Edge WAF rules (SQLi, XSS), rate limiting (120 req/min), and IP allowlisting before reaching the compute container.
- **Transport Security**: Modern TLS (1.2+), automatic HTTP-to-HTTPS redirection, and robust HTTP security response headers (CSP, HSTS, X-Frame-Options, etc.).

## Local Development
1. Install dependencies: `pip install -r requirements.txt`
2. Set up your Google Cloud Application Default Credentials locally: `gcloud auth application-default login`
3. (Optional) Set development environment variables:
   ```bash
   export DEV_MODE=true
   export APPROVED_USER_EMAILS="admin@mycompany.ca,staff@mycompany.ca"
   export ADMIN_USER_EMAILS="admin@mycompany.ca"
   ```
4. Start the server: `uvicorn main:app --reload`
5. Open `http://localhost:8000`

## Google Cloud Setup (Firestore)
Before deploying, initialize your database:
1. Go to the GCP Console -> **Firestore**.
2. Create a Native Mode database.
3. Add a `users` collection. Document ID: `user@mycompany.ca`, Fields: `email` (string), `role` (string: "admin" or "staff"), `enabled` (boolean: true).
4. Add a `students` collection. Document ID: `1001`, Fields: `checked_in` (boolean, false), `name` (string), `preferred_name` (string), `expected_location` (string).

## Production Deployment
The entire project can be deployed using the automated bash script. 

1. Edit `deploy.sh` and change the `ALLOWED_IP` variable to your IP address or CIDR range.
2. Run `chmod +x deploy.sh`
3. Run `./deploy.sh`
4. Enter your custom domain, approved Google Workspace emails, and spreadsheet ID when prompted.
5. Create an A-record in your DNS provider pointing to the provided Static IP address.