# Soundings — every target runs with MODEL_MODE=replay unless the owner overrides
# it on the command line after a paid-run approval (docs/WORKFLOW.md §7).
export MODEL_MODE ?= replay

.PHONY: setup check lint types test web dev sim sim-plot demo eval denylist clean

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

dev:
	uv run uvicorn soundings.api:app --reload --port 8000

sim:
	uv run python -m soundings.sim --seed 7 --ticks 60 --scenario smoke

sim-plot:
	uv run python -m soundings.sim.plot --scenario $(scenario) --out artifacts/$(scenario).png

demo:
	uv run python -m soundings.demo --script docs/demo-script.yaml --mode replay

eval:
	uv run python -m soundings.eval --suite catalog --seeds 1,2,3,4,5 --mode replay --out artifacts/eval

clean:
	rm -rf artifacts/*.png artifacts/eval web/dist
