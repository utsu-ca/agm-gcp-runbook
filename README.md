# Serverless Student Check-in

A secure, serverless application for atomic student check-ins. Built with Python (FastAPI), Google Cloud Firestore, and Vanilla JS.

## Features
- **End-to-End Payload Encryption**: AES-GCM encryption between the browser and the server.
- **Atomic Transactions**: Prevents duplicate check-in race conditions via Firestore transactions.
- **Serverless & Zero-Scale**: Runs on Google Cloud Run (scales to 0 when unused).
- **Edge Security**: Google Cloud Armor blocks non-whitelisted IPs before they reach the compute container.

## Local Development
1. Install dependencies: `pip install -r requirements.txt`
2. Set up your Google Cloud Application Default Credentials locally: `gcloud auth application-default login`
3. Start the server: `uvicorn main:app --reload`
4. Open `http://localhost:8000`

## Google Cloud Setup (Firestore)
Before deploying, initialize your database:
1. Go to the GCP Console -> **Firestore**.
2. Create a Native Mode database.
3. Add a `users` collection. Document ID: `admin1`, Field: `psk_hex` (string, 64-character hex).
4. Add a `students` collection. Document ID: `1001`, Fields: `checked_in` (boolean, false), `name` (string), `expected_location` (string).

## Production Deployment
The entire project can be deployed using the automated bash script. 

1. Edit `deploy.sh` and change the `ALLOWED_IP` variable to your IP address.
2. Run `chmod +x deploy.sh`
3. Run `./deploy.sh`
4. Enter your target domain when prompted.
5. Create an A-record in your DNS provider pointing to the provided Static IP address.