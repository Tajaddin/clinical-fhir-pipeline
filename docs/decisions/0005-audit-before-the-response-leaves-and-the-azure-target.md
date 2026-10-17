# 0005. Audit before the response leaves, API keys with roles, and the Azure target

Date: 2026-10-17

## Context

Every access to patient-level data in a hospital system is logged: who, what, which patient, why and when.
The brief asks for an audit log of every access, authentication, rate limiting and a cloud deployment with
Terraform. The project also has to run entirely on a laptop with no accounts.

## Decision

- **Audit in middleware, before the response is sent.** Every request under `/patients` and `/audit` writes
  a row with the actor (a fingerprint of the API key, never the key), role, action (method and route
  template), patient id, the stated purpose (`X-Purpose` header), request id, client address and status
  code. Denied and failed requests are logged too. If the audit store is unavailable the request is
  answered 503 rather than served unlogged, and a counter and an alert fire.
- **API keys with two roles.** `analyst` reads the cohort, patients, risk and summaries; `admin` also reads
  the audit log. Keys come from the environment as `key:role` pairs; there is no user database because
  this is a service API behind a hospital's identity provider in real life.
- **Per-key rate limit per route** (slowapi), one limiter per application instance.
- **Operational store** in SQLAlchemy with SQLite on a laptop and Postgres in Compose and in the cloud;
  the schema is identical.
- **Azure** for the Terraform: a storage account with hierarchical namespace for the lakehouse (delta-rs
  reads `abfs://` with the same code), a PostgreSQL flexible server, the API as a Container App that
  scales to zero, the dashboard as a Static Web App, Log Analytics for logs. Azure because the healthcare
  employers this project targets run on it, and because P3 in the portfolio already needs the same
  account. Nothing is applied without the owner's login and cost approval.
- Observability as in the rest of the portfolio: JSON logs, Prometheus metrics, OpenTelemetry traces when
  an endpoint is configured, a Grafana dashboard, seven alert rules with promtool tests.

## Consequences

- The audit write adds a few milliseconds to every patient request; a batch or async writer would be the
  first optimisation if it mattered.
- Rate limiting is per process; several replicas need a shared store.
- The summary endpoint is excluded from the latency alert because a local 3B model takes seconds.
