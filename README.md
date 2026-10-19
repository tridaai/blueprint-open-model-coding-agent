<p align="center">
  <img src="https://trida.ai/logos-rounded/trida-rounded.png" alt="Trida AI" width="96">
</p>

# Blueprint — Open-Model Coding Agent

Every engineering organisation piloting AI coding help hits the same
wall. The frontier closed models are good and expensive, and the bill
grows with every engineer. Open models now perform close to the
frontier on coding at a fraction of the cost — but "close" is doing a
lot of work in that sentence. Close on whose tasks? Measured how? And
what happens when a cheaper model is confidently wrong inside your
codebase?

This blueprint is a coding agent built to answer those questions the
way an FDE team would: with a governed shell around the model, and an
evaluation harness that measures **cost per successful task** instead
of cost per token. The agent takes scoped work orders against a
codebase — a bug fix, a failing-test repair, a small feature — and
returns **a patch a human reviews**. A person approves the plan
before anything runs. The model works in a sandbox on a copy of the
repo, with edits confined to the approved file scope and commands
allowlisted to test runs. The patch it produces is then verified by
the shell, not by the agent: applied to a pristine copy, with the
grading tests re-run there. Only a verified patch reaches the review
gate, and only a human's acceptance exports it. The agent never
commits, never pushes, never merges.

The model layer is **open models served by Baseten's Model APIs** —
GLM, Kimi, DeepSeek, Qwen, gpt-oss and their peers behind one
OpenAI-compatible endpoint — and the second half of the blueprint
(rounds 6–8) builds the evaluation discipline as working code: a gold
task suite derived from real work, a governed price table, model
profiles, and a promotion gate a model must pass before it becomes
the agent's default. The workshop codebase and its gold tasks were
written for this blueprint.

> Reference prototype. Not connected to any real codebase, firm, or
> client system. Built against Baseten's public Model API; Trida AI
> is not affiliated with, endorsed by, or partnered with Baseten.

> **Work in progress** — this blueprint is built and passing its automated test and eval gates, but it has not yet finished hands-on local testing, and its companion blog post is not yet published. Expect changes. This notice comes off when both are done.

## Run it with a real model

The agent is built for a real open model served by Baseten. Set the
key and the live profile is selected by name; the governance around
the model — gates, sandbox, verification — does not change.

```bash
cp .env.example .env   # then set BASETEN_API_KEY
```

- **Baseten (headline provider)** — `BASETEN_API_KEY`, endpoint
  `https://inference.baseten.co/v1`, model slugs in `org/name` form:
  `zai-org/GLM-5` (the default named here), `moonshotai/Kimi-K2.5`,
  `deepseek-ai/DeepSeek-V3.1`, `openai/gpt-oss-120b`, and the Qwen and
  MiniMax families. The live adapter — tool calling over Baseten's
  OpenAI-compatible API — lands in **round 2**; until then the
  `baseten` profile exists (endpoint, key env, default slug) and a
  run under it refuses by name rather than silently substituting
  another model.
- **Offline (round 1's default)** — `MODEL_PROFILE=offline`: a
  labelled scripted model drives the same shell over the gold suite.
  That is what the tests, the evals, and the demo run; no key is
  needed and no model is called.
- Further endpoint profiles (NVIDIA NIM, a generic self-hosted
  OpenAI-compatible endpoint) join the profile loader as the rounds
  land — the profile is the seam, and it is fail-closed: an unknown
  profile name is a startup error, never a fallback.

## Quickstart

```bash
make install     # uv sync --extra dev   (no uv? make install-pip)
make demo        # one gold task end to end, offline
make test        # the shell's test suite
make evals       # the gold task gate
make serve       # API + console on http://localhost:8000
```

## How it works

1. **Work orders.** A task arrives as a stored record: the task text,
   the target repo, and an approved **file scope**. Gold tasks
   materialise their target from the bundled workshop codebase.
2. **Plan gate.** The model drafts a plan — approach, files, tests.
   A human approves or rejects it as a recorded act. Execution from
   an unapproved plan is refused in code (`plan_not_approved`).
3. **Sandboxed execution.** Each run copies the target into an
   isolated workspace. The model's tools are list / read / search /
   anchored edit / run-tests, behind a port that enforces the scope
   and the command allowlist in code: edits outside the scope are
   refused and recorded; the only command shape is pytest over named
   test files; the child environment is scrubbed of every
   credential-looking variable.
4. **Verification in code.** The run's patch is applied to a fresh
   copy of the target and the task's grading tests run there. A run
   is SUCCEEDED only when the patch applies cleanly, stays in scope,
   and the grading tests pass on that fresh copy. The agent's own
   in-run test results are never the verdict.
5. **Review gate.** A human accepts a verified run — the `.patch`
   file is exported to the run artifacts — or rejects it with a
   recorded reason. Nothing is applied to the source repo.
6. **Audit.** Every act — work order, plan, decision, tool call,
   refused edit, verification, review — is a stored record; a run's
   history reconstructs end to end.

## The gold suite (v1)

Five tasks, gold-by-construction: each ships a broken state of the
workshop codebase and grading tests that fail on it and pass on the
reference fix.

| Task | Shape | Scope |
| --- | --- | --- |
| Reservations can overcommit stock | bug fix | `workshop/inventory.py` |
| Tax is charged on the pre-discount subtotal | bug fix | `workshop/pricing.py` |
| The low-stock report always comes back empty | missing feature | `workshop/inventory.py` |
| Cancelling an order never releases its stock | bug fix | `workshop/orders.py` |
| Ten-unit orders miss the bulk discount | boundary fix | `workshop/pricing.py` |

`make evals` grades all five end to end through the service, plus
the construction proof and a gate check per task. Zero-tolerance
counters: edits landed outside scope, and successes without a passing
verification.

## Layout

```
src/open_model_coding_agent/  the governance shell (service, gates,
                              sandbox tools, patch + verification,
                              model port, store, API, console)
workshop/                     the sample codebase the agent works on
evals/gold/                   the gold task suite (broken/fixed/grading)
evals/run_evals.py            the eval gate
migrations/                   numbered SQL migrations, both dialects
tests/                        the shell's own test suite
docs/architecture.md          the design, in full
```

## Honest status

Round 1 (core) is built and gated: **27 tests passing, evals PASS**
(5/5 gold tasks verified end to end, counters at zero). What is
deliberately not here yet, by the round map in
`docs/architecture.md`: the live Baseten adapter (round 2 — a live
run needs a `BASETEN_API_KEY`), plan depth and staleness (round 3),
budgets and the priced cost ledger (round 6), the model promotion
gate (round 7), and the shortlist comparison (round 8). The Postgres
profile is code-complete behind the store port (SQLite is the local
default; `DATABASE_URL` switches) with live Postgres conformance
scheduled as the release round's gate, as in the sibling blueprints.
Docker files are provided and follow the house pattern; the image
build is part of hands-on testing.

## License

Apache 2.0 — see `LICENSE`.
