# Soundings — every target runs with MODEL_MODE=replay unless the owner overrides
# it on the command line after a paid-run approval (docs/WORKFLOW.md §7).
export MODEL_MODE ?= replay

.PHONY: setup check lint types test web e2e dev sim sim-plot pol demo eval deck denylist clean

setup:
	uv sync --all-extras
	pnpm -C web install

check: lint types denylist test web

lint:
	uv run ruff check . && uv run ruff format --check .

types:
	uv run mypy soundings

denylist:
	bash scripts/denylist-check.sh

test:
	uv run pytest -q

web:
	pnpm -C web exec tsc --noEmit && pnpm -C web build

e2e:
	pnpm -C web build && SOUNDINGS_E2E_REQUIRED=1 uv run pytest tests/e2e -q

dev:
	uv run uvicorn soundings.api:app --reload --port 8000

sim:
	uv run python -m soundings.sim --seed 7 --ticks 60 --scenario smoke

sim-plot:
	uv run python -m soundings.sim.plot --scenario $(scenario) --out artifacts/$(scenario).png

pol:
	uv run python -m soundings.investigation.pol --seed 42 --days 28 --out data/pol_table.json

demo:
	uv run python -m soundings.demo --script docs/demo-script.yaml --mode replay

eval:
	uv run python -m soundings.eval --suite catalog --seeds 1,2,3,4,5 --mode replay --out artifacts/eval

# Pitch deck (WO-090): every number from artifacts/eval/report.json; placeholders without it.
deck:
	uv run python scripts/build_deck.py
	uv run python scripts/export_deck.py

clean:
	rm -rf artifacts/*.png artifacts/eval web/dist
