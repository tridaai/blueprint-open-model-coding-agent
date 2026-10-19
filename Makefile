# Trida AI Blueprint — Open-Model Coding Agent on Baseten
#
# Primary workflow is uv (https://docs.astral.sh/uv/):
#   make install     uv sync --extra dev
#   make demo        one gold task end to end, offline (no keys)
#   make test        pytest
#   make evals       gold task gate, offline (scripted model)
#   make serve       the API + console on http://localhost:8000
#
# No uv? python3 -m venv .venv && source .venv/bin/activate, then:
#   make install-pip

.PHONY: install install-pip demo test evals serve

install:
	uv sync --extra dev

install-pip:
	# The frozen, hash-verified set first, then the package itself with
	# --no-deps: pip refuses an editable install under a hashed
	# constraints file, so the two steps never mix.
	pip install -r constraints.txt
	pip install --no-deps -e .
	pip install "pytest==9.1.1" "httpx==0.28.1"

demo:
	uv run coding-agent-demo

test:
	uv run pytest -q

evals:
	uv run python evals/run_evals.py

serve:
	uv run uvicorn open_model_coding_agent.api:app --port 8000
