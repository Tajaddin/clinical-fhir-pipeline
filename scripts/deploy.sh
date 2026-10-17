#!/usr/bin/env bash
# Deploy to Azure: build and push the API image to GHCR, apply the Terraform, upload the lakehouse to the
# storage account, and publish the dashboard to the Static Web App. Needs: az login, gh auth login, docker,
# terraform, node. Costs: see docs/DEPLOY.md before running.
set -euo pipefail
cd "$(dirname "$0")/.."

OWNER="${GHCR_OWNER:-$(gh api user --jq .login | tr '[:upper:]' '[:lower:]')}"
TAG="${TAG:-$(git rev-parse --short HEAD)}"
IMAGE="ghcr.io/${OWNER}/clinical-fhir-pipeline/api:${TAG}"
TFVARS=infra/terraform/terraform.tfvars
[ -f "$TFVARS" ] || { echo "copy infra/terraform/terraform.tfvars.example to $TFVARS and fill it in"; exit 1; }

echo "== image $IMAGE"
gh auth token | docker login ghcr.io -u "$OWNER" --password-stdin
docker build -t "$IMAGE" .
docker push "$IMAGE"

echo "== terraform"
terraform -chdir=infra/terraform init -input=false
terraform -chdir=infra/terraform apply -input=false -auto-approve -var "api_image=$IMAGE"
API_URL=$(terraform -chdir=infra/terraform output -raw api_url)
ACCOUNT=$(terraform -chdir=infra/terraform output -raw storage_account)
SWA_TOKEN=$(terraform -chdir=infra/terraform output -raw dashboard_deployment_token)

echo "== lakehouse upload to $ACCOUNT/lakehouse"
az storage blob upload-batch --account-name "$ACCOUNT" --destination lakehouse/lakehouse --source data/lakehouse --overwrite --only-show-errors

echo "== dashboard"
( cd dashboard && npm ci --no-audit --no-fund && API_BASE="$API_URL" npm run build -- --configuration production \
  && npx -y @azure/static-web-apps-cli@2 deploy dist/dashboard/browser --deployment-token "$SWA_TOKEN" --env production )

echo
echo "API:       $API_URL/health"
echo "Dashboard: $(terraform -chdir=infra/terraform output -raw dashboard_url)"
echo "Tear down with: make teardown"
