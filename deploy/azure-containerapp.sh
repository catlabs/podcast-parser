#!/usr/bin/env bash
# deploy/azure-containerapp.sh
# Azure.2a artifact — authored by the coder, NOT run here.
# The operator runs this script in Azure.2b after the mentor verifies the slim
# image locally. See deploy/README.md for prerequisites and ordered steps.
#
# Usage:
#   bash deploy/azure-containerapp.sh            # uses IMAGE_TAG=latest
#   IMAGE_TAG=v1 bash deploy/azure-containerapp.sh
#
# The script is idempotent: it updates an existing Container App rather than
# failing if the app already exists.

set -euo pipefail

# ── Configurable names — override via env or edit here ───────────────────────
RESOURCE_GROUP="${RESOURCE_GROUP:-podcast-search-rg}"
LOCATION="${LOCATION:-westeurope}"
ACR_NAME="${ACR_NAME:-podcastsearchacr}"
ENV_NAME="${ENV_NAME:-podcast-search-env}"
APP_NAME="${APP_NAME:-podcast-search}"
IMAGE_TAG="${IMAGE_TAG:-latest}"
APPINSIGHTS_NAME="${APPINSIGHTS_NAME:-podcast-search-insights}"

IMAGE="$ACR_NAME.azurecr.io/podcast-search:$IMAGE_TAG"

echo "=== Azure.2b: deploying $IMAGE to Container Apps ==="
echo "  Resource group : $RESOURCE_GROUP"
echo "  Location       : $LOCATION"
echo "  ACR            : $ACR_NAME"
echo "  App            : $APP_NAME"
echo ""

# ── 1. Build the image in the cloud ──────────────────────────────────────────
# ACR Tasks build runs server-side — no local Docker daemon required. The
# --platform flag is mandatory: local dev is typically arm64; ACA runs amd64.
#
# COST: ACR task compute (~$0.0001/vCPU·s, build typically 5–10 min for this
#       image) + ACR storage (~$0.003/GB/day for the stored image layers).
az acr build \
  --registry    "$ACR_NAME" \
  --platform    linux/amd64 \
  --image       "podcast-search:$IMAGE_TAG" \
  .

# ── 2. Resolve the App Insights connection string (no hardcoded secret) ───────
# The connection string is read dynamically so it never has to be stored in
# this script, a .env file, or the ACR image. The Container App receives it as
# a plain env var; the azure-monitor exporter uses it without an explicit key.
#
# COST: App Insights data ingestion (~$2.30/GB after the 5 GB/month free tier).
APPINSIGHTS_CONN_STR=$(az monitor app-insights component show \
  --app            "$APPINSIGHTS_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query          connectionString \
  --output         tsv)

# ── 3. Create or update the Container App ────────────────────────────────────
# min-replicas=0 → scale-to-zero: zero cost at idle, cold-start latency of
# ~5–15 s on the first request after an idle period. Set to 1 to trade a small
# constant cost (~$0.02/vCPU·h × 0.5 vCPU) for warm availability.
#
# COST: Container Apps billing = vCPU·s + memory·s consumed during active
#       replicas. At min-replicas=0 you pay only when requests arrive.
if az containerapp show \
     --name            "$APP_NAME" \
     --resource-group  "$RESOURCE_GROUP" \
     &>/dev/null; then
  echo "Container App '$APP_NAME' exists — updating image and env."
  az containerapp update \
    --name            "$APP_NAME" \
    --resource-group  "$RESOURCE_GROUP" \
    --image           "$IMAGE" \
    --set-env-vars    "APPLICATIONINSIGHTS_CONNECTION_STRING=$APPINSIGHTS_CONN_STR"
else
  echo "Container App '$APP_NAME' not found — creating."
  az containerapp create \
    --name              "$APP_NAME" \
    --resource-group    "$RESOURCE_GROUP" \
    --environment       "$ENV_NAME" \
    --image             "$IMAGE" \
    --registry-server   "$ACR_NAME.azurecr.io" \
    --target-port       8000 \
    --ingress           external \
    --min-replicas      0 \
    --max-replicas      3 \
    --cpu               0.5 \
    --memory            1.0Gi \
    --system-assigned \
    --env-vars          "APPLICATIONINSIGHTS_CONNECTION_STRING=$APPINSIGHTS_CONN_STR"
fi

# ── 4. Grant the Managed Identity the Monitoring Metrics Publisher role ───────
# The azure-monitor-opentelemetry-exporter authenticates via
# DefaultAzureCredential. In-cluster that resolves to the system-assigned MI —
# same pattern as the AzureBlobObjectStore in Step 8b. No SDK key in the image.
#
# principalId is the object ID of the MI created/confirmed in step 3.
PRINCIPAL_ID=$(az containerapp show \
  --name            "$APP_NAME" \
  --resource-group  "$RESOURCE_GROUP" \
  --query           "identity.principalId" \
  --output          tsv)

APPINSIGHTS_RESOURCE_ID=$(az monitor app-insights component show \
  --app            "$APPINSIGHTS_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query          id \
  --output         tsv)

az role assignment create \
  --assignee "$PRINCIPAL_ID" \
  --role     "Monitoring Metrics Publisher" \
  --scope    "$APPINSIGHTS_RESOURCE_ID"

# ── 5. Print the public FQDN ──────────────────────────────────────────────────
FQDN=$(az containerapp show \
  --name            "$APP_NAME" \
  --resource-group  "$RESOURCE_GROUP" \
  --query           "properties.configuration.ingress.fqdn" \
  --output          tsv)

echo ""
echo "=== Deploy complete ==="
echo "  FQDN         : https://$FQDN"
echo "  Health check : curl https://$FQDN/healthz"
echo "  Search smoke : curl -s -X POST https://$FQDN/search \\"
echo "                   -H 'Content-Type: application/json' \\"
echo "                   -d '{\"query\": \"machine learning\", \"top_k\": 3}'"
