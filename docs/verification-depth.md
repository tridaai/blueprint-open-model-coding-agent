# Verification depth — Round 10

Round 9 proved the grading tests genuinely *run*: isolated, on
JUnit evidence, with none skipped. This round asks the next
question: can a patch game tests that genuinely run? A patch can
pass an honest grading run and still be a dishonest patch — by
editing the tests it is graded by, by special-casing the exact
inputs the visible tests use, by wandering into files nobody
approved, or by changing nothing at all under a plausible diff.
Round 10 adds a defence for each, at the layer that owns the
verdict (`service._verify`, with the pure checks in
`verification.py` and the shape facts in `patch.patch_shape`),
and stores every fact it computes on the verification record.

## The survey

The verification path end to end, as it stood after round 9:

- The run's patch is built from the workspace diff, then
  `_verify` checks, in order: every changed file inside the
  plan's approved scope; at least one changed file; the patch
  applies cleanly to a pristine copy; the grading tests — staged
  by `_stage_grading` into that copy only — pass there under
  round 9's isolation, read from JUnit evidence.
- The plan record carries an ordered step list, and the plan's
  flat file scope is *derived* from its steps by the one shared
  validator. The sandbox's edit scope is that derived list.
- The gold construction proves the suite's plan steps cover
  exactly each task's file scope.

Two consequences the round is built on, stated plainly:

1. **A gold patch that edits a test file, or a file no plan step
   named, is already impossible through the ordinary run path** —
   the tool port refuses edits outside the derived scope, which
   is the code files and nothing else. Both checks are therefore
   defence in depth, implemented at verification anyway (the
   round 7/9 pattern: the record is checked, not argued about),
   and proved there with crafted plans and patches that put the
   surface nominally in scope — the scenario a future regression
   in scope derivation or staging would create.
2. **Nothing stopped an overfit fix.** A patch that special-cases
   the visible tests' inputs passes an honest visible grading.
   That is a real hole, and the hidden re-grade is its fix.

## The four defences

### Test-editing defence — `test_surface_touched`

A changed file is test/grading surface when, by the repo's layout
rules: its basename is a test module (`test_*.py` / `*_test.py`);
or it sits under a `tests` / `test` / `grading` / `hidden_grading`
directory (so a file planted at, or shadowing, a grading path
counts); or its basename is a collection/startup/config file —
`conftest.py`, `sitecustomize.py`, `usercustomize.py`,
`pytest.ini`, `.pytest.ini`, `pyproject.toml`, `setup.cfg`,
`tox.ini`.

Policy, implemented and recorded: **for a gold task, a patch
touching the surface fails verification by name** — the task's
fix never lives in its tests. **For an ad-hoc order** the flag is
recorded with the files named, the audit's
`verification_completed` record carries them, and the console's
review gate surfaces the flag in red above the accept/reject
buttons — but it does not fail the run, because an operator may
legitimately scope work that includes tests; the human at the
review gate is the judge there, and the flag makes sure they
judge with the fact in front of them in red.

### Hidden-test re-grade — `hidden_grading_failed`

Every gold task gains a second grading file, kept in the eval
area (`evals/gold/hidden/<task>/`), asserting behaviour the
visible tests imply but do not state: the same fix's edge cases
on inputs the visible set never uses (other SKUs, other baskets,
the other bulk tier, a full coupon, freed stock being sold
again). Gold-by-construction applies in full: each hidden set
fails on the task's broken state and passes on its reference
fix, proved per task by the eval runner.

The flow: after a run verifies SUCCEEDED on the visible set, the
shell stages the hidden file — only then, and only into the
verification copy, which is the same patched pristine copy the
visible set just passed on — and re-grades under the same round-9
isolation. A hidden failure flips the verdict to FAILED with the
reason `hidden_grading_failed`, naming the hidden file and its
JUnit evidence. The record distinguishes the verdicts:
`visible_verdict` and `hidden_verdict` / `hidden_grading` stand
side by side, so a reader can see a patch that passed everything
it could see and failed what it could not. The hidden file is
never present in a run workspace (asserted in tests and the eval
leg), never staged alongside the visible set, and the benchmark
content hash now covers it, because a run's verdict depends on
it.

The bite case, kept as a regression in both the test pack and
the eval leg: an overfit repair of *reserve-overcommit* that
refuses reservations only for `WDG-1` — the one SKU every visible
test uses. Visible verdict: pass, 3/3. Hidden verdict: fail. Run:
FAILED, `hidden_grading_failed`.

### Scope-creep detection — `unplanned_files`

Verification compares the landed patch against the **approved
plan's steps**, not just its derived file scope: a changed file
no step named is an unplanned file. The comparison
(`planned_files` / `unplanned_files`) and its result are stored
on every verification record, for every run. For gold tasks —
whose plan steps cover exactly the scope — an unplanned-file
patch fails verification by name. For ad-hoc orders the files
are recorded and named (and shown in red in the console) without
failing the run, on the same operator-may-scope-it reasoning as
the test-surface flag.

As the survey states, on any plan the validator accepted the
steps derive the scope, so an unplanned file is also an
out-of-scope file and the tool port refuses the edit first: this
check cannot bite until a record exists whose steps and scope
disagree — which is exactly the class of regression it is there
to catch. Only file-level comparison is in this round's scope;
whether edits inside a named file stray beyond its step's intent
is the challenge pass's question (round 13).

### Patch hygiene — the patch's shape, and `no_substantive_change`

Every verification record now carries `patch_shape`: files
changed, lines added and removed per file and in total, the
binary files among them (judged from the bytes), whether the
patch is empty, and whether it is whitespace-only — every changed
file's patched text identical to its pristine text once all
whitespace is removed.

The naming is now honest about the two ways a patch can amount
to nothing: an empty patch (no changed files) still fails as
"the run produced no patch", because that is what happened; a
whitespace-only patch — a real diff, of trailing spaces and
re-wrapped lines, that changes no behaviour — fails as
`no_substantive_change`, before any grading runs.

## Counters and gates

The eval gate's unverified-success counter now also counts any
SUCCEEDED gold run whose hidden verdict is not a pass; the
benchmark fold applies the same check to its records
(`unverified_success` failure class), in the gate's usual
defence-in-depth mould — verification already makes both
unreachable, and the records are checked anyway.

```
VERIFICATION DEPTH:
  overfit fix (visible pass, hidden fail): flipped to FAILED, hidden_grading_failed
  gold test-surface patch: refused by name (test_surface_touched)
  gold unplanned-file patch: refused by name (unplanned_files)
  whitespace-only patch: refused by name (no_substantive_change)
  hidden sets: 6/6 fail on the broken state, 6/6 pass on the reference fix
  unverified successes (incl. hidden verdict not a pass): 0 (zero tolerance)
```

The test pack is `tests/test_verification_depth.py`; the eval
leg is `verification depth:` in `evals/run_evals.py`, beside the
per-task hidden construction proofs.

Full gates on the final tree: **147 tests passed** (134 before
this round, 13 new); evals **PASS** — 6/6 gold tasks verified end
to end on both grading sets, the four verification-depth checks
OK, and all three zero-tolerance counters at zero.

## What this round deliberately did not do

- The challenge pass (round 13), the review workbench (round 11),
  and any change to promotion or comparison semantics.
- Step-intent creep inside a named file — file-level comparison
  only, by the round's scope.
- Hidden sets for ad-hoc orders. An ad-hoc order's grading set is
  the operator's own tests; there is no suite-side hidden set to
  re-grade against, and the record says so
  (`hidden_verdict: not_applicable`) rather than implying one ran.
