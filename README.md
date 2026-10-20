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
  the model by profile — the shipped records name the slugs
  Baseten's Model APIs catalog listed when it was read live on
  2026-10-19: `baseten-deepseek`
  (`deepseek-ai/DeepSeek-V4-Flash-0731`, the model this blueprint
  is built with), `baseten-glm` (`zai-org/GLM-5.2`), `baseten-kimi`
  (`moonshotai/Kimi-K3`), `baseten-gpt-oss` (`openai/gpt-oss-120b`).
  The fifth family profile, `baseten-qwen`, names a model that
  catalog does not currently serve; its record says so
  (`servable: false`, with the reason), and every surface that
  lists it records it **not servable** rather than pretending the
  slug is live. Every model call's usage — tokens and
  latency, from the provider's usage object — is recorded against
  its run; that accounting is what the cost ledger prices. Each
  profile also records the per-request completion ceiling
  (`max_tokens`, 8192) the adapter sends — these are reasoning
  models, and the budget they reason within is a per-model fact,
  not a constant in code.
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
- **Deployment profiles** — `DEPLOYMENT_PROFILE` names the
  deployment's shape (`local` by default; `hosted` for production
  on Baseten's Model APIs; `hosted-self` for a self-hosted
  endpoint), as governed records in `profiles/deployment/`.
  Naming one binds its rules at startup — under `hosted` the
  offline model and the SQLite store each refuse by name before
  anything serves. See "Deployment profiles" in
  `docs/architecture.md`.

## The live smoke check (operators)

`tools/live_smoke.py` is the deployment live check: it builds the
service the way the app does (settings from the environment), runs
**one** gold task end to end on a live profile — plan draft, plan
approval, sandboxed run, verification in code — and prints the
recorded evidence: the plan decision, the run's status and
verification verdict, every usage row, and the priced cost.

```bash
BASETEN_API_KEY=... uv run python tools/live_smoke.py
BASETEN_API_KEY=... uv run python tools/live_smoke.py --profile baseten-glm --task low-stock-report
```

The default profile is `baseten-deepseek` and the default task is
`reserve-overcommit`. Without the key in the environment it prints
the shell's named refusal and exits non-zero — no traceback, and
no silent fallback to the offline model.

## Machine clients (the MCP surface)

Round 17 adds an MCP surface for machine clients — an IDE, a
desktop assistant, another agent. It speaks stdio JSON-RPC and
boots on the same settings as the API, so the deployment profile
in force binds it too:

```bash
uv run coding-agent mcp    # or: python -m open_model_coding_agent.mcp_server
```

Point any MCP host at that command. The contract is the surface's
founding rule: **a client may state work and read what the shell
made of it — it may never approve, execute, or decide.** The
eight tools are `list_work_orders`, `get_work_order`,
`get_review_packet`, `get_review_queue`, `get_operations_digest`,
and `get_cost_report` (reads, over the same folds the console
reads), plus `create_work_order` and `draft_plan` — intake and
plan drafting, the two preparatory acts, each taking an explicit
`actor` that is recorded exactly as the API records it, with the
MCP channel named on the order's audit trail. There is no tool
for plan approval or rejection, run execution, review decisions,
challenge dispositions, promotion, batch driving, or template
publishing: those stay human acts in the console or the API, and
a call to a decision-shaped name is refused by name.

Honest status, plainly: the transport is stdio only (no HTTP),
and there is no auth layer — actor names are recorded, not
authenticated, the standing caveat of this blueprint. The
protocol is a small hand-rolled JSON-RPC implementation in the
repo's own code (the architecture doc records why the official
SDK was not adopted against the frozen dependency pins).

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
   in-run test results are never the verdict. The grading run is
   isolated from the repo it grades (round 9): no repo conftest, no
   repo `addopts`, interpreter startup files set aside — and the
   verdict reads pytest's JUnit evidence, so a grading test that
   was skipped or deselected is not a pass. And the verdict has
   depth (round 10): the record carries the patch's shape (files,
   lines added/removed per file, binary, whitespace-only — a
   whitespace-only patch fails as `no_substantive_change`), the
   files it touched that are test/grading surface
   (`test_surface_touched` — for a gold task that fails the run by
   name; for an ad-hoc order it is flagged and shown in red at the
   review gate), and the files no step of the approved plan named
   (`unplanned_files` — a gold patch with one fails by name). A
   gold run that passes the visible set is then re-graded against
   the task's **hidden** grading set — staged only at that moment,
   into the same patched copy, never present in any workspace —
   and a hidden failure flips the verdict to FAILED as
   `hidden_grading_failed`, with the visible and hidden verdicts
   recorded distinctly.
5. **Review gate.** A human accepts a verified run — the `.patch`
   file is exported to the run artifacts — rejects it with a
   recorded reason, or **requests changes**: the reviewer's note
   is stored on the order, which returns to a state a new plan
   version drafts from with the note in its context. Nothing is
   applied to the source repo.
6. **The review workbench (round 11).** Reviewers work from a
   **queue derived on read** from the stored records — plans
   awaiting a decision, verified runs awaiting review, failed
   runs with their failure class, interrupted runs awaiting a
   recovery decision — each entry naming its single next act and
   its age. There is no queue table and no bulk decision anywhere:
   every decision is one recorded act on one order. For each
   order the **review packet** assembles the whole decision in
   one read — task and scope, the approved plan the run ran on,
   the patch and its shape, the verification record with the
   risk flags distinct (test surface touched, unplanned files,
   the hidden verdict — or its named absence on an ad-hoc order),
   usage and priced cost, the run history, and the prior
   decisions; after a send-back, the re-review packet names what
   changed since the previous one. **Reviewer history** folds the
   same decision records by actor: counts by kind, the orders
   decided, and the median time-to-decision.
7. **Audit.** Every act — work order, plan, decision, tool call,
   refused edit, verification, review — is a stored record; a run's
   history reconstructs end to end.
8. **Budgets and the cost ledger (round 6).** A run spends against
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
   version stamped on it when it was recorded. The shipped v1
   table records its slugs with **null prices**: numbers this
   repo has not verified are not invented. Table v2 (2026-10-19)
   is the verified publication — five slugs priced exactly as the
   live catalog read stated them — and v1 stays on disk untouched,
   so rows recorded under it still fold unpriced, counted with
   the reason named, never zeroed. The
   console folds all of it per profile into **cost per successful
   task** — all of a profile's priced spend, failures included,
   over its verified successes. The offline model's accounting is
   characters, labelled `scripted` — never priced as tokens.
9. **The promotion gate (round 7).** The gold suite is split into
   a **tuning** set and a **held-out** set, each named by a
   content hash over its tasks — add a task or edit a defect and
   the hash moves, and every result recorded under the old hash is
   stale. A model profile becomes the agent's **default** only by
   evidence: a benchmark runs the candidate through this same
   shell (work orders, plan gate, runs, verification in code) and
   stores the record; a promotion evaluation runs the held-out set
   three times and is scored by its **worst** run, with any
   out-of-scope edit or unverified success in any run failing it
   outright. Promotion itself is a judged act — it needs that
   passing, current evaluation, a judge who is not the evaluator,
   and no regression against the incumbent default's own recorded
   baseline (an equal score promotes; the bar is no regression,
   not must-beat). The default is a stored record: promotion and
   rollback are append-only events, and the console shows the
   whole gate — sets and hashes, evaluations, history — beside
   the cost fold.
10. **The shortlist comparison (round 8).** A comparison runs
   **every** recorded profile — the five Baseten family profiles
   and the offline profile — once over the same benchmark set
   (the tuning set by default, for breadth; the set and its
   content hash are named on the record) through the same
   benchmark path, under one actor, and stores the comparison as
   a record. A profile that cannot serve in this environment —
   a live profile with no key set — is recorded `not_servable`
   with the reason named: never skipped, never faked. The report
   folds the stored records into the evidence matrix: verified
   successes per task, total usage, priced cost and **cost per
   successful task** where the price table prices the slug
   (unpriced folds are labelled, never zero), failure classes,
   and each profile's promotion standing read from the gate's
   records. The report ranks nothing and changes nothing —
   recomputing it is a pure derivation, a comparison whose set
   has moved reads as stale, and the record states on its face
   that the promotion gate is the only path that changes the
   default.
11. **The task library (round 12).** The shapes of recurring work
   — a missing guard, a boundary off by one, a feature that is a
   stub — are stored as **governed templates**: a task-text
   pattern with named parameters, a scope pattern, and a step
   skeleton, versioned like plans (an edit is a new version,
   never a mutation). A version is usable only once **published**,
   and publishing is an act by someone other than its author.
   **Instantiation is ordinary intake**: the parameters are
   substituted and validated in code — a value that lands in a
   path is a single path-segment token, so traversal, absolute
   paths, and glob escapes refuse by name, and a caller restating
   a wider scope than the pattern produces is refused as
   widening. The created order carries the template's provenance
   and a snapshot of the substituted skeleton, and is then an
   ordinary order: the skeleton reaches the plan draft as
   **labelled context only** — a template can suggest steps,
   never smuggle a plan; the plan is still drafted, and still
   approved, per order. The review packet names the provenance,
   and each version's **health** — orders created, runs by
   outcome and failure class, acceptance rate, median priced
   cost per success — is folded on read from those orders, never
   a stored counter. Three seed templates ship in the library,
   created and published through the same acts (by two separate
   seed actors). Publishing a template is a human governed act,
   deliberately unlike the model profiles: templates are not
   promoted through benchmark evaluations — the evidence a
   template earns is its health fold, and the judgement stays a
   person's.
12. **The challenge pass (round 13).** Verification says the
   patch passes its grading; it does not ask whether the patch
   is the change that was asked for. Before a review can accept,
   a **challenge pass** takes a second, independent look —
   deterministic rules computed from the run's **stored records
   alone**: the patch, the approved plan, the task text, and the
   pristine snapshot verification graded on. Never the model's
   transcript or its account of itself. Three rules:
   **intent creep** — the patch changes an existing function or
   method that no plan step and no line of the task text names
   (file-level scope is verification's; this works a level
   down, at the symbol the diff actually touches — an untouched
   neighbour caught in a hunk's context is not a touch, and a
   function appended after a function is an introduction, not
   an edit to the one above it); **single-source figures** — a
   numeric constant in the patch that appears nowhere in the
   task text, the plan, or the file's prior content (0 and 1
   are exempt as structural constants: defaults and indexes,
   not figures); and **unrequested surface** — a new public
   function or class neither the task text nor the plan names.
   Each fired rule stores a **challenge record** — the rule, its
   severity (`must_answer`, or `advisory` for private symbols),
   and the evidence: the file, the hunk, the symbol or the
   figure itself. An unanswered must-answer challenge **refuses
   acceptance by name** (`unanswered_challenges`, with the
   count); advisory challenges are shown and never block. A
   reviewer answers each challenge with a recorded act:
   **rebut** it, naming the counter-evidence record the
   rebuttal rests on; **downgrade** it to advisory with a reason
   (the original severity stays on the record); or **accept
   and revise** — the order returns through the request-changes
   path with the challenge cited in the stored note.
   Challenges are never deleted: when a newer run supersedes
   the run they were raised on, they are marked superseded,
   dispositions intact, and the pass itself is idempotent —
   re-running it over the same run stores nothing new. They
   ride the review packet as their own section, and the review
   queue names the unanswered must-answer count on the entry
   itself. The rules are deliberately conservative — a false
   negative costs a reviewer nothing they were not already
   doing; a false positive would train everyone to wave the
   pass through — and the binding proof is the gold suite
   itself: all six tasks' verified patches raise **zero**
   must-answer challenges, checked on every eval run.
13. **Batch work orders (round 14).** Work often arrives in a
    pile, so a **batch** submits it as one named record — gold
    tasks, ad-hoc orders, and template instantiations, mixed —
    with an optional budget of its own. A batch is not a
    comparison: a comparison benchmarks models and creates no
    work; a batch creates work and benchmarks nothing.
    Submission **validates every item first** — an unknown gold
    task, an unpublished template, a traversal parameter — and
    the first invalid item refuses the whole batch by name,
    creating nothing. Each item then becomes an ordinary work
    order through ordinary intake, carrying the batch's
    provenance (which the review queue and the review packet
    show), and is governed exactly as if it had arrived alone.
    A **sequential driver** advances items one at a time
    through the acts that need no person — drafting a plan,
    running an approved plan — and stops each item at the gate
    a person owns, recording its run sheet on the batch; it
    never approves, never reviews, and never runs two items at
    once (sequential is the design, not a limitation), and a
    second pass while one is recorded active refuses by name.
    The **batch budget** folds from the items' usage records
    on read; when the fold reaches a ceiling, the unstarted
    items are marked `budget_stopped` **on the batch record** —
    their orders stay in their true state — and the driver
    refuses to start further items by that name; each run's own
    ceilings still apply inside its item. **Cancellation** (any
    named actor, reason recorded) marks only unstarted items;
    started work is untouched and finishes through its own
    gates, and cancelled orders are never deleted. The **batch
    report** folds it all into one page: items by state, runs
    by outcome and failure class, challenges outstanding,
    priced cost and cost per verified success across the batch,
    and the items awaiting a person.
14. **Observability (round 15).** An operator inherits every
    failure this shell has ever named, so the failures are
    classified once, in governed data: a versioned **taxonomy**
    (`taxonomy/taxonomy-v1.json`) maps every failure name — the
    gate refusals, the tool port's codes, the classes the
    records carry — to a class, a likely cause, and the
    operator's next action, loaded fail-closed at startup like
    the price table. A conformance test derives the names from
    the code and holds the table to covering all of them; a
    record carrying a name the table predates classifies as
    **unclassified** and is counted under its own name — records
    outlive builds, and nothing is silently dropped. The
    **operator digest** (`GET /api/operations-digest`, and a
    section of the console) folds the deployment's standing
    from the stored records on every read — runs by status and
    failure class, plan-gate and review decisions (request
    changes counted as its own decision), challenges raised and
    outstanding, batch standings, usage, priced cost and cost
    per verified success, and the top failure names with their
    next actions — over an optional `created_at` window. There
    is no metrics table and no stored counter: the digest is a
    fold, re-derivable like every other read in this shell.
    **Readiness** deepens to match: `/readiness` probes the
    store live and reports migration currency (applied vs
    on-disk versions), the serving profile's servability state
    by name (a profile recorded not servable, or a live profile
    missing its key, is not-ready with the reason — never a
    stack trace), and the price-table and taxonomy versions in
    force; `/health` stays the cheap liveness check. The JSON
    log contract is stated and tested — every line carries its
    timestamp, level, event, request id (the serving request's,
    or null outside one) and the ids of the entities it touches —
    and the audit found the plan lifecycle was the one governed
    act that audited without logging; plan drafts, decisions
    and amendments log now, like everything else.
15. **Deployment profiles (round 16).** The deployment's shape is
    a governed record too (`profiles/deployment/`), and naming
    one binds its rules: **`local`** (the default — offline or
    live model, SQLite or Postgres, no key beyond the model
    profile's own rule), **`hosted`** (production on Baseten's
    Model APIs — a live profile must serve, Postgres is
    required, the key must be present), and **`hosted-self`**
    (production on a self-hosted endpoint — like hosted, and the
    endpoint's host must not be a Baseten host). The records are
    loaded fail-closed — an unknown name, field, or rule refuses
    at startup — and every rule is enforced when settings load,
    not at first use: a configuration that violates a rule of
    the profile it names (`DEPLOYMENT_PROFILE`) refuses before
    anything serves, naming the profile and the rule. The
    active profile is visible everywhere it matters: readiness
    reports its name, version, content hash, and per-rule
    states; the console header and the operations digest name
    it; and every run record is stamped with the deployment
    profile it ran under.
16. **The MCP surface (round 17).** Machine clients — an IDE, a
    desktop assistant, another agent — get a surface under the
    opposite contract to a raw tool bridge: **clients submit
    work and read standing; they never decide.** Eight tools
    over stdio JSON-RPC: six reads (`list_work_orders`,
    `get_work_order`, `get_review_packet`, `get_review_queue`,
    `get_operations_digest`, `get_cost_report`) riding the same
    folds the console reads, and two preparatory acts
    (`create_work_order`, `draft_plan`) that enter through doors
    on the service as the ordinary acts under the caller's name,
    with the channel on the audit trail. No tool approves a
    plan, starts a run, decides a review, answers a challenge,
    promotes a model, drives a batch, or publishes a template —
    a call to a decision-shaped name is refused by name. A plan
    drafted here still awaits a person's approval; a governance
    refusal returns in the shell's own words, never as a stack
    trace. See "Machine clients" below for how to run it.

## The gold suite (v1)

Six tasks, gold-by-construction: each ships a broken state of the
workshop codebase and grading tests that fail on it and pass on the
reference fix. Since round 10 each task also ships a **hidden**
grading file (`evals/gold/hidden/`) — edge cases of the same fix on
inputs the visible tests never use, failing on the broken state and
passing on the reference fix by the same construction, and used
only for the verification re-grade.

| Task | Shape | Scope |
| --- | --- | --- |
| Reservations can overcommit stock | bug fix | `workshop/inventory.py` |
| Tax is charged on the pre-discount subtotal | bug fix | `workshop/pricing.py` |
| The low-stock report always comes back empty | missing feature | `workshop/inventory.py` |
| Cancelling an order never releases its stock | bug fix | `workshop/orders.py` |
| Ten-unit orders miss the bulk discount | boundary fix | `workshop/pricing.py` |
| Orders cannot take a coupon | feature, two files | `workshop/pricing.py`, `workshop/orders.py` |

For the promotion gate the suite is split: **tuning** — the four
single-file repairs — and **held-out** — the boundary fix and the
two-file coupon feature. Promotion is judged on held-out only;
nothing is tuned against it.

`make evals` grades all six end to end through the service, plus
the construction proof, a gate check per task, and legs for the
live adapter's code path, plan staleness, and plan amendments.
Zero-tolerance counters: edits landed outside scope, successes
without a passing verification (since round 10, including any
success whose hidden re-grade verdict is not a pass), and
injection effects landed.

## Layout

```
src/open_model_coding_agent/  the governance shell (service, gates,
                              sandbox tools, patch + verification,
                              model port, Baseten adapter, store,
                              API, console)
profiles/                     the model profiles, as versioned records
                              (deployment profiles in profiles/deployment/)
prices/                       the governed price table, as versioned records
workshop/                     the sample codebase the agent works on
evals/gold/                   the gold task suite (broken/fixed/grading)
evals/run_evals.py            the eval gate
migrations/                   numbered SQL migrations, both dialects
tests/                        the shell's own test suite
docs/architecture.md          the design, in full
```

## Honest status

Rounds 1–17 plus the live-wiring round are built and gated:
**265 tests passing, evals PASS**
(6/6 gold tasks verified end to end on the offline model, plus one
gold task end to end through the Baseten adapter's code path, a
recovery leg where an ambiguous first edit refuses by name and the
run still lands its repair, an interruption leg where a run
that dies after its first edit resumes to a verified success
without re-applying it, a costing leg that prices a live-path
run plan-vs-execution and folds cost per successful task, a
promotion leg that measures the incumbent's held-out baseline and
a copy candidate at the gate's full worst-of-3, refuses the
evaluator's self-promotion, promotes through a second actor,
rolls back, and refuses a regressed candidate, and a comparison
leg where a throwaway shortlist — the offline profile, a copy,
a priced fake-transport live profile, and a keyless live profile
recorded not_servable with its reason — runs side by side over
the held-out set and prints the folded matrix, and a red-team leg
where planted repo instructions obeyed by a hostile model, a
skip-all conftest, self-approving task text, a fake verification
transcript, path tricks, symlinks, and a hostile live plan draft
land nothing; counters at zero, and a verification-depth leg
where an overfit fix that special-cases the visible tests' inputs
passes the visible set and fails the hidden re-grade, a gold
test-surface patch and a gold unplanned-file patch are each
refused by name, and a whitespace-only patch is named
`no_substantive_change`, and a workbench leg where one order is
driven through the reviewer's whole journey on the queue and
packet reads — rejection, a request-changes send-back against
the stored note, a new plan and run, a re-review packet naming
what changed, and acceptance, with the leg's decisions folded
into reviewer history, and a library leg where a seed template
is instantiated onto its matching gold task — provenance on the
order, no plan smuggled, the skeleton labelled as template
context in the draft, the run verified, the packet naming the
provenance, and health folding the accepted journey, with an
unpublished template and a traversal parameter each refusing
by name, and a batch leg where a mixed submission — one gold
task, one template instantiation — is validated whole (one bad
item refusing the batch with nothing created), driven to the
plan gate and stopped there, approved outside the batch one
order at a time, driven again to two verified runs, and read
back as one folded report whose completion is derived from
the items' own records, and a deployment leg where the
settings boot under the hosted profile with a complete fake
env and every declared rule stands satisfied, the same boot
without DATABASE_URL refuses naming the deployment profile
and the store rule, and a gold task serves end to end under
the hosted profile with the deployment stamp on its run
record — the leg's one deviation stated in it: the eval
environment has no Postgres, so its serving store is the
harness's SQLite under the hosted boot's record).
The default model profile is now earned, not configured: the gold
suite is split tuning/held-out under content hashes, a profile is
benchmarked through the real service path with the records
stored, and promotion — a judged act with named refusals
(`no_passing_evaluation`, `evaluation_stale`, `regression`,
`self_promotion`) — moves the default as an append-only record,
with rollback as its own recorded act. Runs now record
checkpoints as they work, and a run that dies mid-flight lands
INTERRUPTED — resumable when its basis (plan, target, workspace)
checks out in code, restartable under the same approval, or
abandonable as a recorded act that names its partial patch, which
the review gate can never accept. Runs also spend against enforced
budgets (calls, tokens, active wall seconds — snapshotted at
start; a budget-stopped run cannot resume, only restart into a
fresh budget) and every run carries its priced cost from the
governed price table. The profiles and the price table have since
been verified against Baseten's live Model APIs catalog (read
2026-10-19): the profile allowlists name exactly the slugs that
read verified, and price table v2 prices five of them at the
figures that read stated — v1 remains on disk, and rows recorded
under it still fold as unpriced (counted, reasoned, never zero).
One profile the catalog does not serve stands recorded as such:
`baseten-qwen`, not servable with its reason on the record. The live adapter is exercised in tests and evals
through a fake transport replaying recorded-shape responses —
and it has been run against the real endpoint: on 2026-10-19
the blueprint's own adapter ran the gold task
`reserve-overcommit` end to end against Baseten's live Model
APIs on `deepseek-ai/DeepSeek-V4-Flash-0731` (profile
`baseten-deepseek`) — the model drafted the plan, took 9 tool
steps, and produced an in-scope patch to
`workshop/inventory.py`; verification passed on both the visible
and the hidden grading sets; the run recorded 8 API calls and
20,366 tokens at a total priced cost of $0.00281619 under price
table v2 (run `run-97f7d412bd`). That is a dated
deployment-check result, recorded once; `tools/live_smoke.py` is
the repeatable check that re-runs it. The
shortlist now runs side by side as a stored comparison: every
profile over the same set through the benchmark path, the
keyless ones recorded not_servable with their reasons, and the
qwen profile recorded not_servable with or without a key, and the
report folded from the records — evidence with no ranking
attached, because the promotion gate is the only path that
changes the default. The injection red team (round 9) then
attacked the shell with hostile repos, hostile task text, and
hostile plan drafts, driven by models that obey the injection —
two attacks landed on the opening build: a target-carried
conftest (or `sitecustomize.py`) could fake verification by
keeping the grading tests from ever running, and symlinks in the
target were dereferenced by search and by the tree copies. Both
are fixed at the layer that owns them — grading now runs
isolated, on JUnit evidence that every grading test ran, and
symlinks are followed nowhere — and both are kept as
regressions; the full report is `docs/red-team.md`. Verification
then gained depth (round 10): a patch is now checked as a change,
not only as a test result — its shape is on the record, a gold
patch that edits the test/grading surface or lands a file no plan
step named fails by name, and every gold task carries a hidden
grading set that re-grades a visible pass on inputs the visible
tests never use, so an overfit fix that special-cases them flips
to FAILED as `hidden_grading_failed`; the full report is
`docs/verification-depth.md`. The review workbench
(round 11) then gave reviewers their surface: a queue folded
from the records on every read — each waiting order naming its
single next act and its age, with no bulk decision anywhere in
the route table — a review packet that assembles the whole
decision (plan, patch and shape, verdicts with the risk flags
distinct, usage and cost, run history, prior decisions, and,
on a re-review, what changed since the previous packet), a
third decision — request changes — whose note is stored on the
order and shown to the next plan draft, and reviewer history
folded from the decision records. The task library (round 12)
then made the shapes of recurring work governed records —
templates versioned like plans, published by a second person,
instantiated through ordinary intake with the parameter values
validated in code (a path value is a single segment token, so
traversal and scope-widening refuse by name), the skeleton
reaching the plan draft as labelled context only, provenance
on the order and in the review packet, and each version's
health folded on read from the orders it produced. The
challenge pass (round 13) then gave the verified patch its
second reader: deterministic rules over the stored records
alone — never the model's account of itself — asking whether
the patch stayed inside the intent the plan and the task text
record (intent creep at symbol level), carried only figures
the evidence carries (single-source figures), and grew no
public surface nobody asked for (unrequested surface) — each
fired rule a stored challenge a reviewer answers with a
recorded disposition before acceptance can land, superseded
with its run and never deleted. Batch work orders (round 14)
then let work arrive as a pile without diluting any of it: a
named submission of gold, ad-hoc, and template items —
validated whole, the first invalid item refusing the batch
with nothing created — whose items are ordinary orders
through ordinary intake, advanced by a sequential driver that
stops at every human gate and approves nothing, under a batch
budget folded from the items' own usage that stops unstarted
items at batch level when it is spent, with cancellation that
marks only what never started and a report that folds the
whole submission into one honest page. Deployment profiles
(round 16) then made the deployment itself a governed record:
naming `hosted` or `hosted-self` binds production rules at
startup (live model, Postgres, key present — the self-hosted
shape also refuses a Baseten endpoint host), enforced when
settings load and reported per rule in readiness, with every
run stamped with the profile it ran under. The MCP surface
(round 17) then opened the shell to machine clients under the
opposite contract to a raw tool bridge: eight stdio tools —
six reads over the console's own folds, plus intake and plan
drafting through service doors that record the channel on the
audit trail — and no tool that approves, executes, or decides,
with decision-shaped calls refused by name and a session leg
that ends with the plan still awaiting a human. What is
deliberately not here yet, by the round map in
`docs/architecture.md`: dual control at the plan gate
(round 18). The Postgres
profile is code-complete behind the store port (SQLite is the local
default; `DATABASE_URL` switches) with live Postgres conformance
scheduled as the release round's gate, as in the sibling blueprints.
Docker files are provided and follow the house pattern; the image
build is part of hands-on testing.

## License

Apache 2.0 — see `LICENSE`.
