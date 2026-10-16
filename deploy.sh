#!/bin/bash
set -e  # Exit immediately if a command fails

# Trapper to capture exact script line and error message on failure
trap 'echo "❌ ERROR: Deployment script failed at line $LINENO while running command: $BASH_COMMAND"' ERR

# --- CONFIGURATION ---
PROJECT_ID=$(gcloud config get-value project)
REGION="us-central1"
SERVICE_NAME="student-checkin-api"
ALLOWED_IP="203.0.113.50/32"  # Change to your actual IP or CIDR block

echo "Deploying to project: $PROJECT_ID in $REGION"
read -p "Enter your custom domain or subdomain (e.g., checkin.example.com): " DOMAIN_NAME

# NEW PROMPT: Ask for the Spreadsheet ID
echo ""
echo "Google Sheets Integration (Optional)"
echo "If you want a live dashboard, create a Google Sheet and share it with:"
echo "checkin-api-sa@${PROJECT_ID}.iam.gserviceaccount.com"
read -p "Enter your Google Spreadsheet ID (leave blank to skip): " SPREADSHEET_ID

# 0. Initialize Database
echo "--> Initializing and seeding Firestore database..."
pip install -r requirements.txt
python seed.py

# 1. Enable GCP Services
echo "--> Enabling GCP APIs..."
gcloud services enable run.googleapis.com compute.googleapis.com firestore.googleapis.com iam.googleapis.com sheets.googleapis.com

# 2. Setup Least Privilege Service Account
echo "--> Setting up service account..."
SA_NAME="checkin-api-sa"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

if ! gcloud iam service-accounts describe $SA_EMAIL >/dev/null 2>&1; then
    echo "Creating service account $SA_NAME..."
    gcloud iam service-accounts create $SA_NAME --display-name="Student Checkin API SA"
else
    echo "Service account $SA_NAME already exists."
fi

echo "Binding Firestore Data User permissions to service account..."
gcloud projects add-iam-policy-binding $PROJECT_ID \
    --member="serviceAccount:$SA_EMAIL" \
    --role="roles/datastore.user" \
    --condition=None \
    --quiet

# 3. Deploy Cloud Run
echo "--> Deploying Cloud Run service container..."

# Build the deployment command conditionally based on whether a Spreadsheet ID was provided
DEPLOY_CMD="gcloud run deploy $SERVICE_NAME \
    --source . \
    --region=$REGION \
    --ingress=internal-and-cloud-load-balancing \
    --allow-unauthenticated \
    --service-account=$SA_EMAIL \
    --quiet"

# Inject the environment variable if the user provided one
if [ -n "$SPREADSHEET_ID" ]; then
    DEPLOY_CMD="$DEPLOY_CMD --set-env-vars=SPREADSHEET_ID=$SPREADSHEET_ID"
fi

# Execute the deployment command
eval $DEPLOY_CMD

# 4. Create & Configure Cloud Armor Security Policy
echo "Configuring Cloud Armor policy & WAF..."
if ! gcloud compute security-policies describe checkin-ip-allowlist --global >/dev/null 2>&1; then
    gcloud compute security-policies create checkin-ip-allowlist \
        --description "WAF and IP Whitelist" --global
fi

# Set default rule to DENY ALL
gcloud compute security-policies rules update 2147483647 \
    --security-policy checkin-ip-allowlist \
    --action "deny-403"

# WAF Priority 900: Block SQL Injection
if ! gcloud compute security-policies rules describe 900 --security-policy checkin-ip-allowlist >/dev/null 2>&1; then
    gcloud compute security-policies rules create 900 \
        --security-policy checkin-ip-allowlist \
        --description "Block SQL Injection" \
        --expression "evaluatePreconfiguredWaf('sqli-v422-stable', {'sensitivity': 1})" \
        --action "deny-403"
fi

# WAF Priority 901: Block Cross-Site Scripting
if ! gcloud compute security-policies rules describe 901 --security-policy checkin-ip-allowlist >/dev/null 2>&1; then
    gcloud compute security-policies rules create 901 \
        --security-policy checkin-ip-allowlist \
        --description "Block XSS" \
        --expression "evaluatePreconfiguredWaf('xss-v422-stable', {'sensitivity': 1})" \
        --action "deny-403"
fi

# IP Allowlist Priority 1000: Allow your network
if gcloud compute security-policies rules describe 1000 --security-policy checkin-ip-allowlist >/dev/null 2>&1; then
    gcloud compute security-policies rules update 1000 \
        --security-policy checkin-ip-allowlist \
        --src-ip-ranges "$ALLOWED_IP" \
        --action "allow"
else
    gcloud compute security-policies rules create 1000 \
        --security-policy checkin-ip-allowlist \
        --description "Allow admin network" \
        --src-ip-ranges "$ALLOWED_IP" \
        --action "allow"
fi

# 4. Reserve a Global Static IP Address
echo "Reserving global static IP..."
gcloud compute addresses create ${SERVICE_NAME}-ip \
    --network-tier=PREMIUM \
    --global || true
    
IP_ADDRESS=$(gcloud compute addresses describe ${SERVICE_NAME}-ip --global --format="value(address)")

# 5. Create Google-Managed SSL Certificate
echo "Requesting SSL Certificate for $DOMAIN_NAME..."
gcloud compute ssl-certificates create ${SERVICE_NAME}-cert \
    --domains=$DOMAIN_NAME \
    --global || true

# 6. Create Serverless NEG
echo "Creating Serverless NEG..."
gcloud compute network-endpoint-groups create ${SERVICE_NAME}-neg \
    --region=$REGION \
    --network-endpoint-type=serverless \
    --cloud-run-service=$SERVICE_NAME || true

# 7. Create Backend Service and attach Cloud Armor
echo "Creating Backend Service..."
gcloud compute backend-services create ${SERVICE_NAME}-backend \
    --global \
    --security-policy=checkin-ip-allowlist \
    --load-balancing-scheme=EXTERNAL_MANAGED || true

gcloud compute backend-services add-backend ${SERVICE_NAME}-backend \
    --global \
    --network-endpoint-group=${SERVICE_NAME}-neg \
    --network-endpoint-group-region=$REGION || true

# 8. Create URL Map
echo "Creating URL Map..."
gcloud compute url-maps create ${SERVICE_NAME}-url-map \
    --default-service ${SERVICE_NAME}-backend || true

# 9. Create Target HTTPS Proxy
echo "Creating HTTPS Proxy..."
gcloud compute target-https-proxies create ${SERVICE_NAME}-https-proxy \
    --url-map=${SERVICE_NAME}-url-map \
    --ssl-certificates=${SERVICE_NAME}-cert || true

# 10. Create Forwarding Rule (Frontend IP on port 443)
echo "Creating HTTPS Forwarding Rule..."
gcloud compute forwarding-rules create ${SERVICE_NAME}-https-rule \
    --global \
    --target-https-proxy=${SERVICE_NAME}-https-proxy \
    --ports=443 \
    --address=${SERVICE_NAME}-ip || true

echo "======================================================"
echo "INFRASTRUCTURE PROVISIONED!"
echo "Static IP Address: $IP_ADDRESS"
echo "Requested Domain: $DOMAIN_NAME"
echo "======================================================"
echo "CRITICAL NEXT STEP:"
echo "1. Go to your DNS provider."
echo "2. Create an 'A Record' pointing $DOMAIN_NAME to $IP_ADDRESS"
echo "Google will provision the SSL certificate automatically once the DNS propagates."
echo "======================================================"
