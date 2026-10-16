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

# Google Workspace Authentication Setup
echo ""
echo "Google Workspace Authentication Setup"
echo "Provide approved user emails (e.g. staff@mycompany.ca) with GWorkspace credentials."
read -p "Enter approved user emails (comma-separated): " APPROVED_USER_EMAILS
read -p "Enter admin user emails (comma-separated, subset of approved): " ADMIN_USER_EMAILS
read -p "Enter OAuth Client ID (optional, press Enter to skip if using IAP): " OAUTH_CLIENT_ID

# Google Sheets Integration Setup
echo ""
echo "Google Sheets Integration (Optional)"
echo "If you want a live dashboard, create a Google Sheet and share it with:"
echo "checkin-api-sa@${PROJECT_ID}.iam.gserviceaccount.com"
read -p "Enter your Google Spreadsheet ID (leave blank to skip): " SPREADSHEET_ID

# 0. Initialize Database
echo "--> Initializing and seeding Firestore database..."
pip install -r requirements.txt
APPROVED_USER_EMAILS="$APPROVED_USER_EMAILS" ADMIN_USER_EMAILS="$ADMIN_USER_EMAILS" python seed.py

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

# Least privilege: restrict runtime service account to datastore.user
echo "Binding Firestore Data User permissions to service account..."
gcloud projects add-iam-policy-binding $PROJECT_ID \
    --member="serviceAccount:$SA_EMAIL" \
    --role="roles/datastore.user" \
    --condition=None \
    --quiet

# 3. Deploy Cloud Run
echo "--> Deploying Cloud Run service container..."

# Build environment variables list
ENV_VARS=()
[ -n "$SPREADSHEET_ID" ] && ENV_VARS+=("SPREADSHEET_ID=$SPREADSHEET_ID")
[ -n "$APPROVED_USER_EMAILS" ] && ENV_VARS+=("APPROVED_USER_EMAILS=$APPROVED_USER_EMAILS")
[ -n "$ADMIN_USER_EMAILS" ] && ENV_VARS+=("ADMIN_USER_EMAILS=$ADMIN_USER_EMAILS")
[ -n "$OAUTH_CLIENT_ID" ] && ENV_VARS+=("OAUTH_CLIENT_ID=$OAUTH_CLIENT_ID")

DEPLOY_CMD="gcloud run deploy $SERVICE_NAME \
    --source . \
    --region=$REGION \
    --ingress=internal-and-cloud-load-balancing \
    --allow-unauthenticated \
    --service-account=$SA_EMAIL \
    --quiet"

if [ ${#ENV_VARS[@]} -gt 0 ]; then
    ENV_STRING=$(IFS=,; echo "${ENV_VARS[*]}")
    DEPLOY_CMD="$DEPLOY_CMD --set-env-vars=$ENV_STRING"
fi

eval $DEPLOY_CMD

# 4. Create & Configure Cloud Armor Security Policy with Rate Limiting
echo "Configuring Cloud Armor policy & WAF..."
if ! gcloud compute security-policies describe checkin-ip-allowlist --global >/dev/null 2>&1; then
    gcloud compute security-policies create checkin-ip-allowlist \
        --description "WAF, Rate Limiting, and IP Allowlist" --global
fi

# Set default rule to DENY ALL
gcloud compute security-policies rules update 2147483647 \
    --security-policy checkin-ip-allowlist \
    --action "deny-403"

# WAF Priority 800: Rate Limiting (Throttle burst/brute force)
if ! gcloud compute security-policies rules describe 800 --security-policy checkin-ip-allowlist >/dev/null 2>&1; then
    gcloud compute security-policies rules create 800 \
        --security-policy checkin-ip-allowlist \
        --description "Rate Limiting Protection (120 req/min per IP)" \
        --action "throttle" \
        --rate-limit-threshold-count=120 \
        --rate-limit-threshold-interval-sec=60 \
        --conform-action "allow" \
        --exceed-action "deny-429" \
        --enforce-on-key "IP" || true
fi

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

# 5. Reserve a Global Static IP Address
echo "Reserving global static IP..."
gcloud compute addresses create ${SERVICE_NAME}-ip \
    --network-tier=PREMIUM \
    --global || true
    
IP_ADDRESS=$(gcloud compute addresses describe ${SERVICE_NAME}-ip --global --format="value(address)")

# 6. Create Google-Managed SSL Certificate & SSL Policy (TLS 1.2+ MODERN)
echo "Requesting SSL Certificate for $DOMAIN_NAME..."
gcloud compute ssl-certificates create ${SERVICE_NAME}-cert \
    --domains=$DOMAIN_NAME \
    --global || true

echo "Configuring modern SSL Policy (TLS 1.2+)..."
if ! gcloud compute ssl-policies describe ${SERVICE_NAME}-ssl-policy --global >/dev/null 2>&1; then
    gcloud compute ssl-policies create ${SERVICE_NAME}-ssl-policy \
        --profile MODERN \
        --min-tls-version 1.2 \
        --global || true
fi

# 7. Create Serverless NEG
echo "Creating Serverless NEG..."
gcloud compute network-endpoint-groups create ${SERVICE_NAME}-neg \
    --region=$REGION \
    --network-endpoint-type=serverless \
    --cloud-run-service=$SERVICE_NAME || true

# 8. Create Backend Service and attach Cloud Armor
echo "Creating Backend Service..."
gcloud compute backend-services create ${SERVICE_NAME}-backend \
    --global \
    --security-policy=checkin-ip-allowlist \
    --load-balancing-scheme=EXTERNAL_MANAGED || true

gcloud compute backend-services add-backend ${SERVICE_NAME}-backend \
    --global \
    --network-endpoint-group=${SERVICE_NAME}-neg \
    --network-endpoint-group-region=$REGION || true

# 9. Create URL Map
echo "Creating URL Map..."
gcloud compute url-maps create ${SERVICE_NAME}-url-map \
    --default-service ${SERVICE_NAME}-backend || true

# 10. Create Target HTTPS Proxy with Modern SSL Policy
echo "Creating HTTPS Proxy..."
gcloud compute target-https-proxies create ${SERVICE_NAME}-https-proxy \
    --url-map=${SERVICE_NAME}-url-map \
    --ssl-certificates=${SERVICE_NAME}-cert \
    --ssl-policy=${SERVICE_NAME}-ssl-policy || true

# 11. Create HTTPS Forwarding Rule (Frontend IP on port 443)
echo "Creating HTTPS Forwarding Rule..."
gcloud compute forwarding-rules create ${SERVICE_NAME}-https-rule \
    --global \
    --target-https-proxy=${SERVICE_NAME}-https-proxy \
    --ports=443 \
    --address=${SERVICE_NAME}-ip || true

# 12. Create HTTP-to-HTTPS Redirection on port 80
echo "Creating HTTP to HTTPS Redirect..."
if ! gcloud compute url-maps describe ${SERVICE_NAME}-http-redirect --global >/dev/null 2>&1; then
    gcloud compute url-maps import ${SERVICE_NAME}-http-redirect --global --quiet <<EOF || true
name: ${SERVICE_NAME}-http-redirect
defaultUrlRedirect:
  httpsRedirect: true
  stripQuery: false
EOF
fi

gcloud compute target-http-proxies create ${SERVICE_NAME}-http-proxy \
    --url-map=${SERVICE_NAME}-http-redirect \
    --global || true

gcloud compute forwarding-rules create ${SERVICE_NAME}-http-rule \
    --global \
    --target-http-proxy=${SERVICE_NAME}-http-proxy \
    --ports=80 \
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
