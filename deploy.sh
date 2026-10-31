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
ask ALLOWED_IP     "Office IPs/CIDRs allowed to connect, comma-separated, max 10 (e.g. 198.51.100.7/32,203.0.113.0/24)" \
    '^([0-9]{1,3}\.){3}[0-9]{1,3}(/[0-9]{1,2})?(,[[:space:]]*([0-9]{1,3}\.){3}[0-9]{1,3}(/[0-9]{1,2})?){0,9}$' \
    "enter up to 10 IPv4 addresses or CIDRs separated by commas"
ALLOWED_IP="$(printf '%s' "$ALLOWED_IP" | tr -d '[:space:]' | tr ',' '\n' | sed 's|^\([^/]*\)$|\1/32|' | paste -sd, -)"

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
# Scale to zero: the service only runs while handling requests. The dashboard export is a
# scheduled Cloud Run Job (below), so nothing needs an always-on instance.
[ -n "$SPREADSHEET_ID" ] && ENV_VARS="$ENV_VARS|SPREADSHEET_ID=$SPREADSHEET_ID"
DEPLOY_ARGS+=(--min-instances=0 --max-instances="${MAX_INSTANCES:-3}" --cpu=1 --memory=512Mi --cpu-boost)
DEPLOY_ARGS+=(--set-env-vars="$ENV_VARS")
gcloud run deploy "$SERVICE_NAME" "${DEPLOY_ARGS[@]}"

# Keep only the newest images built by --source deploys (cheaper Artifact Registry storage).
AR_REPO="cloud-run-source-deploy"
if exists gcloud artifacts repositories describe "$AR_REPO" --location="$REGION"; then
    POLICY_FILE="$(mktemp)"
    cat >"$POLICY_FILE" <<JSON
[{"name":"keep-recent","action":{"type":"Keep"},"mostRecentVersions":{"keepCount":3}},
 {"name":"delete-old","action":{"type":"Delete"},"condition":{"tagState":"any"}}]
JSON
    gcloud artifacts repositories set-cleanup-policies "$AR_REPO" --location="$REGION" \
        --policy="$POLICY_FILE" --no-dry-run --quiet || echo "  (could not set cleanup policy; continuing)"
    rm -f "$POLICY_FILE"
fi

# --- 4b. Scheduled dashboard export (Cloud Run Job + Cloud Scheduler) --------
JOB_NAME="${SERVICE_NAME}-export"
SCHEDULER_NAME="${JOB_NAME}-schedule"
EXPORT_SCHEDULE="${EXPORT_SCHEDULE:-*/5 8-18 * * 1-5}"   # every 5 min, 08:00-18:55 Mon-Fri
EXPORT_TZ="${EXPORT_TZ:-America/Toronto}"
if [ -n "$SPREADSHEET_ID" ]; then
    echo "--> Setting up scheduled dashboard export ($EXPORT_SCHEDULE $EXPORT_TZ)..."
    gcloud services enable cloudscheduler.googleapis.com
    IMAGE="$(gcloud run services describe "$SERVICE_NAME" --region="$REGION" \
        --format='value(spec.template.spec.containers[0].image)')"
    gcloud run jobs deploy "$JOB_NAME" --region="$REGION" --image="$IMAGE" \
        --service-account="$SA_EMAIL" --command=python --args=export_job.py \
        --set-env-vars="$ENV_VARS" --max-retries=1 --task-timeout=300s --quiet
    gcloud run jobs add-iam-policy-binding "$JOB_NAME" --region="$REGION" \
        --member="serviceAccount:$SA_EMAIL" --role="roles/run.invoker" --quiet >/dev/null
    JOB_URI="https://run.googleapis.com/v2/projects/${PROJECT_ID}/locations/${REGION}/jobs/${JOB_NAME}:run"
    SCHED_ARGS=(--location="$REGION" --schedule="$EXPORT_SCHEDULE" --time-zone="$EXPORT_TZ"
        --uri="$JOB_URI" --http-method=POST --oauth-service-account-email="$SA_EMAIL")
    if exists gcloud scheduler jobs describe "$SCHEDULER_NAME" --location="$REGION"; then
        gcloud scheduler jobs update http "$SCHEDULER_NAME" "${SCHED_ARGS[@]}"
    else
        gcloud scheduler jobs create http "$SCHEDULER_NAME" "${SCHED_ARGS[@]}"
    fi
else
    # Spreadsheet removed on a re-run: stop paying for the export.
    exists gcloud scheduler jobs describe "$SCHEDULER_NAME" --location="$REGION" && \
        gcloud scheduler jobs delete "$SCHEDULER_NAME" --location="$REGION" --quiet
    exists gcloud run jobs describe "$JOB_NAME" --region="$REGION" && \
        gcloud run jobs delete "$JOB_NAME" --region="$REGION" --quiet
fi

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
    --global --security-policy="$POLICY" --enable-logging --logging-sample-rate=0.1

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
2. Load students (trusted CSV with student_number,name,preferred_name,expected_location[,last_name]):
     pip install google-cloud-firestore && python seed.py students.csv
MSG
