# Architecture — Open-Model Coding Agent

The design in full: what each part does, which decisions are enforced
in code rather than prompted, and where the rounds go from here.

## The shape

```
            ┌────────────┐   plan    ┌──────────────┐
 work order │ plan gate  │ ────────► │  human       │
 ─────────► │ (service)  │ ◄──────── │  approve /   │
            └────────────┘  decision │  reject      │
                  │ approved         └──────────────┘
                  ▼
            ┌────────────┐  steps   ┌──────────────┐
            │ agent loop │ ───────► │ model port   │
            │            │ ◄─────── │ offline (R1) │
            └────────────┘  actions │ Baseten (R2) │
                  │ tool calls      └──────────────┘
                  ▼
            ┌────────────┐
            │ tool port  │  scope + allowlist enforced here, in code
            │ (sandbox)  │  on an isolated copy of the target repo
            └────────────┘
                  │ patch
                  ▼
            ┌────────────┐  verdict  ┌──────────────┐
            │ verify in  │ ────────► │ review gate  │ ─► .patch export
            │ code       │           │ (human)      │
            └────────────┘           └──────────────┘
        fresh copy + grading tests        every act an audit record
```

## Components

- **`service.py` — the governance shell.** Work orders, the plan
  gate, the run loop, verification, and the review gate. The model
  proposes; the service disposes. All state transitions go through
  it, so the API and the console can never bypass a gate.
- **`model_port.py` — the model seam.** A model is asked for a plan
  draft, then for one step at a time. The labelled offline scripted
  model (the default for tests and the demo) and the live Baseten
  adapter implement the same port; nothing in the shell knows which
  model is serving. Every draft, from either, passes the port's one
  validator (`validate_plan_draft`: approach present, files named
  and inside the work order's approved scope) — a live model's plan
  is refused by exactly the code that checks the scripted one's.
  The port also carries usage accounting: implementations note each
  call's usage and the service drains and stores it (below).
- **`baseten.py` — the live adapter.** OpenAI Chat Completions over
  the profile's endpoint, the key sent as the OpenAI SDK sends it.
  It translates both ways: the shell's tool schemas become function
  tools on the wire; tool calls come back as port steps (a batched
  response queues its steps); tool results fold back as tool
  messages. Plan drafting runs in JSON mode and parses through the
  same refusal (`malformed_plan`) as any bad draft. HTTP goes
  through a transport seam — a small urllib transport in production
  (no new dependency), a fake in tests — and failures are named and
  bounded: 401 `model_auth_failed`, 404 `model_unknown_slug`,
  429/529/5xx retried a bounded number of times with backoff, then
  `model_rate_limited` / `model_overloaded` / `model_server_error`.
  Inside a run, such a failure ends the run FAILED with the reason
  recorded; the shell never hangs or silently retries.
- **`profiles.py` + `profiles/` — the model profiles, as data.**
  One versioned JSON record per servable configuration, loaded
  fail-closed: unknown profile name, unknown field, a slug outside
  the profile's own allowlist (its recorded models or prefixes, in
  org/name shape), or an offline record carrying live fields each
  refuse by name. Settings resolve the named profile at startup;
  a live profile without its key env refuses there too, and the
  `BASETEN_MODEL` / `BASETEN_BASE_URL` overrides are validated
  against the selected profile's record — an override can pin, it
  cannot widen.
- **Usage records (`model_usage`, migration 0002).** One row per
  model call: the work order, the run (NULL for plan drafting), the
  profile and slug that served, the counts, and the latency. Live
  rows carry the provider's usage object (`source: api`, unit
  tokens); the offline model's rows carry its own accounting
  (`source: scripted`, unit chars) so the two are never confused.
  Run reads fold their rows in, with totals — the raw material the
  priced cost ledger (round 6) spends.
- **`tools.py` — the tool port.** The only hands the agent has,
  rooted at one run workspace:
  - `list_files`, `read_file`, `search` — reads, confined to the
    workspace by path resolution.
  - `edit_file` — an *anchored* replacement: the old text must occur
    exactly once, and the file must be inside the plan's approved
    scope. Refusals are returned as data and recorded.
  - `run_tests` — the single allowed command shape: pytest over
    named, existing test files inside the workspace. There is no
    shell and no way to name another command. The child environment
    is scrubbed of every variable whose name carries a credential
    marker (KEY, TOKEN, SECRET, PASSWORD, AUTH, CREDENTIAL).
- **`patch.py` — patches with proof.** A run's patch is a unified
  diff of workspace vs the pristine snapshot. `apply_patch` replays
  it with every hunk's context verified; a mismatch raises rather
  than half-applying.
- **`gold.py` + `evals/gold/` — the benchmark, as data.** Each gold
  task carries a broken overlay, a fixed overlay, grading tests, and
  the scripted edits between them. Construction is proved in code
  (anchors unique; edits reproduce the fix byte for byte). Grading
  tests are staged only into the verification copy — a run workspace
  never contains them, and the reference fix reaches a run only as
  the scripted model's edits.
- **`store.py` — one port, two adapters.** SQLite local default at
  `./data/codingagent.db`; `DATABASE_URL` switches to Postgres.
  Payloads are JSON in TEXT columns in *both* dialects, so a payload
  read has the same wire shape on either store — a lesson the
  sibling blueprints paid for (a JSONB-shaped read crashing a store
  that had only ever run on SQLite). Migrations are numbered SQL in
  `migrations/` and `migrations/sqlite/`, applied at startup.
- **`api.py` + the console.** FastAPI: `/health`, `/readiness`
  (names the store and the model profile), the work-order API, and a
  single-file console. Gate refusals surface as 409 with the
  refusal's stable code. Structured JSON logs carry a request id.

## State machines

Work order: `draft → plan_ready → approved → running → succeeded →
accepted`, with `plan_rejected` (re-draftable), `failed` (the run's
verification did not pass), and `rejected` (at review) as the other
exits. The transitions are the gates:

- `start_run` requires status `approved` *and* an approved plan
  record; anything else is `plan_not_approved`.
- `decide_review` requires status `succeeded`; accepting exports the
  patch from the run record to the artifacts directory.

## Verification, precisely

A run is SUCCEEDED only when all of these hold, computed by the
shell on a fresh copy of the target (the run's pristine snapshot):

1. the run produced a patch (at least one changed file);
2. every changed file is inside the plan's approved scope;
3. the patch applies cleanly (context-verified) to the fresh copy;
4. the task's grading tests pass in that copy.

The agent's in-run test runs are part of its transcript, never of
the verdict. A wrong-but-plausible repair therefore lands FAILED
with the grading failure named — there is a test that proves exactly
this.

## Budgets (round 1 form)

`Policy` carries the run's step ceiling (`CODINGAGENT_MAX_STEPS`,
default 40) and the per-invocation test timeout
(`CODINGAGENT_TEST_TIMEOUT`, default 120s). Priced budgets — token
ceilings, the governed price table, cost per successful task — land
in round 6; the promotion gate that spends them wisely lands in
round 7.

## The round map

R1 core (this round) · R2 Baseten live path + profiles v1 · R3 plan
depth + staleness · R4 tool depth · R5 failure recovery · R6 budgets
+ cost ledger · R7 model promotion gate · R8 shortlist comparison ·
R9 injection red team · R10 verification depth · R11 review
workbench · R12 task library · R13 challenge pass · R14 batch work
orders · R15 observability · R16 deployment profiles · R17 MCP
surface · R18 dual control · R19 skills library · R20 release
hardening (full gate re-run, live Postgres conformance, hosted boot,
prod smoke).

## Honest limits (round 2)

- The live Baseten adapter is built to Baseten's documented
  OpenAI-compatible shape and is exercised end to end in tests and
  evals through a fake transport replaying recorded-shape
  responses. It has not yet been run against the real endpoint —
  that needs a `BASETEN_API_KEY` and is a deployment step; nothing
  here claims otherwise.
- The offline model is a scripted stand-in: it solves gold tasks from
  the suite's scripted edits and does not pretend to solve ad-hoc
  work (it inspects, runs tests, and finishes without edits).
- The sandbox confines by construction — isolated copy, path
  resolution, allowlisted invocations, scrubbed environment — not by
  OS-level isolation; the test child runs the repo's own pytest.
  Network is never invoked by any tool; there is no tool that can
  reach it.
- The Postgres adapter is code-complete and dialect-reviewed; its
  live conformance run is the release round's gate (R20), following
  the sibling blueprints' discipline of never claiming a profile
  that has not run.
