#!/usr/bin/env bash
# Destroy everything scripts/deploy.sh created. The GHCR images stay (free); delete them in the GitHub UI.
set -euo pipefail
cd "$(dirname "$0")/.."
terraform -chdir=infra/terraform destroy -input=false -auto-approve
echo "destroyed"
