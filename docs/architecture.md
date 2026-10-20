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
  same refusal (`malformed_plan`) as any bad draft. Every request
  carries the serving profile's recorded `max_tokens` ceiling —
  the shortlist's models reason before they answer, and a reply
  that hits the ceiling with nothing usable (finish_reason
  `length`, empty content, no tool calls) fails as
  `model_output_truncated`, with the spent call's usage recorded
  first — never as an empty plan the shell misreads. HTTP goes
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
  cannot widen. A live record also governs the per-request
  completion ceiling (`max_tokens`, default 8192) and whether the
  provider's catalog currently serves its model at all
  (`servable`): a profile the catalog does not list is recorded
  `servable: false` with the reason and read date on the record,
  and every serving path — settings, profile resolution, the model
  factory, and through them benchmarks and comparisons (which
  record it `not_servable`, with the reason) — refuses it by that
  reason. The shipped records were refreshed against Baseten's
  live Model APIs catalog on 2026-10-19: the allowlists are
  exactly the verified slugs, and the `baseten-qwen` profile
  stands recorded not servable — the catalog lists no Qwen model.
- **`taxonomy.py` + `taxonomy/` — the failure taxonomy, as data
  (round 15).** Every named failure in the codebase — a gate
  refusal, a tool-port code, a failure class on a stored record —
  classified once to a class, a likely cause, and the operator's
  next action, in a versioned JSON record loaded fail-closed at
  startup. Classification of an unknown name returns the
  unclassified standing rather than refusing, so records written
  by a newer build count visibly instead of vanishing; a
  conformance test derives every raisable name from the code and
  holds the shipped table to covering them all.
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
  the scripted model's edits. Since round 10 each task also carries
  a hidden grading file (`evals/gold/hidden/`), staged only at
  re-grade time; construction checks it exists, is named for the
  hidden set, and is not the visible file restated.
- **`verification.py` — verification depth, as pure checks.**
  Test-surface classification by the repo's layout rules, the
  plan-steps comparison behind `unplanned_files`, and the planned
  file list — no state, no I/O; the service's `_verify` applies
  the policy (gold fails by name; ad-hoc flags) and
  `patch.patch_shape` supplies the hygiene facts.
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
exits. Round 11 adds `changes_requested`: a review decision that
sends a succeeded run back — from `succeeded`, or from `rejected`
when a reviewer reverses a rejection into a send-back — to a state
a fresh plan version drafts from, carrying the reviewer's note on
the order (see "The review workbench"). Two round-3 transitions bend the line without breaking it:
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

Since round 10 the verdict also carries the patch's depth facts,
computed before any grading runs (the pure checks live in
`verification.py`, the shape in `patch.patch_shape`): the patch's
shape (files changed, lines added/removed per file, binary files,
empty, whitespace-only); `test_surface_touched` with the files
named — a changed file that is, or shadows, a test/grading path
by the repo's layout rules (test modules, test/grading directory
trees, `conftest`/startup/pytest-config files); and
`unplanned_files` — changed files no step of the approved plan
named, the landed patch compared against the plan's steps rather
than only its derived scope. Policy: for a gold task a
test-surface or unplanned patch fails verification by name (the
fix never lives in tests; the steps cover exactly the scope);
for an ad-hoc order both are recorded and named, and the
test-surface flag is surfaced in red at the review gate, without
failing a run an operator may have legitimately scoped that way.
A whitespace-only patch fails as `no_substantive_change` — the
empty patch keeps its own honest name. And a gold task's visible
pass is only provisional: the shell then stages the task's
*hidden* grading file — kept in the eval area, present in no
workspace, staged only at that moment into the same patched
pristine copy — and re-grades; a hidden failure flips the
verdict to FAILED as `hidden_grading_failed`, with the record
keeping `visible_verdict` and `hidden_verdict` distinct. See
`docs/verification-depth.md`.

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
invented, and a null price folds as unpriced. Table v2 is the
verified publication: five slugs (DeepSeek V4 Flash 0731, DeepSeek
V4 Pro 0813, GLM-5.2, Kimi-K3, gpt-oss-120b) priced exactly as the
live catalog read of 2026-10-19 stated them, with the read named
in the record's source. V1 stays on disk untouched; rows recorded
under it keep folding under it — still unpriced, as they were.

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

## Verification depth (round 10)

Round 10 assumes the grading tests run honestly (round 9) and
asks whether the *patch* is honest. The full report is
`docs/verification-depth.md`; the four defences, all computed at
verification and stored on the verdict record:

- **Test-editing defence.** A patch touching the test/grading
  surface (by the repo's layout rules: test modules, files under
  test/grading trees, `conftest`/startup/pytest-config files) is
  flagged `test_surface_touched` with the files named. On a gold
  task it fails verification by name; on an ad-hoc order the flag
  is recorded, carried on the audit record, and shown in red at
  the review gate. Through the ordinary run path the tool port
  already makes such a gold patch impossible — the scope is the
  code files — so the check is defence in depth, proved at
  `_verify` with crafted plans that put the surface in scope.
- **Hidden-test re-grade.** Each gold task gains a hidden grading
  file in the eval area — edge cases of the same fix, on inputs
  the visible set never uses, failing on the broken state and
  passing on the reference fix by construction. It is staged
  only at re-grade time, into the same patched pristine copy the
  visible set just passed on, and never exists in a workspace. A
  hidden failure flips the verdict: `hidden_grading_failed`. The
  record keeps the visible and hidden verdicts distinct, and the
  benchmark content hash now covers the hidden sets, because a
  verdict depends on them.
- **Scope-creep detection.** The landed patch is compared against
  the approved plan's *steps*: files no step named are
  `unplanned_files`, stored on every record. Gold patches with
  unplanned files fail by name. Because the validator derives a
  plan's scope from its steps, the comparison is a stored proof
  on every run and a live check against any record whose steps
  and scope ever disagree — file-level only this round.
- **Patch hygiene.** `patch_shape` records files changed, lines
  added/removed per file, binary files, empty, and
  whitespace-only. An empty patch still fails as "the run
  produced no patch"; a whitespace-only patch fails earlier and
  by its own name, `no_substantive_change`.

## The review workbench (round 11)

**The queue is a derivation, not a table.** `review_queue` folds
the stored records on read into the reviewers' queue: every order
standing where a person must act — `plan_ready` (a plan awaiting
its decision), `succeeded` (a verified run awaiting review, with
the verification's risk flags riding the entry), `failed` (with
the failure class read off the run record: hidden re-grade,
test surface, unplanned files, budget, plain verification
failure), and `interrupted` (with its interruption named). Each
entry names the order, its state, the **single next act** that
moves it (`decide_plan`, `review`, `restart_run`, `resume_run`),
the acts open beside it where recovery offers more than one, and
its **age** — the time since the order's record last changed,
which is when the act became available. Longest-waiting first.
Nothing is cached and nothing is written: an order enters and
leaves the queue exactly when its records say so. And there is
**no bulk decision** — the route table is the proof: every
decision route is a POST on one work order, and the queue itself
is a read. A decision is one act, on one order, by one named
actor, or it is not a decision this shell records.

**The packet is the decision's whole evidence, in one read.**
`review_packet` assembles, for one order: the task text and its
scope; the approved plan the latest run ran on (its version,
steps, and fingerprint state — not merely the latest plan, which
an amendment may have moved past the run); the patch and its
round-10 shape; the verification record with the risk flags
lifted out and named distinctly — `test_surface_touched` with
its files, `unplanned_files`, the visible verdict, and the
hidden verdict with an explicit `hidden_verdict_absent` flag for
an ad-hoc order, which has no hidden set (absent says absent; it
never reads as a pass); the run's usage rows and priced cost;
the order's full run history (each attempt's status, plan
version, failure or interruption); and its prior review
decisions. A reviewer decides from the packet without opening
another view — that is the packet's contract, and the console
renders it with the risk flags visually distinct. Once a decision
stands on an earlier run, the packet also carries
`changes_since_previous`: the plan version then and now, the
patch's files added and removed between the two runs, and the
verdicts (overall, visible, hidden) then and now — a re-review
starts from what changed, not from a cold read.

**Request changes is a third decision.** Beside accept and
reject, `decide_review` records `request_changes`: the reviewer's
note is stored on the order, an audit record names the act, and
the order returns to `changes_requested` — a state a fresh plan
version drafts from, with the note placed in the draft's context
(the port receives it under its own key; the live adapter puts it
in the planning prompt, the scripted model records it in its
accounting of what it was shown). Accept and reject stand only on
an order standing `succeeded`. Request changes stands on the run:
it is available wherever a SUCCEEDED run stands unaccepted —
including on a `rejected` order, where it is the recorded way a
reviewer reverses a rejection into a send-back rather than a
dead end — and it refuses by name (`no_succeeded_run_to_return`)
where no succeeded run exists to send back, and
(`changes_note_required`) where the note is missing: the note is
the decision's content, and a silent send-back would leave the
next plan drafting against nothing.

**Reviewer history is the same fold, by actor.** Decisions are
audit records — actor, timestamp, the run decided on — so
history needs no tracking table: `reviewer_history` folds them
into per-actor counts by kind (accepted, rejected, changes
requested), the work orders each actor decided in decision order,
their decision list, and a **median time-to-decision**, derived
as the decision's timestamp minus the landing of the run it
decided (the run's `finished_at`, its `created_at` as fallback);
a decision whose run cannot be read counts in the tallies but
not the timing, and the read states the derivation on its face.

## The task library (round 12)

**A template is a governed record, not a file.** A template
lives in the store (`task_templates`, migration 0007 in both
dialects), versioned the way plans are: creating under an
existing name writes the next version, and no act ever mutates a
version in place. The record carries the pattern in three parts —
a **task text** with named parameters (`{module}`,
`{behaviour}`), a **scope pattern** naming the file shape an
instantiated order will carry, and a **step skeleton** — plus a
description of when to use it, its author, and its publication
state. Creation validates the shape in code
(`templates.py`, pure functions the service's acts lean on): the
declared parameter set must equal the set the patterns actually
use, in both directions; a scope entry must be a clean
repo-relative path pattern; a skeleton step must name an intent
and files, and its files must be among the scope pattern's
entries — a skeleton may not teach a plan to reach outside the
scope its own template declares.

**Publishing is a second person's act.** A version is born a
draft: readable, comparable, and unusable. `publish_template`
flips it to published, recording the publisher and the moment,
and refuses the version's own author — on normalised names, the
same separation of duties as the promotion gate. (The three seed
templates the library ships — `guard-clause-fix`,
`boundary-fix`, `missing-feature`, one per shape the gold suite
exercises — are seeded through these same two acts at startup,
by two standing seed actors; a deployment that already has a
template of a seed's name keeps its own, untouched.) Version
resolution is the library's standing rule: an explicit
name+version always serves, an omitted version resolves to the
latest published one, and anything unpublished refuses
`template_not_published` — there is no draft instantiation.

**Instantiation is ordinary intake, with the values checked on
the way in.** `instantiate_template` resolves the version,
validates the parameter values against the declared set exactly
(missing and unknown both refuse by name), substitutes, and
creates a work order through the same `create_work_order` every
other order uses. The validation follows where a value lands:
a parameter used in the scope pattern or a step's files/tests is
a **path parameter**, and its value must be a single
path-segment token — no slash, no `..`, no glob — so a value can
widen nothing; a prose-only parameter (the task text, an intent)
must still be brace-free, so it cannot masquerade as a
placeholder. The substituted scope is then re-checked entry by
entry (`template_scope_invalid`) — defence in depth, since the
pattern and the values were supplied by different people. A
caller restating a file scope that differs from the substituted
pattern refuses `template_scope_widening_refused`. What the
template can never do is smuggle a plan: instantiation creates
an order standing at `draft` with no plan on record, and the
gate refuses an unapproved run exactly as for any order.

**The skeleton is context, labelled.** The order's record
carries its provenance: template name and version, the parameter
values, and a **snapshot** of the substituted skeleton — later
template versions cannot rewrite what an existing order was
instantiated from. At drafting, the service places the skeleton
in the draft's context under its own keys, next to the reviewer's
note: the live adapter prints it in the planning prompt, labelled
as the template's suggestion and advisory only; the scripted
model adopts it as its draft when no gold steps compete, and
records it in its accounting of what it was shown. Either way
the draft passes the one shared validator and the per-order plan
gate, and the stored plan carries `template_context` — the
template's name and version, so a reader of the plan can see the
suggestion was in the room. The review packet carries the same
provenance for the reviewer.

**Health is a fold, like everything else here.**
`template_health` derives a version's standing on read from the
orders instantiated from it: orders created; runs by status;
failures by the workbench's own failure classes; review
decisions counted from the audit records, with the acceptance
rate among decided reviews; and the **median priced cost per
success** over the verified successes whose usage the price
table actually priced — unpriced successes are counted in
`priced_successes`' denominator by their absence, never folded
in as zero. Nothing is stored, so the fold can be re-derived
and argued with, like the queue and the reviewer history.

**Publishing is a human act here, on purpose.** The model
profiles earn the default through benchmark evaluations (round
7); templates do not. A template's evidence is its health fold,
and the judgement to publish a shape for other people's work
orders stays a person's recorded act. A future round may gate
template publication on evidence; this round's design keeps the
two governances distinct rather than borrowing the promotion
gate's authority for a decision it was not built to judge.

## The challenge pass (round 13)

Verification (round 10) judges the patch as a change to code:
its shape, its surface, its behaviour under re-grade. It does
not ask the prior question — is this patch the change that was
asked for? A patch can pass every grading test while quietly
doing more than the task asked: tuning an unrelated function
"while it was in there", inventing a threshold, or leaving a
new public helper behind for the next reader to trip over.
The challenge pass is the second reader whose only job is that
question, and it reads nothing but records: the run's stored
patch, the approved plan, the task text, and the pristine
snapshot the run's verification graded on — the same ground
verification stood on. The model's transcript, its step notes,
and any account it gives of itself are not inputs; a challenge
is computed from what the records show, so it can be recomputed
and argued with.

**The rules** (`challenges.py`, a pure module — candidates in,
records out):

- `intent_creep` — the patch changes an existing function or
  method that no plan step and no line of the task text names.
  File-level scope is round 10's verdict; this rule works a
  level down, at the symbol the diff actually touches, because
  a step's intent names symbols, not just files, and creep
  lives inside allowed files. Attribution is by touch: removed
  lines touch the symbol they sit in; an insertion touches the
  symbol it lands strictly inside. Context lines are not
  touches — a hunk whose context reaches into the next function
  has not changed that function — and an insertion after a
  symbol's last line is outside it, so a function appended
  after a function is an introduction (the surface rule's
  business), not an edit to the function above it. A public
  unnamed symbol is `must_answer`; a private one (leading
  underscore) is `advisory` — internal helpers drift more
  innocently, and the reviewer should still see it.
- `single_source_figures` — a numeric constant in an added
  line whose value appears nowhere in the task text, the plan,
  or the changed file's prior content: a figure the evidence
  does not carry. Comparison is by value (`Decimal`-normalised,
  so `100` and `100.0` are the same figure), and 0 and 1 are
  exempt — defaults, indexes, and increments are structure,
  not figures. `must_answer`: a magic number is exactly the
  kind of quiet decision a reviewer exists to catch.
- `unrequested_surface` — an added definition line introducing
  a public function or class that the file did not already
  define and neither the task text nor the plan names.
  Signature changes to existing symbols are edits, not
  introductions; private helpers are not surface. `must_answer`.

One planned rule was **not** built: `weakened_tests_signal`.
Round 10's test-surface check already names every test
configuration file (conftest, sitecustomize, the pytest/pyproject
configuration files) and refuses a gold patch that touches
them at verification — such a patch never reaches SUCCEEDED,
so a challenge rule over the same ground could never fire; and
on ad-hoc orders a test-config touch is already on the
verification record and shown at the review gate as a risk
flag. Building the rule would duplicate round 10, not extend
it.

**The pass and its records.** The pass runs when a run lands
SUCCEEDED, after verification, over the landed records — and
can be re-run on demand over the same records. Each fired rule
stores a challenge record (migration `0008`: `run_challenges`)
keyed by an evidence key — a hash over the rule and its
canonical evidence — so re-running the pass stores nothing new:
same rule, same evidence, same challenge. That is the whole
idempotence argument, and it is why a re-verification or a new
patch gets a genuinely fresh pass rather than recycled
challenges. Records carry the rule, the severity the rule
raised (`severity`), the severity the challenge currently
stands at (`current_severity` — a downgrade moves this, never
the original), the evidence dict, a status (`open`, `answered`,
`superseded`), and the disposition once answered.

**The gate.** Acceptance consults the challenges before it
consults anything else about the run: an order whose latest
succeeded run carries an open must-answer challenge refuses
acceptance as `unanswered_challenges`, naming the count.
Advisory challenges never block — they are shown in the packet
and the console and left for the reviewer's judgement. The
refusal is a gate, not a state: answer the challenges and the
same accept lands.

**Dispositions are recorded acts**, one per challenge, each
with actor and timestamp:
- `rebut` — the reviewer states the challenge does not stand
  and **cites the counter-evidence record** it rests on (the
  plan, the task text, the verification record); a rebuttal
  without its citation refuses. The text and the citation are
  stored on the record.
- `downgrade` — a must-answer challenge is stood down to
  advisory, with a reason stored on the record; the original
  severity stays beside it, so the pass's judgement and the
  reviewer's are both on the record, distinguishable forever.
- `accept_and_revise` — the challenge is upheld and the order
  returns through round 11's request-changes path: the stored
  review note carries the reviewer's text with the challenge
  cited in it (its id, rule, and evidence anchor), so the next
  plan draft sees exactly which challenge sent the work back.

**Supersession.** A work order has one current run; when a
fresh run starts, the challenges of every earlier run are
marked `superseded` — open or answered alike, dispositions
intact. Nothing is deleted, ever: the record of what was
challenged on a run that no longer stands is part of the
order's history, and an answer attempted on a superseded
challenge refuses by name. (A resumed run is the same run —
its challenges stand; only a fresh run supersedes.)

**Surfaces.** The review packet carries the run's challenges
as their own section — rule, severity (with the original shown
when downgraded), evidence, disposition state — with a folded
summary beside them; the review queue's entry for a succeeded
order names its unanswered must-answer count, so the block is
visible before the packet is even opened; the console renders
both and offers the three answers inline. The API serves the
records and takes the answers (`GET …/challenges`,
`POST …/challenges/{id}/answer`).

**The rules' stated limits.** Symbol extraction is this repo's
own simple machinery — `ast` for parseable Python, an
indentation scan as fallback — and naming is a word match
against the task text and the plan's recorded words: a symbol
the records name only in prose the pass cannot parse (a ticket
reference, a paraphrase) will be challenged, and the rebuttal
path exists for exactly that case. Cross-file reasoning (a
helper edited because its caller changed) is beyond the
records the pass reads, and macro-generated or dynamically
defined symbols are invisible to it. The pass is deliberately
conservative in the other direction too: figures carried by
another changed file's prior content, or named in a *different*
step's tests, still challenge — the evidence compared is the
evidence each rule states, no wider. False negatives are
accepted by design; the reviewer's eyes remain the last
reader, now with the challenges in hand.

## Batch work orders (round 14)

**A batch is a named submission of work, stored as one
record** (`batches`, migration 0009 in both dialects): its
name, its submitter, an optional budget, and its items — each
item the specification it was submitted as, the ordinary work
order it created, and the batch-level state the item stands
in. It is not the round-8 comparison, and the difference is
load-bearing: a **comparison** runs model profiles over a
benchmark set to produce evidence for the promotion gate — it
creates no work and decides nothing about any task; a
**batch** submits work to be done and benchmarks nothing. The
two records never borrow each other's authority: finishing a
batch promotes no model, and running a comparison creates no
orders.

**Creation validates whole, then creates.** Every item is
checked before any is created — its shape first (the kind is
known; the fields that kind's intake cannot proceed without
are present), then its semantics against the live records:
a gold item's task exists; a template item is planned through
exactly the computation instantiation performs (the published
version resolved, the parameters validated, the scope
substituted and re-checked) with nothing created; an ad-hoc
item naming a gold task names a real one. The first invalid
item refuses the whole batch, keeping the intake's own
refusal name with the item's index in the message — a batch
with one bad item creates nothing, not even a batch record.
Creation then runs item by item through the ordinary intakes
(gold, ad-hoc, template instantiation), and each order carries
the batch's provenance — id, name, index — on its own record,
beside any template provenance. From that moment an item's
order is indistinguishable from one that arrived alone: same
plan gate, same sandbox, same verification, same review.

**The driver advances; it never decides.** A drive pass is a
recorded act that walks the items in submission order and, per
item, does only the work that needs no person: draft a plan
where one is draftable, run an approved plan, and otherwise
record where the item waits. It stops each item at the gate a
person owns — the plan decision, the review, the recovery
decision on an interrupted run — and never approves, reviews,
resumes, or restarts anything: there is no bulk approval
anywhere in this round, by construction. The pass runs items
strictly one at a time; sequential execution is the design,
not a limitation (the house rule for verification), and there
is no parallel path. The pass marks itself active on the batch
record before it starts and clears the mark when it completes,
storing its run sheet — per item: the act taken, where it
stopped, the next act — as the batch's `last_drive`. A second
pass while a mark stands refuses `batch_driver_active`; a pass
that dies mid-flight leaves its mark standing, saying plainly
that a driver was here and did not finish, until a person
releases it (`release_batch_driver`, a recorded act with a
reason — the dead pass's actor and start time stay in the
audit trail).

**The batch budget is a fold, checked at item starts.** The
batch may carry ceilings of its own — model calls, tokens,
wall seconds — on top of every run's per-run ceilings (round
6), which still apply inside each item unchanged. Spend is
folded on read from the items' usage records: model calls are
every usage row the items' orders recorded, drafting
included; tokens are provider-measured tokens only (scripted
character accounting never counts, round 6's rule); wall
seconds are the sum of the items' runs' active seconds — the
quantity a run's own ceiling measures (plan drafting is model
latency recorded per call in usage, not a run, and carries no
wall record of its own). The fold is checked before the driver
starts an unstarted item: when it has reached a ceiling, that
item and every unstarted item after it are marked
`budget_stopped` **on the batch record** — each order stays in
its true state (draft, unplanned, unrun; the mark is not a
fake run or order status) — and the completed pass refuses by
name, `batch_budget_exhausted`, naming the ceiling, the
limit, and what was spent. Work already past its plan gate is
not batch-stopped: it finishes under its own per-run ceilings.
The batch budget governs starts, the run budgets govern runs —
the two compose, neither replaces the other.

**Cancellation marks only what never started.** Any named
actor may cancel a batch — cancellation stops work, it
approves nothing, and the record names who did it and why (a
reason is required). Unstarted items are marked `cancelled`
at batch level; their orders remain readable, never deleted.
Started items are untouched and finish through their own
gates. Whether an item has started is itself folded at the
moment of the mark, from the order's own records — a plan, a
run, or a single usage row — never stored anywhere as a
judgement that could drift from the records.

**The report is one honest page, folded on read.**
`batch_report` derives everything a reader decides from: the
items with their effective batch state (a stored mark, or
`started`/`pending` folded from the order's records), each
order's true status and its next act; runs counted by status
with failures by the workbench's failure classes; challenges
outstanding across the batch's orders (round 13); the spend
fold against the batch's ceilings with the budget stop when
one stands; the priced cost fold over all the items' usage —
**cost per verified success across the batch**, the webinar's
metric at batch scope, with unpriced usage counted and kept
out of the money exactly as everywhere else; and the items
awaiting a person, under the workbench's own categories. The
batch's standing is derived, not stored: `cancelled` when the
record says so, `complete` when every item is batch-marked or
its order terminal, `open` otherwise — the record is never
told the work finished, the fold can see it. The review queue
and the review packet carry the batch provenance, so a person
deciding on one order sees the submission it arrived in.

## Observability (round 15)

Three pieces, all in the shell's fold-on-read discipline.

**The failure taxonomy** (`taxonomy.py`, data in
`taxonomy/taxonomy-v<N>.json`) classifies every named failure in
the codebase exactly once: a GateError code, a tool-port refusal
code, or a failure class a stored record carries, mapped to one
of eight classes (model serving, plan, sandbox/tool,
verification, budget, gate refusal, run lifecycle, store/infra),
a likely cause, and the operator's next action. It is governed
data in the price table's mould — versioned files, never edited
in place, loaded fail-closed at startup (an entry naming an
undeclared class, a duplicate name, a version disagreeing with
its file name, or an entry missing its cause or action each
refuse by name). Only the highest version is in force:
classification is a read-time act over records, so there is no
per-record version to honour the way costing honours old price
tables. Classification itself never refuses — `classify()` of an
unknown name returns the **unclassified** standing, and the
digest counts it under its own name. That direction is
deliberate: records outlive the build that wrote them, and a
silent drop would hide exactly the drift an operator needs to
see. The other direction is held by a conformance test that
derives the names from the code — AST over the GateError raise
sites, the tool port's `code=` constants, and the failure-class
constants the records are built from — and holds the shipped
table to covering every one. (`config_error` is the one
deliberate non-derived entry: a ConfigError carries no code, so
configuration refusals classify under that single name.)

**The operator digest** (`service.operations_digest`,
`GET /api/operations-digest`) folds the deployment's standing
from the stored records on every read: runs by status; failures
by name, by taxonomy class, and unclassified (with the top ten
names carrying their class, likely cause, and next action);
plan-gate decisions (drafted, approved, rejected, amended) and
review decisions (accepted, rejected, changes requested) from
the audit records; challenges raised, by rule and by status,
with the outstanding split must-answer vs advisory; batches by
their derived standing; usage totals; and the priced cost fold
— the R6 fold over the window's usage rows, with cost per
verified success counted over the window's SUCCEEDED runs. The
window (`since`/`until`) filters each family by its own
`created_at`; an unreadable timestamp or a start after the end
refuses as `invalid_digest_window`. Two standings are reported
**as they stand at read time** — a challenge's current status, a
batch's derived standing — because the fold reads current
records, and the digest's derivation text says so rather than
pretending to reconstruct history. There is no metrics table
and no stored counter anywhere in this design: a stored counter
could drift from the records it summarises, and the records are
the evidence.

**Readiness and the log contract.** `/health` stays the cheap
liveness check. `/readiness` (`service.readiness`) now probes:
the store answers `migration_status()` (applied versions read
from `schema_migrations` against the migration files on disk for
the dialect — the difference named, never a stack trace); the
serving profile resolves and, when live, is checked for its two
named not-ready states (recorded `servable: false`, with its
reason carried from the record; its key env unset, named); and
the governed versions in force are reported — the price table's
and the taxonomy's — alongside every field the endpoint has
always carried. The status is `ready` only when all three
probings stand clean. The log contract is stated in
`logging_setup.py` and held by a test that drives one full
work-order journey through the API and asserts it on every
captured line: one JSON object with `ts`, `level`, `event`, and
`request_id` (the serving request's id, or null outside a
request), plus the ids of the entities the event touches. The
round's audit of the existing log sites found the plan lifecycle
was the one governed act that wrote audit records without log
lines; `plan_drafted`, `plan_decided`, and `plan_amended` log
now, with the same fields their audit records carry. Refusals
are not logged — they are audit-and-409 business, and the digest
counts failures from the records, not the log.

## The round map

R1 core · R2 Baseten live path + profiles v1 · R3 plan depth +
staleness · R4 tool depth · R5 failure recovery · R6 budgets
+ cost ledger · R7 model promotion gate · R8 shortlist comparison ·
R9 injection red team · R10 verification depth · live wiring
(profiles and prices verified against the provider's live
catalog, the reasoning-model request shape, and the live
smoke tool) · R11 review workbench · R12 task library ·
R13 challenge pass · R14 batch work
orders · R15 observability · R16 deployment profiles · R17 MCP
surface · R18 dual control · R19 skills library · R20 release
hardening (full gate re-run, live Postgres conformance, hosted boot,
prod smoke).

## Honest limits

- The model profiles and the price table are no longer doc-shaped:
  both were verified against Baseten's Model APIs catalog read
  live on 2026-10-19, and the records name that read. The catalog
  will move again; a profile or price is only ever as current as
  the read its record cites, and refreshing one is a data change
  through the same governed loaders, not a code change.
- The live Baseten adapter is built to Baseten's documented
  OpenAI-compatible shape and is exercised end to end in tests and
  evals through a fake transport replaying recorded-shape
  responses. It has also been run against the real endpoint: on
  2026-10-19 the blueprint's own adapter ran the gold task
  reserve-overcommit end to end against Baseten's live Model APIs
  on `deepseek-ai/DeepSeek-V4-Flash-0731` (profile
  `baseten-deepseek`) — the model drafted the plan, took 9 tool
  steps, and produced an in-scope patch to
  `workshop/inventory.py`; verification passed on both the
  visible and the hidden grading sets; the run recorded 8 API
  calls and 20,366 tokens at a total priced cost of $0.00281619
  under price table v2 (run `run-97f7d412bd`). That is a dated
  deployment-check result, recorded once — not a standing claim
  about the endpoint. `tools/live_smoke.py` is the repeatable
  check that re-runs it and prints the recorded evidence.
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
- The digest is a fold, and folds cost reads: over a deployment's
  full history every digest request re-reads the run, usage,
  audit, challenge, and batch records. That is the accepted price
  of never storing a counter that could drift from the evidence —
  at blueprint scale it is nothing; a deployment that outgrows it
  should cache the fold, never replace it with stored counters.
  The window is `created_at`-based, so a decision about work
  created before the window does not appear in it even when the
  decision itself lands inside; challenge and batch standings are
  as they stand at read time, as the digest's own derivation
  text states.
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
  profiles record not-servable), and a deployment with a key
  benchmarks four of them — the qwen profile records not-servable
  there too, because the catalog its record was verified against
  lists no Qwen model — and pays a full benchmark per servable
  live profile per comparison. That is the
  honest price of evidence over assertion, and the reason the
  comparison is a recorded act rather than a dashboard refresh.
- The red team's grading isolation (round 9) proves the grading
  tests *run* — unweakened collection, no repo conftest, evidence
  counts on the verdict. It does not prove the tests are strong:
  for an ad-hoc order the grading set is the operator's own tests
  in the target repo, and a grading test that asserts nothing is
  a test the operator chose. And the pack attacks the shell with
  hostile scripted steps, not a live model: whether a real model
  follows planted repo text is that model's property, and no
  defence here depends on it.
- Verification depth (round 10) narrows the test-gaming surface
  but does not close it. The hidden sets are one extra file per
  gold task — real edge cases, and a small set: a deployment
  growing its suite grows the hidden sets with it, and an overfit
  clever enough to satisfy both sets' stated behaviour is, at
  that point, close to a fix. Ad-hoc orders have no hidden set
  (the record says `not_applicable` rather than pretending one
  ran), and their test-surface touches are flagged, not refused:
  the review gate's human is the control there. Scope-creep
  detection at this layer is file-level; whether an edit inside
  a named file strays beyond its step's intent is the challenge
  pass's question, answered in round 13 at symbol level from
  the stored records — with its own limits stated in its
  section (naming is a word match over the records' own words;
  cross-file reasoning is beyond what the pass reads).
- The review workbench (round 11) is folds over the records, so
  it inherits their resolution: timestamps are second-resolution,
  so a decision taken inside the run's landing second reads a
  time-to-decision of 0 — the median is honest arithmetic over
  honest records, not a finer clock invented for the dashboard.
  The queue's age is time in the order's current state by its
  record's last update; an order whose state was entered by an
  act that did not touch the order record would age from the
  wrong moment, and every transition in the shell writes the
  order record — the fold leans on that discipline.
- The task library (round 12) governs the shape of an order, not
  its content. Parameter validation makes widening and traversal
  impossible through the values, and the substituted scope is the
  scope the gates then enforce — but a template's task text is
  prose a person wrote, and a published template with a careless
  (or hostile) task text produces careless work orders that pass
  every mechanical check; the controls are the ones the record
  shows: authorship on the version, a second person's publish
  act, provenance on every order, and a health fold that makes a
  bad shape visible in its outcomes. The skeleton's influence on
  a live model's plan is advisory in fact as well as in name —
  the plan is the model's, validated and approved per order —
  and health attributes outcomes to the template version the
  order names, though the model, the target, and the reviewer
  all share the causation; the fold reports the association, it
  does not apportion it.
- Batch work orders (round 14) orchestrate; they do not
  parallelise, and the driver is a synchronous in-process pass,
  not a scheduler: a deployment that wants a batch driven keeps
  an operator (or a cron) invoking the pass, and the record of
  each pass is its run sheet on the batch. The one-driver mark
  is a stored record checked at pass start — two processes
  racing the same instant could both read it clear; the store's
  write lock narrows that window but the mark is a governance
  record, not a distributed lock, and it is not presented as
  one. The batch budget is checked at item starts, not
  continuously: a single item's run can carry the fold past a
  ceiling mid-flight (its own per-run ceilings are the bound
  there), and the stop lands on the next unstarted item. Wall
  seconds inherit the runs' active-seconds convention, so a
  batch's wall figure excludes the time its items spent
  waiting on people at gates — it is work time, not elapsed
  calendar time, and it reads as such.
