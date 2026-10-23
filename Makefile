ARM ?= A4
COUNT ?= 5
PLAN ?= results/plans/$(ARM).json
TASKS ?= data/private/tasks.jsonl
IMAGES ?= configs/private/images.json
PYTHON ?= .venv/bin/python
WORKFLOW ?= results/workflows/$(ARM)
STAGE ?= prepare
REPORTS ?= results/private/reports.json
OUTPUT ?= results/analysis/$(ARM)
EXECUTE ?=

.PHONY: plan reproduce check
plan:
	$(PYTHON) -m ctxroute.evalkit.cli plan --arms $(ARM) --count $(COUNT) --output $(PLAN)

# Default stage is offline. Inference additionally needs EXECUTE=--execute and live config.
reproduce:
	$(PYTHON) -m ctxroute.evalkit.cli reproduce $(STAGE) --workflow $(WORKFLOW) --plan $(PLAN) --tasks $(TASKS) --images $(IMAGES) --reports $(REPORTS) --output $(OUTPUT) $(EXECUTE)

check:
	sh scripts/check.sh
