.PHONY: install synthea bronze silver quality gold train eval pipeline serve up up-obs down lint format typecheck test alerts-test image images tf-validate dashboard-dev e2e-fixture e2e report deploy teardown

install:
	uv sync --extra dev
	cd dashboard && npm ci --no-audit --no-fund

synthea:        ## 2,000 synthetic Tampa patients with the pinned Synthea release (Java 11+)
	bash scripts/synthea.sh 2000 42

bronze:
	uv run cfp bronze

silver:
	uv run cfp silver

quality:        ## data quality checks; exit 1 on a critical failure
	uv run cfp quality

gold:
	uv run cfp gold

train:          ## fit, calibrate, evaluate; scores into gold.risk_scores; reports/model-metrics.json
	uv run cfp train

eval:           ## cited summaries for 40 patients with the configured model and the faithfulness report
	uv run cfp eval-summaries --patients 40

pipeline:       ## bronze -> silver -> quality -> gold -> train -> eval (20 patients)
	uv run cfp pipeline

serve:
	uv run cfp serve --port 8110

up:             ## Postgres, the API and the dashboard from local images
	docker compose up -d --build postgres api dashboard

up-obs:
	docker compose --profile observability up -d

down:
	docker compose --profile observability --profile jobs down -v

lint:
	uv run ruff check . && uv run ruff format --check .
	cd dashboard && npm run lint

format:
	uv run ruff format . && uv run ruff check --fix .
	cd dashboard && npx prettier --write "src/**/*.{ts,html,css}" "e2e/**/*.ts"

typecheck:
	uv run mypy

test:
	uv run pytest --cov=cfp --cov-report=term-missing --cov-report=xml

alerts-test:
	docker run --rm -v "$(CURDIR)/observability:/w" -w /w --entrypoint promtool prom/prometheus:v3.5.0 test rules alerts_test.yml

image:
	docker build -t cfp/api:local .

images: image
	docker build -t cfp/dashboard:local dashboard

tf-validate:
	docker run --rm -v "$(CURDIR)/infra/terraform:/w" -w /w hashicorp/terraform:1.9 fmt -check
	docker run --rm -v "$(CURDIR)/infra/terraform:/w" -w /w hashicorp/terraform:1.9 init -backend=false -input=false >/dev/null && docker run --rm -v "$(CURDIR)/infra/terraform:/w" -w /w hashicorp/terraform:1.9 validate

dashboard-dev:  ## Angular dev server on :4200 proxying /api to the API on :8110
	cd dashboard && npm start

e2e-fixture:    ## deterministic 160-patient cohort under data/e2e for end-to-end tests
	uv run python scripts/e2e_fixture.py

e2e:            ## Playwright against an API serving data/e2e (start it with: make serve-e2e)
	cd dashboard && npx playwright test

serve-e2e:
	CFP_LAKEHOUSE_ROOT=data/e2e/lakehouse CFP_REPORTS_DIR=data/e2e/reports CFP_DATABASE_URL=sqlite:///data/e2e/cfp.db CFP_API_KEYS=e2e-analyst:analyst,e2e-admin:admin CFP_LLM_BASE_URL= uv run cfp serve --port 8110

report:         ## render reports/*.json into README.md and docs/MODEL-CARD.md
	uv run python scripts/render_results.py --write

deploy:         ## Azure via Terraform (see docs/DEPLOY.md)
	bash scripts/deploy.sh

teardown:
	bash scripts/teardown.sh
