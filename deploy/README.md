# Deploy — Azure Container Apps (Azure.2a / Azure.2b)

This directory contains the deployment artifacts for the podcast search service.
The **coder** authors these files; the **operator** runs them in Azure.2b.

---

## Observability note

**No Langfuse keys in the cloud deploy.** Observability is App Insights only,
via the system-assigned Managed Identity. The `DefaultAzureCredential` chain
resolves to the MI in-cluster — no SDK key or connection string lives in the
image.

---

## Prerequisites

### 1. Azure CLI

```bash
az --version      # ≥ 2.61
az login
az account set --subscription "<your-subscription-id>"
```

### 2. Resource providers registered

```bash
az provider register --namespace Microsoft.App               # Container Apps
az provider register --namespace Microsoft.ContainerRegistry # ACR
az provider register --namespace microsoft.insights          # App Insights
```

Check status: `az provider show --namespace Microsoft.App --query registrationState`

### 3. Infrastructure in place

The script assumes these resources already exist in `$RESOURCE_GROUP`:

| Resource | Variable | Description |
|---|---|---|
| Resource group | `RESOURCE_GROUP` | `podcast-search-rg` (default) |
| ACR | `ACR_NAME` | `podcastsearchacr` (default) |
| Container Apps environment | `ENV_NAME` | `podcast-search-env` (default) |
| App Insights component | `APPINSIGHTS_NAME` | `podcast-search-insights` (default) |

Create them if they don't exist yet:

```bash
# Resource group
az group create --name podcast-search-rg --location westeurope

# ACR (Basic tier is sufficient; GRS not needed for a dev image)
# COST: ACR Basic ~$0.167/day + storage ~$0.003/GB/day
az acr create --resource-group podcast-search-rg \
              --name podcastsearchacr \
              --sku Basic

# Container Apps environment (Consumption plan — pay-per-use)
az containerapp env create --name podcast-search-env \
                           --resource-group podcast-search-rg \
                           --location westeurope

# Application Insights
# COST: first 5 GB/month free, then ~$2.30/GB
az monitor app-insights component create \
  --app podcast-search-insights \
  --resource-group podcast-search-rg \
  --location westeurope \
  --kind web
```

---

## Deploy

Run from the **repo root** (the Dockerfile and `rag/` code are there):

```bash
IMAGE_TAG=azure2a bash deploy/azure-containerapp.sh
```

Override any name with env vars:

```bash
RESOURCE_GROUP=my-rg ACR_NAME=myacr IMAGE_TAG=v1 bash deploy/azure-containerapp.sh
```

What the script does (in order):

1. **ACR build** — builds the image server-side (`az acr build --platform linux/amd64`). No local Docker daemon needed. This is the fix for the Azure.1 arm64 blocker.
2. **Resolves App Insights connection string** from Azure (not hardcoded).
3. **Creates or updates** the Container App: external ingress, port 8000, system-assigned MI, min-replicas=0 (scale-to-zero).
4. **Grants the MI** the `Monitoring Metrics Publisher` role on the App Insights component so the `azure-monitor-opentelemetry-exporter` can push traces without an explicit key.
5. **Prints the public FQDN** for smoke testing.

---

## Smoke the live endpoint

```bash
FQDN="<fqdn-from-deploy-output>"

# Liveness probe (no auth required)
curl https://$FQDN/healthz
# Expected: {"status":"ok"}

# Semantic search
curl -s -X POST https://$FQDN/search \
  -H "Content-Type: application/json" \
  -d '{"query": "machine learning", "top_k": 3}' | python3 -m json.tool
# Expected: {"query":..., "n_episodes":..., "n_chunks":3, "chunks":[...]}
```

If `SERVICE_API_KEY` is set on the app, add `-H "x-api-key: <key>"` to the
`/search` request (`/healthz` remains open for probes).

---

## Cost summary

| Resource | When | Approximate cost |
|---|---|---|
| ACR build task | Per deploy | ~$0.0001/vCPU·s (build ~5–10 min) |
| ACR storage | Ongoing | ~$0.003/GB/day per stored image |
| Container Apps | While replicas are active | ~$0.000024/vCPU·s + ~$0.000003/GiB·s |
| App Insights ingestion | Ongoing | Free up to 5 GB/month, then ~$2.30/GB |

With `min-replicas=0` (scale-to-zero default) the Container App costs nothing
while idle. A single 0.5 vCPU replica kept always-on costs roughly **$0.35/day**.

---

## Teardown

```bash
az group delete --name podcast-search-rg --yes --no-wait
```

This removes all resources in the group. Confirm the group name before running.
