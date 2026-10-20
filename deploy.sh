#!/usr/bin/env bash
# One-shot deploy: clone this repo in Google Cloud Shell, run ./deploy.sh, answer the prompts.
# Safe to re-run. Any answer can be pre-set via environment variable to skip its prompt:
#   DOMAIN_NAME, ALLOWED_DOMAIN, GOOGLE_CLIENT_ID, ALLOWED_IP, SPREADSHEET_ID
set -euo pipefail
trap 'echo "ERROR: deploy failed at line $LINENO: $BASH_COMMAND" >&2' ERR

REGION="${REGION:-us-central1}"
SERVICE_NAME="student-checkin-api"
SA_NAME="checkin-api-sa"
POLICY="checkin-ip-allowlist"

# --- Prompt helpers ---------------------------------------------------------
# ask VAR "Question" REGEX "Error hint" [default]  -> keeps asking until the answer matches REGEX
ask() {
    local var="$1" question="$2" regex="$3" hint="$4" default="${5:-}" answer
    answer="${!var:-}"
    while :; do
        if [ -z "$answer" ]; then
            read -r -p "$question${default:+ [$default]}: " answer
            answer="${answer:-$default}"
        fi
        if [[ "$answer" =~ $regex ]]; then break; fi
        echo "  Invalid: $hint" >&2
        answer=""
    done
    printf -v "$var" '%s' "$answer"
}

# gcloud describe-or-create helper: exists <describe command...>
exists() { "$@" >/dev/null 2>&1; }

# --- Preflight --------------------------------------------------------------
command -v gcloud >/dev/null || { echo "ERROR: gcloud CLI not found (use Google Cloud Shell)." >&2; exit 1; }
PROJECT_ID="$(gcloud config get-value project 2>/dev/null)"
if [ -z "$PROJECT_ID" ] || [ "$PROJECT_ID" = "(unset)" ]; then
    echo "ERROR: no project selected. Run: gcloud config set project <PROJECT_ID>" >&2
    exit 1
fi
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
echo "Deploying to project: $PROJECT_ID in $REGION"
echo ""

# --- Questions --------------------------------------------------------------
HOST_RE='^([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$'
ask DOMAIN_NAME    "Website address people will visit (e.g. checkin.example.com)" "$HOST_RE" "enter a hostname like checkin.example.com"
ask ALLOWED_DOMAIN "Google Workspace domain allowed to sign in" "$HOST_RE" "enter a domain like example.com" "utsu.ca"
ask ALLOWED_IP     "Office IP or CIDR allowed to connect (e.g. 198.51.100.7/32; find yours at https://ifconfig.me)" \
    '^([0-9]{1,3}\.){3}[0-9]{1,3}(/[0-9]{1,2})?$' "enter an IPv4 address or CIDR"
case "$ALLOWED_IP" in */*) ;; *) ALLOWED_IP="$ALLOWED_IP/32" ;; esac

if [ -z "${GOOGLE_CLIENT_ID:-}" ]; then
    cat <<MSG

======== One-time OAuth client setup (about 1 minute) ========
1. Consent screen (set User type = Internal so only your org can sign in):
   https://console.cloud.google.com/auth/overview?project=${PROJECT_ID}
2. Create client: https://console.cloud.google.com/auth/clients/create?project=${PROJECT_ID}
   - Application type:              Web application
   - Authorized JavaScript origins: https://${DOMAIN_NAME}
   - Authorized redirect URIs:      (leave empty)
3. Copy the Client ID (ends in .apps.googleusercontent.com).
===============================================================
MSG
fi
ask GOOGLE_CLIENT_ID "OAuth client ID" '^[0-9A-Za-z._-]+\.apps\.googleusercontent\.com$' \
    "must end in .apps.googleusercontent.com"

echo ""
echo "Optional live dashboard: create a Google Sheet and share it (Editor) with:"
echo "  $SA_EMAIL"
SPREADSHEET_ID="${SPREADSHEET_ID-}"
if [ -z "$SPREADSHEET_ID" ]; then
    read -r -p "Spreadsheet ID (leave blank to skip): " SPREADSHEET_ID
fi
if [ -n "$SPREADSHEET_ID" ] && [[ ! "$SPREADSHEET_ID" =~ ^[A-Za-z0-9_-]+$ ]]; then
    echo "ERROR: Spreadsheet ID may only contain letters, digits, '-' and '_'." >&2
    exit 1
fi

# --- 1. APIs ----------------------------------------------------------------
echo "--> Enabling GCP APIs..."
gcloud services enable run.googleapis.com compute.googleapis.com firestore.googleapis.com \
    iam.googleapis.com sheets.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com

# --- 2. Firestore database --------------------------------------------------
if ! exists gcloud firestore databases describe --database="(default)"; then
    echo "--> Creating Firestore database..."
    gcloud firestore databases create --location="$REGION" --type=firestore-native
fi

# --- 3. Least-privilege service account -------------------------------------
echo "--> Setting up service account..."
if ! exists gcloud iam service-accounts describe "$SA_EMAIL"; then
    gcloud iam service-accounts create "$SA_NAME" --display-name="Student Checkin API SA"
    # eventual-consistency buffer after SA creation
    echo " Sleeping for 30s to ensure account is created... "
    sleep 30s
fi
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:$SA_EMAIL" --role="roles/datastore.user" --condition=None --quiet >/dev/null

# --- 4. Cloud Run -----------------------------------------------------------
echo "--> Deploying Cloud Run service (this takes a few minutes)..."
ENV_VARS="^|^GOOGLE_CLIENT_ID=$GOOGLE_CLIENT_ID|ALLOWED_DOMAIN=$ALLOWED_DOMAIN"
DEPLOY_ARGS=(
    --source .
    --region="$REGION"
    --ingress=internal-and-cloud-load-balancing
    --allow-unauthenticated  # reachable only via the load balancer, which enforces Cloud Armor
    --service-account="$SA_EMAIL"
    --quiet
)
if [ -n "$SPREADSHEET_ID" ]; then
    ENV_VARS="$ENV_VARS|SPREADSHEET_ID=$SPREADSHEET_ID"
    # The dashboard export runs inside the service every minute, so keep one instance running.
    DEPLOY_ARGS+=(--min-instances=1 --max-instances=1 --no-cpu-throttling)
else
    DEPLOY_ARGS+=(--min-instances=0)
fi
DEPLOY_ARGS+=(--set-env-vars="$ENV_VARS")
gcloud run deploy "$SERVICE_NAME" "${DEPLOY_ARGS[@]}"

# --- 5. Cloud Armor ---------------------------------------------------------
echo "--> Configuring Cloud Armor policy & WAF..."
if ! exists gcloud compute security-policies describe "$POLICY" --global; then
    gcloud compute security-policies create "$POLICY" --description "WAF and IP allowlist" --global
fi
gcloud compute security-policies rules update 2147483647 --security-policy "$POLICY" --action "deny-403"

ensure_rule() {  # priority, description, then the remaining rule flags
    local priority="$1" description="$2"; shift 2
    if exists gcloud compute security-policies rules describe "$priority" --security-policy "$POLICY"; then
        gcloud compute security-policies rules update "$priority" --security-policy "$POLICY" "$@"
    else
        gcloud compute security-policies rules create "$priority" --security-policy "$POLICY" \
            --description "$description" "$@"
    fi
}
ensure_rule 900 "Block SQL Injection" --action deny-403 \
    --expression "evaluatePreconfiguredWaf('sqli-v422-stable', {'sensitivity': 1})"
ensure_rule 901 "Block Cross-Site Scripting" --action deny-403 \
    --expression "evaluatePreconfiguredWaf('xss-v422-stable', {'sensitivity': 1})"
ensure_rule 1000 "Throttle per IP" --action throttle --src-ip-ranges "$ALLOWED_IP" \
    --rate-limit-threshold-count 60 --rate-limit-threshold-interval-sec 60 \
    --conform-action allow --exceed-action deny-429 --enforce-on-key ip
ensure_rule 1100 "Allow office network" --action allow --src-ip-ranges "$ALLOWED_IP"

# --- 6. Load balancer -------------------------------------------------------
echo "--> Creating load balancer..."
create_if_missing() {  # resource kind, name, then the remaining create flags
    local kind="$1" name="$2"; shift 2
    # shellcheck disable=SC2086
    if ! exists gcloud compute $kind describe "$name" "${SCOPE[@]}"; then
        # shellcheck disable=SC2086
        gcloud compute $kind create "$name" "$@" "${SCOPE[@]}"
    fi
}

SCOPE=(--global)
create_if_missing addresses "${SERVICE_NAME}-ip" --network-tier=PREMIUM
IP_ADDRESS="$(gcloud compute addresses describe "${SERVICE_NAME}-ip" --global --format='value(address)')"
create_if_missing ssl-certificates "${SERVICE_NAME}-cert" --domains="$DOMAIN_NAME"

SCOPE=(--region="$REGION")
create_if_missing network-endpoint-groups "${SERVICE_NAME}-neg" \
    --network-endpoint-type=serverless --cloud-run-service="$SERVICE_NAME"

SCOPE=(--global)
create_if_missing backend-services "${SERVICE_NAME}-backend" \
    --load-balancing-scheme=EXTERNAL_MANAGED

# backend-services create does not support --security-policy; attach policy via update
gcloud compute backend-services update "${SERVICE_NAME}-backend" \
    --global --security-policy="$POLICY"

if ! gcloud compute backend-services describe "${SERVICE_NAME}-backend" --global \
        --format='value(backends[].group)' | grep -q "${SERVICE_NAME}-neg"; then
    gcloud compute backend-services add-backend "${SERVICE_NAME}-backend" --global \
        --network-endpoint-group="${SERVICE_NAME}-neg" --network-endpoint-group-region="$REGION"
fi

create_if_missing url-maps "${SERVICE_NAME}-url-map" --default-service "${SERVICE_NAME}-backend"
create_if_missing target-https-proxies "${SERVICE_NAME}-https-proxy" \
    --url-map="${SERVICE_NAME}-url-map" --ssl-certificates="${SERVICE_NAME}-cert"
create_if_missing forwarding-rules "${SERVICE_NAME}-https-rule" \
    --target-https-proxy="${SERVICE_NAME}-https-proxy" --ports=443 --address="${SERVICE_NAME}-ip"

cat <<MSG

======================================================
DEPLOYED!
Static IP address: $IP_ADDRESS
Website:           https://$DOMAIN_NAME
======================================================
NEXT STEPS:
1. At your DNS provider, create an A record: $DOMAIN_NAME -> $IP_ADDRESS
   (Google issues the SSL certificate automatically once DNS propagates; can take up to an hour.)
2. Load students (trusted CSV with student_number,name,preferred_name,expected_location):
     pip install google-cloud-firestore && python seed.py students.csv
MSG
