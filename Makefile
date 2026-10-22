ARM ?= A4
COUNT ?= 5
PLAN ?= results/plans/$(ARM).json
TASKS ?= data/private/tasks.jsonl
IMAGES ?= configs/private/images.json
PYTHON ?= .venv/bin/python

.PHONY: plan reproduce check
plan:
	$(PYTHON) -m ctxroute.evalkit.cli plan --arms $(ARM) --count $(COUNT) --output $(PLAN)

# Execution remains blocked while runtime=offline_only or paid execution=false.
reproduce:
	$(PYTHON) -m ctxroute.evalkit.cli run --plan $(PLAN) --tasks $(TASKS) --images $(IMAGES) --execute

check:
	sh scripts/check.sh
