# Deploying

## Local (no accounts)

```bash
make synthea && make pipeline && make report
make up          # postgres :5437, api :8110, dashboard :4210
make up-obs      # prometheus :9105, alertmanager :9106, grafana :3007 (admin / change-me)
make down
```

The API container mounts `data/lakehouse` read-only and `reports/` for the model card and the offline
faithfulness report. Summaries go to `CFP_LLM_BASE_URL`; the Compose default is Ollama on the host
(`http://host.docker.internal:11434/v1`, model `qwen2.5:3b`). Set `CFP_LLM_BASE_URL=` (empty) for the
deterministic fake model. `docker compose --profile jobs run --rm pipeline` re-runs the pipeline inside the
image against the mounted data, the way a scheduled job would.

## Azure with Terraform

What it creates (`infra/terraform`), region `eastus2` by default:

| Resource | SKU | Cost while it exists |
|---|---|---|
| Storage account, hierarchical namespace, container `lakehouse` | Standard LRS | cents per month at this size |
| PostgreSQL flexible server, 32 GB | B_Standard_B1ms | about $13/month (the only line item that always costs money) |
| Container Apps environment and the API app | consumption, 0.5 vCPU / 1 GiB, 0 to 2 replicas | free grant covers a demo; scales to zero |
| Static Web App | Free | free |
| Log Analytics workspace | PerGB2018, 30 days | first 5 GB/month free |

Steps:

1. `az login`, `gh auth login`, Terraform 1.9+, Docker, Node 24.
2. `cp infra/terraform/terraform.tfvars.example infra/terraform/terraform.tfvars` and fill in the
   subscription id, two long random API keys with their roles, the `CFP_DEID_SALT` the pipeline used, and
   either an OpenAI-compatible `llm_base_url` with its key or leave it empty for the fake model. For a
   private GHCR image add a token with `read:packages`.
3. `make deploy`: builds and pushes `ghcr.io/<you>/clinical-fhir-pipeline/api:<sha>`, applies the
   Terraform, uploads `data/lakehouse` to the storage account with `az storage blob upload-batch`, builds
   the dashboard with the API URL written into `config.js` and publishes it to the Static Web App.
4. Check `<api_url>/health`, open the dashboard URL, paste a key.
5. `make teardown` when the demo is over.

The audit log, the stored summaries and the lakehouse are the state; `terraform destroy` deletes all of it.

### Summaries in the cloud

A Container App has no GPU and Ollama is not part of the deployment. Options: Azure OpenAI (any chat model
with JSON mode behind its OpenAI-compatible endpoint), another hosted OpenAI-compatible API, or the fake
model (deterministic, cites correctly, useful for demonstrating the audit trail and the dashboard without a
model bill). The faithfulness checker runs the same way whichever model answers.

## CI deploy

Not wired: the brief asks for deploy-on-merge, and the Terraform and scripts are ready, but applying them
needs the owner's Azure credentials in GitHub environment secrets (`AZURE_CREDENTIALS`,
`TF_VAR_subscription_id`, the tfvars secrets). `ci.yml` builds the images on every push; a `deploy.yml`
that runs `scripts/deploy.sh` on `workflow_dispatch` and on merge is a short addition once those secrets
exist.
