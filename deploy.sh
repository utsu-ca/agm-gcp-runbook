#!/bin/bash
set -e

# --- CONFIGURATION ---
PROJECT_ID=$(gcloud config get-value project)
REGION="us-central1"
SERVICE_NAME="student-checkin-api"
ALLOWED_IP="203.0.113.50/32"

echo "Deploying to project: $PROJECT_ID in $REGION"
read -p "Enter your custom domain or subdomain (e.g., checkin.example.com): " DOMAIN_NAME

# 0. Initialize Database
echo "Installing dependencies and seeding Firestore database..."
pip install -r requirements.txt
python seed.py

# 1. Enable required APIs
echo "Enabling GCP APIs..."
gcloud services enable run.googleapis.com compute.googleapis.com firestore.googleapis.com

# 2. Deploy Cloud Run
echo "Deploying Cloud Run service..."
gcloud run deploy $SERVICE_NAME \
    --source . \
    --region=$REGION \
    --ingress=internal-and-cloud-load-balancing \
    --allow-unauthenticated \
    --quiet

# 3. Create Cloud Armor Security Policy
echo "Creating Cloud Armor policy..."
gcloud compute security-policies create checkin-ip-allowlist \
    --description "Deny all except whitelisted IPs" \
    --global || true

gcloud compute security-policies rules update 2147483647 \
    --security-policy checkin-ip-allowlist \
    --action "deny-403" || true

gcloud compute security-policies rules create 1000 \
    --security-policy checkin-ip-allowlist \
    --description "Allow admin network" \
    --src-ip-ranges "$ALLOWED_IP" \
    --action "allow" || true

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
