"""The benchmark sets: the gold suite, split and hash-checked.

The gold suite is the benchmark a team builds from its own work —
here, the six gold tasks. Round 7 splits it into two named sets
with different jobs:

- ``tuning`` — the set a profile's behaviour may be studied and
  adjusted against: prompts, plan shapes, tool habits.
- ``held-out`` — the set promotion is judged on. Nothing is tuned
  against it; it is run, recorded, and compared. A candidate earns
  the default here or nowhere.

The split is fixed data in this module, and it must partition the
suite: every gold task in exactly one set, no set naming a task
that does not exist. :func:`split_problems` states any violation;
the service refuses to benchmark over a broken split rather than
silently grade a subset.

Comparability is enforced by content, not by name. Each set carries
a **content hash** — SHA-256 over a canonical manifest of its
tasks: the task specs, the broken and fixed overlays, the grading
tests, and (since round 10) the hidden grading tests — a run's
verdict now depends on both sets, so both are part of what the
hash names. Add a task, edit a defect, touch a grading test,
and the hash moves; a benchmark or evaluation recorded under the
old hash no longer describes the set that exists now, and the
promotion gate refuses it as ``evaluation_stale``. Re-running the
benchmark is the only way back — there is no edit that makes an old
result current again.
"""

from __future__ import annotations

import hashlib
import json

from .gold import GoldSuite

SET_TUNING = "tuning"
SET_HELD_OUT = "held-out"

# The fixed split. Held-out is the two later, harder shapes — the
# boundary fix and the two-file feature — and tuning is the four
# original single-file repairs. Nothing in the codebase tunes
# against the held-out tasks: the scripted model's scripts come
# from the suite's construction, and the promotion gate reads only
# held-out records.
BENCHMARK_SETS: dict[str, tuple[str, ...]] = {
    SET_TUNING: (
        "reserve-overcommit",
        "tax-before-discount",
        "low-stock-report",
        "cancel-leaks-reservations",
    ),
    SET_HELD_OUT: (
        "bulk-tier-boundary",
        "order-coupon",
    ),
}

# A promotion evaluation runs the held-out set this many times and
# is scored by its worst run. One good run is weather; the worst of
# three is a claim a deployment can stand behind.
DEFAULT_EVALUATION_RUNS = 3


def split_problems(suite: GoldSuite) -> list[str]:
    """The split must partition the suite. Returns the list of
    violations (empty = the split is sound)."""
    problems: list[str] = []
    known = {t.id for t in suite.tasks}
    assigned: dict[str, str] = {}
    for set_name, task_ids in BENCHMARK_SETS.items():
        for task_id in task_ids:
            if task_id not in known:
                problems.append(
                    f"benchmark set {set_name!r} names unknown gold task "
                    f"{task_id!r}"
                )
            elif task_id in assigned:
                problems.append(
                    f"gold task {task_id!r} is in two benchmark sets: "
                    f"{assigned[task_id]!r} and {set_name!r}"
                )
            else:
                assigned[task_id] = set_name
    for task_id in sorted(known - set(assigned)):
        problems.append(
            f"gold task {task_id!r} is in no benchmark set; the split "
            f"must partition the suite"
        )
    return problems


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def task_set_hash(suite: GoldSuite, set_name: str) -> str:
    """The content hash of one benchmark set: SHA-256 over the
    canonical manifest of its tasks — specs, broken and fixed
    overlays, grading tests. Any change to what the set measures
    moves the hash."""
    manifest: dict = {"set": set_name, "tasks": []}
    for task_id in sorted(BENCHMARK_SETS[set_name]):
        task = suite.get(task_id)
        assert task is not None, f"split names unknown task {task_id!r}"
        manifest["tasks"].append(
            {
                "id": task.id,
                "spec": task.spec,
                "broken": {
                    rel: _sha256(content)
                    for rel, content in sorted(task.broken_files().items())
                },
                "fixed": {
                    rel: _sha256(content)
                    for rel, content in sorted(task.fixed_files().items())
                },
                "grading_file": task.grading_file,
                "grading": _sha256(task.grading_source()),
                "hidden_grading_file": task.hidden_grading_file,
                "hidden_grading": _sha256(task.hidden_grading_source()),
            }
        )
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    return _sha256(canonical)


def normalise_actor(name: str) -> str:
    """An actor's name, normalised for comparisons: case-folded,
    whitespace collapsed. Separation of duties compares names this
    way, so 'Pat Lee', 'pat lee', and '  Pat   Lee ' are one
    person to the gate."""
    return " ".join(str(name).split()).casefold()
