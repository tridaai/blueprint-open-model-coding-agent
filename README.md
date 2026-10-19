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
  `https://inference.baseten.co/v1`, model slugs in `org/name` form.
  The live adapter (round 2) speaks tool calling over that
  OpenAI-compatible API behind the same model port the offline model
  uses; the agent loop cannot tell which backend served it. Pick
  the model by profile: `baseten-glm` (`zai-org/GLM-5`),
  `baseten-kimi` (`moonshotai/Kimi-K2.5`), `baseten-deepseek`
  (`deepseek-ai/DeepSeek-V3.1`), `baseten-qwen`
  (`Qwen/Qwen3-Coder-480B-A35B-Instruct`), `baseten-gpt-oss`
  (`openai/gpt-oss-120b`). Every model call's usage — tokens and
  latency, from the provider's usage object — is recorded against
  its run; that accounting is what the cost ledger prices.
- **Profiles are governed data** — versioned JSON records in
  `profiles/`, loaded fail-closed: an unknown profile name, an
  unknown field in a record, or a slug outside a profile's own
  allowlist refuses by name. Selecting a live profile without
  `BASETEN_API_KEY` set refuses at startup — never a silent fall
  back to the offline stand-in. `BASETEN_MODEL` may pin a slug only
  inside the selected profile's allowlist.
- **Offline (the default)** — `MODEL_PROFILE=offline`: a labelled
  scripted model drives the same shell over the gold suite. That is
  what the tests and the demo run; no key is needed and no model is
  called. Its usage rows are its own accounting, labelled
  `scripted`, never presented as API usage.
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
2. **Plan gate.** The model drafts a plan — an approach and the work
   as an ordered list of steps, each with its intent, its files, and
   the tests it expects to run. A human approves or rejects it as a
   recorded act. Execution from an unapproved plan is refused in
   code (`plan_not_approved`). Approval also records a **scope
   fingerprint** — a hash over the task and the contents of every
   in-scope file — and execution recomputes it first: if the repo
   moved inside the approved scope, the run refuses as
   `stale_plan` and a fresh plan is drafted against the repo as it
   stands. An approved plan can be **amended** before it runs: the
   amendment is a new version with a recorded diff, it cannot widen
   past the work order's scope, and it needs its own approval — the
   old one does not carry.
3. **Sandboxed execution.** Each run copies the target into an
   isolated workspace. The model's tools are list / read / search /
   anchored edit / run-tests, behind a port that enforces the scope
   and the command allowlist in code: edits outside the scope are
   refused and recorded; the only command shape is pytest over named
   test files; the child environment is scrubbed of every
   credential-looking variable. The tools have depth, not just
   guards: an edit's anchor must occur exactly once — a missing or
   ambiguous anchor refuses by name (`anchor_not_found`,
   `anchor_ambiguous`) as data the model can act on, and a landed
   edit records its anchor and the file's before/after digests;
   reads take line ranges and label any truncation with the file's
   true size; search results carry context and label their own
   truncation with the true match total; and the tests a run's
   edits affect are computed by the shell from the plan's steps and
   the actual edits — the model can ask for that set, never define
   it. Every tool result, refusals included, is recorded verbatim
   on the run's audit trail.
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
7. **Budgets and the cost ledger (round 6).** A run spends against
   ceilings enforced in the shell — model calls, provider-measured
   tokens, active wall-clock seconds — snapshotted onto the run at
   start from `Policy` (`CODINGAGENT_MAX_MODEL_CALLS`,
   `CODINGAGENT_MAX_TOKENS`, `CODINGAGENT_MAX_WALL_SECONDS`). The
   loop checks the budget before every model call and after every
   usage record lands; crossing a ceiling stops the run
   INTERRUPTED with the ceiling named, and a budget-stopped run
   cannot resume — restart, into a fresh budget, is its way back.
   Every run's usage is priced by the **governed price table**
   (`prices/table-v<N>.json`: versioned records, loaded
   fail-closed, Decimal throughout) and stored on the run, split
   plan vs execution — each usage row priced under the table
   version stamped on it when it was recorded. The shipped table
   records the shortlist's slugs with **null prices**: numbers this
   repo has not verified are not invented, and an unpriced slug's
   tokens are counted with the reason named, never zeroed. The
   console folds all of it per profile into **cost per successful
   task** — all of a profile's priced spend, failures included,
   over its verified successes. The offline model's accounting is
   characters, labelled `scripted` — never priced as tokens.

## The gold suite (v1)

Six tasks, gold-by-construction: each ships a broken state of the
workshop codebase and grading tests that fail on it and pass on the
reference fix.

| Task | Shape | Scope |
| --- | --- | --- |
| Reservations can overcommit stock | bug fix | `workshop/inventory.py` |
| Tax is charged on the pre-discount subtotal | bug fix | `workshop/pricing.py` |
| The low-stock report always comes back empty | missing feature | `workshop/inventory.py` |
| Cancelling an order never releases its stock | bug fix | `workshop/orders.py` |
| Ten-unit orders miss the bulk discount | boundary fix | `workshop/pricing.py` |
| Orders cannot take a coupon | feature, two files | `workshop/pricing.py`, `workshop/orders.py` |

`make evals` grades all six end to end through the service, plus
the construction proof, a gate check per task, and legs for the
live adapter's code path, plan staleness, and plan amendments.
Zero-tolerance counters: edits landed outside scope, and successes
without a passing verification.

## Layout

```
src/open_model_coding_agent/  the governance shell (service, gates,
                              sandbox tools, patch + verification,
                              model port, Baseten adapter, store,
                              API, console)
profiles/                     the model profiles, as versioned records
prices/                       the governed price table, as versioned records
workshop/                     the sample codebase the agent works on
evals/gold/                   the gold task suite (broken/fixed/grading)
evals/run_evals.py            the eval gate
migrations/                   numbered SQL migrations, both dialects
tests/                        the shell's own test suite
docs/architecture.md          the design, in full
```

## Honest status

Rounds 1–6 are built and gated: **101 tests passing, evals PASS**
(6/6 gold tasks verified end to end on the offline model, plus one
gold task end to end through the Baseten adapter's code path, a
recovery leg where an ambiguous first edit refuses by name and the
run still lands its repair, an interruption leg where a run
that dies after its first edit resumes to a verified success
without re-applying it, and a costing leg that prices a live-path
run plan-vs-execution and folds cost per successful task;
counters at zero). Runs now record
checkpoints as they work, and a run that dies mid-flight lands
INTERRUPTED — resumable when its basis (plan, target, workspace)
checks out in code, restartable under the same approval, or
abandonable as a recorded act that names its partial patch, which
the review gate can never accept. Runs also spend against enforced
budgets (calls, tokens, active wall seconds — snapshotted at
start; a budget-stopped run cannot resume, only restart into a
fresh budget) and every run carries its priced cost from the
governed price table. The shipped table's prices are null —
unverified numbers are recorded as unverified, so live runs fold
as unpriced (counted, reasoned, never zero) until a deployment
publishes a table version with prices it verified. The live adapter is exercised in tests and evals
through a fake transport replaying recorded-shape responses — it
has **not yet been run against the real Baseten endpoint**, which
needs a `BASETEN_API_KEY`; the first live smoke is a deployment
step, and this status will say so until it has happened. What is
deliberately not here yet, by the round map in
`docs/architecture.md`: the model promotion gate
(round 7), and the shortlist comparison (round 8). The Postgres
profile is code-complete behind the store port (SQLite is the local
default; `DATABASE_URL` switches) with live Postgres conformance
scheduled as the release round's gate, as in the sibling blueprints.
Docker files are provided and follow the house pattern; the image
build is part of hands-on testing.

## License

Apache 2.0 — see `LICENSE`.
