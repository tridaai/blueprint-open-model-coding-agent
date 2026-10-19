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
  validator (`validate_plan_draft`: the plan's steps are structured
  — intent, in-scope files, resolvable tests; see "Plans, precisely"
  below) — a live model's plan is refused by exactly the code that
  checks the scripted one's.
  The port also carries usage accounting: implementations note each
  call's usage and the service drains and stores it (below).
- **`plans.py` — plan arithmetic.** Pure computations the gate
  leans on: per-file SHA-256 digests of a plan's approved scope and
  the fingerprint over them, drift between two digest maps, and the
  diff between two plan versions (steps added, removed, changed;
  scope movement). No state, no I/O beyond the target files the
  service names.
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
  priced cost ledger spends. Each row is also stamped with the
  price-table version in force when it was recorded.
- **`tools.py` — the tool port.** The only hands the agent has,
  rooted at one run workspace. Every result is structured — `ok`,
  output, `refused`, a stable `code` naming a refusal, and a
  `detail` dict — and the service records each result verbatim on
  the run's audit trail, so a refusal or truncation is a stored
  record, not a log line:
  - `list_files` — the workspace's files, confined by path
    resolution.
  - `read_file` — one file, optionally a 1-based line range. Past
    the read ceiling (32 KiB) the head comes back with a truncation
    marker naming the file's total size; binary or undecodable
    files refuse as `binary_file` instead of returning garbage;
    bad ranges refuse as `invalid_line_range` /
    `line_range_out_of_bounds`.
  - `search` — regex over workspace files, matches with file, line,
    and a bounded context window. Case sensitivity and `glob`/`path`
    scoping are explicit parameters that cannot reach outside the
    workspace (`search_outside_workspace`, `search_glob_invalid`).
    When matches exceed the limit, the output ends with a
    truncation marker naming the true total — the cut is labelled,
    never silent.
  - `edit_file` — an *anchored* replacement, the only way code
    changes (there is no whole-file write and no append tool): the
    anchor must occur exactly once, and the file must be inside the
    plan's approved scope. Zero occurrences refuse as
    `anchor_not_found`, several as `anchor_ambiguous` — both are
    data the model can act on (re-read, narrow the anchor), not
    retries in the dark. A landed edit records the anchor and the
    file's before/after SHA-256 digests.
  - `run_tests` — the single allowed command shape: pytest over
    named, existing test files inside the workspace. There is no
    shell and no way to name another command. The child environment
    is scrubbed of every variable whose name carries a credential
    marker (KEY, TOKEN, SECRET, PASSWORD, AUTH, CREDENTIAL). With
    `only_affected`, it runs the **affected set** the shell
    computes in code from the plan's steps and the run's actual
    edits: a step naming an edited file contributes its tests
    (plan rule), and a workspace test file importing an edited
    module is affected (import rule). `paths` then names a subset
    of that set — the model can request the set, never define it.
    Verification still runs the full grading set on the pristine
    copy; the affected set is quick feedback, never the verdict.
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
exits. Two round-3 transitions bend the line without breaking it:
`approved → plan_ready` when a plan amendment lands (the new version
awaits its own decision), and `approved → draft` when execution
refuses a stale plan (a fresh plan must be drafted). Round 5 adds
the recovery states: `running → interrupted` when a run dies
mid-flight, `interrupted → running` on a resume that passes its
basis checks, and `interrupted/failed → abandoned` when a human
gives the run up. An approval is not consumed by a run that did not
finish — resume and restart both stand on the approval already
recorded. The transitions are the gates:

- `start_run` requires status `approved` *and* an approved plan
  record; anything else is `plan_not_approved`. Naming a superseded
  plan version is `plan_superseded`; naming one that does not exist
  is `unknown_plan_version`.
- `decide_review` requires status `succeeded`; accepting exports the
  patch from the run record to the artifacts directory.

## Plans, precisely

A plan is a stored, versioned artifact whose content is an
**ordered list of steps**. Each step names its intent in words, the
files it will touch, and the tests it expects to run. The plan's
flat `files`/`tests` lists are derived from the steps — the unions,
in step order — and are what the sandbox scope and verification
check against. Validation is one function for every draft, from
either model, and it checks structure, not vibes: at least one
step; every step has an intent and at least one file; every named
file is inside the work order's approved scope; and every named
test **resolves** — to a file that exists in the target repo, or to
the task's grading set (a gold task's grading file, or an ad-hoc
order's named grading tests). A draft that fails is `malformed_plan`.

**Approval binds the version to the world.** Deciding a plan
approved records its *scope fingerprint*: a hash over the task
text, the scope list, and a SHA-256 digest of every in-scope file
in the target repo, plus the digests themselves. Before a run
starts, the fingerprint is recomputed; any drift in an in-scope
file — edited or deleted — refuses the run as `stale_plan`, names
the drifted files on the audit trail (`stale_plan_refused`), and
returns the order to `draft` for a fresh plan. Files outside the
approved scope are never digested: they cannot stale a plan,
because the scope is what was approved.

**Amendments are new versions.** Amending an approved plan (only
possible before execution starts) writes version n+1 with a full
replacement step list and a stored diff against its predecessor —
steps added, removed, and changed, and how the derived scope
moved. The work order's scope is the amendment's ceiling: naming a
file outside it refuses as `scope_widening_refused`; narrowing is
fine. The new version starts undecided and the order returns to
`plan_ready`: **an approval recorded against v(n) never authorises
v(n+1)**. Execution always runs the latest approved version; a
re-draft after rejection is likewise a new version, never an
overwrite. Plan reads show every version with its steps, its
decision, and its fingerprint state — `unrecorded`, `current`, or
`stale`.

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

Since round 9 the grading run is also *isolated* from the tree it
grades: pytest runs with `--noconftest` and the repo's `addopts`
overridden, interpreter startup files (`sitecustomize.py`,
`usercustomize.py`) are set aside in the verify copy and named on
the verdict, and the verdict reads pytest's JUnit report — the
grading counts (tests, failures, errors, skipped) are stored on
the verdict as `grading_evidence`, and a pass requires every
grading test to have actually run and passed. An exit code of
zero over skipped or deselected tests is not a verdict.

## Failure recovery, precisely

**Checkpoints.** As a run works, the shell records an append-only
checkpoint after each landed edit, and another at each completed
plan step (a step is complete when every file it names has taken a
landed edit). A checkpoint names the run, the plan step it reached,
the per-file digests of the plan's in-scope files in the run's
workspace, the tool calls so far, and the usage totals so far. The
workspace is not copied per checkpoint — the digest is the record.

**Interruption is a state, not an accident.** A run that dies
mid-flight lands INTERRUPTED, with the reason and the last
checkpoint named on its record: a transient model failure after the
adapter's bounded retries (rate-limited, overloaded, server error,
unreachable) or a process-level exception in the loop. Terminal
model errors — a rejected key, an unknown slug, a malformed
exchange — still land FAILED, as they did in round 2: resuming into
the same wall would fail identically. And because runs execute
synchronously inside one process, a run record still standing at
`running` when a service boots belonged to a process that died
mid-run; startup reconciliation marks it INTERRUPTED with the
reason `process_lost`. No zombie rows, no silent failures.

**Resume is decided in code.** An interrupted run may resume only
on the basis it was interrupted on, checked in order: the plan
version the run started on is still the approved version; the
target repo still fingerprints as the world that plan was approved
against; and the run's workspace still digests to its last
checkpoint (or, before any checkpoint, to the pristine copy). Any
basis that moved refuses as `resume_basis_changed`, naming the
basis — `plan_superseded`, `target_moved`, `workspace_changed` —
and the refusal is recorded. A resume that passes continues in the
run's own workspace with its context rebuilt from the stored
tool-call records: completed steps are not re-executed and landed
edits are not re-applied.

**Restart** discards the old workspace (what it held is summarised
on the audit trail) and runs fresh, pinned to the same plan version,
through start_run's ordinary checks — staleness included.

**Abandon is a recorded act** — who, and why — valid from
interrupted, failed, or running (a running run is stopped first,
as an interruption). If edits had landed, the run's record names
the **partial patch**: the diff as far as it got, the files it
touches, and the checkpoint it reached. A partial patch is evidence
for the humans, never a submittable patch: the review gate takes
SUCCEEDED runs only, and an abandoned run is refused there by name
(`partial_patch_not_reviewable`).

## Budgets and the cost ledger (round 6)

**Budgets are ceilings in code, snapshotted per run.** `Policy`
carries the step ceiling (`CODINGAGENT_MAX_STEPS`, default 40), the
per-invocation test timeout (`CODINGAGENT_TEST_TIMEOUT`, default
120s), and — since round 6 — the priced budgets:
`CODINGAGENT_MAX_MODEL_CALLS` (200), `CODINGAGENT_MAX_TOKENS`
(2,000,000), and `CODINGAGENT_MAX_WALL_SECONDS` (1800). A run copies
the three onto its own record at start; from then on the snapshot
rules the run. The loop checks the budget **before every model
call** (at a ceiling, the call is not made) and **after every usage
record lands** (the call that just landed may itself have crossed
the token ceiling — the run stops there, so the overshoot is that
single in-flight call, never more). Tokens are the provider's
measured tokens only; the scripted model's character accounting is
not tokens and can never trip the token ceiling. Wall seconds are
the run's *active* seconds, accumulated across attempts — time an
interrupted run spends waiting on a human is not its wall clock.

Crossing a ceiling stops the run **INTERRUPTED** with the ceiling
named on the record (`budget_stop`) and on the audit trail
(`budget_exhausted`, with ceiling, limit, and spend). The work is
not wrong; the spend is done. A budget-stopped run **cannot
resume** — resume spends against the same run's accumulated usage,
and that budget is already spent, so resume refuses by the budget's
own name (`budget_exhausted`). Its way back is **restart**, which
snapshots a fresh budget from the policy in force. A resumed
(non-budget) run likewise keeps its original snapshot and its
accumulated usage: recovery never refreshes a budget.

**The price table is governed data.** `prices/table-v<N>.json`
records, per model slug, an input and output price per million
tokens as decimal strings, with a currency (USD), an `as_of` date,
and a source note. The loader is fail-closed in the profiles'
mould: unknown fields, a float price, a half-priced slug, or a
version that disagrees with its file name each refuse by name at
startup. Changing a price publishes a **new version** alongside the
old; versions are never edited in place. Every usage row is
stamped, at record time, with the version in force (migration
0004), and every fold prices each row under its own version — a
historical run keeps the prices it was costed under even after the
table moves on. The shipped v1 records the shortlist's five slugs
with **null prices**: numbers this repo has not verified are not
invented, and a null price folds as unpriced.

**The ledger.** All money is `Decimal`; no float touches it. A run
stores its cost fold at every landing: `total`, split into `plan`
(the drafting of its work order's plan — the order's plan-phase
usage rows; an order's runs share the one drafting) and `execution`
(the run's own rows). A row enters the money only when it is
provider accounting (source `api`, unit `tokens`) priced under its
stamped version for its slug. Everything else is **counted, with
its reason named, and kept out of the money**: `no_price_recorded`,
`price_table_version_unrecorded`,
`price_table_version_missing` — and `scripted_accounting`, the
offline model's characters, which are never priced as tokens and
never estimated into tokens. The suite fold (`cost_report`, served
at `GET /api/costs` and rendered in the console) groups runs and
usage by profile: runs, verified successes, priced spend, and
**cost per successful task** — ALL of the profile's priced spend,
failures and interruptions included, divided by its verified
SUCCEEDED runs. That is the webinar's headline metric, and it is
honest only with the failures in the numerator.

## The promotion gate (round 7)

**The benchmark is the team's own work.** The gold suite splits
into two named sets (`benchmarks.py`): **tuning** (the four
single-file repairs) and **held-out** (the boundary fix and the
two-file feature). The split is fixed data and must partition the
suite — every task in exactly one set — or benchmarking refuses.
Tuning is where a profile's behaviour may be studied and adjusted;
held-out is where promotion is judged, and nothing is tuned
against it. Each set is named by a **content hash**: SHA-256 over a
canonical manifest of its tasks (specs, broken and fixed overlays,
grading tests). Add a task, edit a defect, touch a grading test —
the hash moves, and every benchmark or evaluation recorded under
the old hash is stale by construction. Re-running the benchmark
is the only way back; no edit makes an old result current.

**A benchmark is the real path, recorded.** `run_benchmark` builds
a sibling service over the same store, pinned to the candidate
profile, and runs each task as a gold work order end to end —
draft, plan approval by the benchmark actor, sandboxed run,
verification in code. The record stores per-task outcomes
(verified success, or the failure class: `run_failed`,
`run_interrupted`, a gate refusal by name), usage, priced cost,
and the set hash. Two failure classes are **zero-tolerance** — an
edit landing outside the approved scope, a run standing SUCCEEDED
without a passing verification. The tool port and verification
make both structurally unreachable (scope is enforced at the edit;
SUCCEEDED is only ever the verification's verdict), and the gate
checks the stored records anyway: it trusts records, not arguments
about why they must be fine.

**Worst-of-N decides.** A promotion evaluation runs the held-out
set `DEFAULT_EVALUATION_RUNS` (3) times and folds the runs into one
stored record: the score is the **worst** run's verified successes,
and any zero-tolerance violation in any run fails the evaluation
outright, whatever the counts say. One good run is weather; the
worst of three is a claim a deployment can stand behind. The
latest evaluation is the one a promotion judges by — a newer one
supersedes an older one by being later, so results cannot be
shopped.

**Promotion is a judged act.** `promote(profile, actor,
rationale)` moves the default only when, in order: a held-out
evaluation for the candidate **exists** (`no_passing_evaluation`),
is **current** (`evaluation_stale` — its hash is the set's hash
now), and **passed** (`no_passing_evaluation` — a violation in any
run is disqualifying); the actor is **not the evaluation's actor**,
compared by normalised name (`self_promotion`); and the
candidate's worst run does **not regress** against the incumbent
default's own recorded baseline — its latest current-hash held-out
evaluation (`regression`). The incumbent's baseline is measured
the same way the candidate's is: by running it. Before the first
promotion, a deployment records the seeded default's baseline with
the same `evaluate_profile` act; the gate refuses to judge a
candidate against a bar nobody measured. The bar is **no
regression, not must-beat**: an equal candidate promotes — the
byte-identical-copy control in the tests and the eval pack proves
it — because a profile is earned on evidence, not on beating a
teammate by a point.

**The default is a stored record.** Promotion and rollback are
append-only events (migration 0005); the current default is the
latest event's target, and the configured profile (`MODEL_PROFILE`)
is only the seed a deployment starts with. A primary service
serves the governed default — plans, runs, and usage rows are
stamped with it — resolving a promoted profile through the same
rules as the configured one (its record, the deployment's
validated overrides, the live key check). **Rollback** restores
the previous default as its own event: it needs no evaluation,
because it returns to a state that was itself promoted on
evidence, and it is refused only when no history exists
(`no_promotion_history`). The surfaces follow the records:
`/readiness` names the default, `GET /api/promotions` returns the
whole position (sets and current hashes, evaluations, history),
and the console renders them beside the cost fold — the two halves
of the webinar's discipline side by side: what the work costs per
successful task, and what a model must prove before it does the
work by default.

## The shortlist comparison (round 8)

**A comparison is a record, produced the governed way.** Producing
one (`run_comparison`, `POST /api/comparisons`) runs **every
recorded profile** — the profiles directory as it stands: the five
Baseten family profiles and the offline profile in the shipped
set — once over the **same** benchmark set, through the round-7
benchmark path (`run_benchmark`: gold work orders, plan gate,
sandboxed runs, verification in code), all under one comparison
actor. The default set is **tuning** — the breadth set; the
held-out set may be named instead, and the record states which set
it used and that set's content hash. The request names the set and
the actor, never the shortlist: a comparison that covered only
the profiles its requester liked would not be a comparison.

**A profile that cannot serve is recorded, not skipped.** When a
profile's benchmark cannot even start — a live profile whose key
is not set in this environment, an override outside its allowlist
— the refusal (`profile_not_servable`, with its reason: the key
env it needs, in the refusal's own words) is recorded on the
comparison as a `not_servable` entry with no benchmark behind it.
Nothing is silently dropped, and nothing is faked: a not-servable
profile contributes no successes, no usage, and no cost — it
contributes the fact that it could not be measured here, and why.
In an environment with no Baseten key, a comparison over the
shipped shortlist therefore benchmarks the offline profile and
records all five live profiles not-servable; with a key and a
transport, the live profiles benchmark like any other.

**The report is a pure fold.** `GET /api/comparisons` and
`GET /api/comparisons/{id}` fold the stored records — the
comparison, the benchmark records it names, the usage rows those
benchmarks' work orders recorded, the price tables, and the
promotion records — into the report, per profile:

- the **per-task matrix**: for each task in the set, verified
  successes over attempts (one benchmark per profile in a
  comparison, so one attempt; the fold is written for the general
  shape, and `worst_successes` / `mean_successes` are computed
  over however many benchmark records the comparison names);
- **total usage** — calls, tokens, latency — folded from the same
  usage rows the money came from, plan drafting included;
- **priced cost and cost per successful task**, folded exactly as
  the round-6 ledger folds (each row under its stamped table
  version, all spend over verified successes) — stated **only**
  where the fold priced the profile's slug. An unpriced fold is
  labelled with its reasons (`no_price_recorded`, …) and a
  scripted fold is labelled `scripted`; in both cases cost per
  successful task is *not stated* (null), never a zero presented
  as a price;
- **failure classes**, counted from the benchmark outcomes, and
  the zero-tolerance violation count;
- the profile's **promotion standing**, read from the gate's own
  records at read time: the state of its latest held-out
  evaluation (`none` / `passed` / `failed` / `stale` — stale when
  that evaluation's hash is no longer the held-out set's hash)
  and whether it is the incumbent default right now.

Recomputing the report changes nothing — it writes no record and
moves no default. A comparison whose set has since moved (a task
edited after it was recorded) is marked `stale` on the report,
with both hashes shown; the stored record is never rewritten.

**No ranking theatre.** The report carries no ordering and no
verdict: profiles appear in the shortlist's recorded order, the
matrix shows what each one did, and that is all. The comparison
record states on its face that it is evidence only and that the
promotion gate is the only path that changes the default — a
profile with the cleanest row in the matrix still becomes the
default only by passing a current held-out evaluation, judged by
a second actor, with no regression against the incumbent's
recorded baseline. The console renders the latest comparison as
that matrix beside the cost fold and the gate, with the same
note attached.

## The injection red team (round 9)

**Repo content is data.** The agent reads the codebase it works
on — comments, READMEs, docstrings, file names, test output — and
every one of those is a channel an attacker who can plant a file
in the target repo can write into. Round 9 attacks every channel
with a model that has *already decided to obey* the injection,
because a defence that works only when the model ignores planted
text is not a defence. The full report is `docs/red-team.md`; the
pack runs as `tests/test_red_team.py` and as a leg of the eval
gate, graded zero-tolerance.

Four of the six groups held by construction, and the pack proves
each: instructions in repo content die at the tool port (scope,
the one pytest shape, path resolution) whatever the model asks
for; task text cannot widen a scope, skip the plan gate, or
approve itself, because the gates are service state, not phrases;
tool results claiming a verdict change nothing, because the
verdict is computed on the pristine copy and a claim is not a
patch; and a live model's plan draft passes the same validator as
the scripted model's, refusing `malformed_plan` before anything
is stored.

Two groups landed, and both were fixed at the layer that owns
the defect:

- **Grading poisoning.** Verification took pytest's exit code as
  its verdict, and a target-carried `conftest.py` could make that
  code 0 without any grading test passing — by skipping the whole
  set, or by patching the module under test before the grading
  tests imported it; a root `sitecustomize.py` did the same
  through interpreter startup. A grading run is now an *isolated*
  run: `--noconftest`, the repo's `addopts` overridden away,
  startup files set aside in the throwaway verify copy and named
  on the verdict, and the verdict reads the JUnit **evidence** —
  every grading test must actually run and pass (skipped = 0);
  anything less reports `grading_not_fully_run`, not a pass.
- **Symlink dereference.** Named access resolved links and
  refused escapes, but enumeration did not: `search` read a
  planted symlink's outside target into results the model sees,
  `build_patch` read targets into diffs, and the service's tree
  copies dereferenced links into the workspace and the pristine
  snapshot. Symlinks are now followed nowhere: enumeration and
  patch building skip them, and copies drop them at the copy
  (`_copy_ignore`).

What this round deliberately did not do: verification depth
beyond proving the grading tests run (test-gaming is round 10),
the challenge pass (round 13), and any change to promotion or
comparison semantics.

## The round map

R1 core · R2 Baseten live path + profiles v1 · R3 plan depth +
staleness · R4 tool depth · R5 failure recovery · R6 budgets
+ cost ledger · R7 model promotion gate · R8 shortlist comparison ·
R9 injection red team (this round) · R10 verification depth · R11 review
workbench · R12 task library · R13 challenge pass · R14 batch work
orders · R15 observability · R16 deployment profiles · R17 MCP
surface · R18 dual control · R19 skills library · R20 release
hardening (full gate re-run, live Postgres conformance, hosted boot,
prod smoke).

## Honest limits (round 9)

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
- A resumed live-model conversation is rebuilt from the stored
  tool-call records — a faithful rendering of what was called and
  how it came back — not a replay of the earlier attempt's wire
  history, which the audit records deliberately summarise rather
  than keep verbatim. The workspace, not the transcript, is the
  ground truth a resume is checked against.
- The promotion gate's evidence is only as strong as the held-out
  set: two tasks, chosen as the suite's harder shapes. That is a
  real benchmark and a small one — a deployment adopting this
  shell grows the suite from its own merged work, which is the
  webinar's method and the reason the set is data with a hash
  rather than a number in code. Benchmarks are real runs: a
  worst-of-3 held-out evaluation is six agent runs, each verified
  by shelling out to pytest, and the gate's tests and eval leg pay
  that cost openly rather than mocking the runs they judge.
- A comparison is as expensive as the benchmarks it runs: one
  benchmark per servable profile over the named set, each task a
  real verified run. The shipped shortlist in a keyless
  environment benchmarks only the offline profile (the five live
  profiles record not-servable), but a deployment with a key pays
  a full benchmark per live profile per comparison — that is the
  honest price of evidence over assertion, and the reason the
  comparison is a recorded act rather than a dashboard refresh.
- The red team's grading isolation (round 9) proves the grading
  tests *run* — unweakened collection, no repo conftest, evidence
  counts on the verdict. It does not prove the tests are strong:
  for an ad-hoc order the grading set is the operator's own tests
  in the target repo, and a grading test that asserts nothing is
  a test the operator chose. Test-gaming depth — a patch that
  games tests which genuinely run — is round 10's subject. And
  the pack attacks the shell with hostile scripted steps, not a
  live model: whether a real model follows planted repo text is
  that model's property, and no defence here depends on it.
