#!/usr/bin/env bash
# Off-season "sleep": removes the fixed-cost load balancer pieces and the scheduled export.
# Keeps Firestore data, the Cloud Run service (scales to zero), the service account, the Cloud Armor
# policy and the static IP (so DNS stays valid). Re-run ./deploy.sh to bring everything back.
# Set DELETE_STATIC_IP=1 to also release the IP (DNS must then be updated after the next deploy).
set -uo pipefail

REGION="${REGION:-us-central1}"
SERVICE_NAME="student-checkin-api"
JOB_NAME="${SERVICE_NAME}-export"
POLICY="checkin-ip-allowlist"

exists() { "$@" >/dev/null 2>&1; }
del() {  # description, describe+delete via: kind name [scope flags...]
    local kind="$1" name="$2"; shift 2
    if exists gcloud compute $kind describe "$name" "$@"; then
        echo "--> Deleting $kind $name"
        gcloud compute $kind delete "$name" "$@" --quiet
    fi
}

echo "Tearing down load balancer and scheduled export in $(gcloud config get-value project 2>/dev/null)..."
exists gcloud scheduler jobs describe "${JOB_NAME}-schedule" --location="$REGION" && \
    gcloud scheduler jobs delete "${JOB_NAME}-schedule" --location="$REGION" --quiet
exists gcloud run jobs describe "$JOB_NAME" --region="$REGION" && \
    gcloud run jobs delete "$JOB_NAME" --region="$REGION" --quiet

del forwarding-rules "${SERVICE_NAME}-https-rule" --global
del target-https-proxies "${SERVICE_NAME}-https-proxy" --global
del url-maps "${SERVICE_NAME}-url-map" --global
del backend-services "${SERVICE_NAME}-backend" --global
del network-endpoint-groups "${SERVICE_NAME}-neg" --region="$REGION"
del ssl-certificates "${SERVICE_NAME}-cert" --global
del security-policies "$POLICY" --global
[ "${DELETE_STATIC_IP:-}" = "1" ] && del addresses "${SERVICE_NAME}-ip" --global

echo "Done. The site is offline. Re-run ./deploy.sh to restore it."
