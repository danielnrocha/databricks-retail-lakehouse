# Retail Lakehouse — developer entrypoints.
#
# Two virtualenvs exist on purpose. databricks-connect and pyspark ship the same `pyspark`
# module and cannot coexist; see the note in pyproject.toml. Keeping them separate is the
# difference between "integration tests are slow" and "unit tests silently lie".

UV       ?= uv
VENV     := .venv
VENV_DBC := .venv-dbconnect
PY       := $(VENV)/bin/python
PYTEST   := $(VENV)/bin/pytest

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
.PHONY: setup
setup: $(VENV) ## Create the local (pyspark) environment
	@echo "Local environment ready. Run 'make setup-dbconnect' for integration work."

$(VENV):
	$(UV) venv $(VENV) --python 3.12
	VIRTUAL_ENV=$(VENV) $(UV) pip install -e ".[local-spark,data,ml,dev]"

.PHONY: setup-dbconnect
setup-dbconnect: ## Create the separate Databricks Connect environment
	$(UV) venv $(VENV_DBC) --python 3.12
	VIRTUAL_ENV=$(VENV_DBC) $(UV) pip install -e ".[dbconnect,data,ml,dev]"

.PHONY: clean
clean: ## Remove virtualenvs and caches
	rm -rf $(VENV) $(VENV_DBC) .pytest_cache .mypy_cache .ruff_cache dist build
	find . -name __pycache__ -type d -prune -exec rm -rf {} +

# ---------------------------------------------------------------------------
# Quality gates — run in this order; each is cheap enough to run on save
# ---------------------------------------------------------------------------
.PHONY: fmt
fmt: ## Format
	$(VENV)/bin/ruff format src tests generator scripts
	$(VENV)/bin/ruff check --fix src tests generator scripts

.PHONY: lint
lint: ## Lint (no fixes)
	$(VENV)/bin/ruff format --check src tests generator scripts
	$(VENV)/bin/ruff check src tests generator scripts

.PHONY: types
types: ## Type check
	$(VENV)/bin/mypy src scripts

.PHONY: trace
trace: ## Enforce requirement -> test traceability
	python3 scripts/check_traceability.py

.PHONY: test-unit
test-unit: ## Unit tests — no workspace required (ENV-006)
	$(PYTEST) tests/unit -m unit

.PHONY: test-integration
test-integration: ## Integration tests — requires an authenticated workspace
	$(VENV_DBC)/bin/pytest tests/integration -m integration

.PHONY: check
check: lint types trace test-unit ## Everything CI runs on a pull request

# ---------------------------------------------------------------------------
# Databricks
# ---------------------------------------------------------------------------
.PHONY: auth
auth: ## Authenticate the CLI against the workspace (OAuth U2M)
	databricks auth login --host $${DATABRICKS_HOST} --profile $${DATABRICKS_PROFILE:-DEFAULT}

.PHONY: validate
validate: ## Validate the bundle for a target (TARGET=dev|test|prod)
	databricks bundle validate -t $${TARGET:-dev}

.PHONY: deploy
deploy: ## Deploy the bundle (TARGET=dev|test|prod)
	databricks bundle deploy -t $${TARGET:-dev}

.PHONY: destroy
destroy: ## Tear down a deployed target
	databricks bundle destroy -t $${TARGET:-dev}
