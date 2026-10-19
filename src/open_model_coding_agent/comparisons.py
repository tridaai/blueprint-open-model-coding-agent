"""The shortlist comparison report: a pure fold over stored records.

Round 7 gave the shell a benchmark path and a promotion gate.
Round 8 runs the whole shortlist through that path side by side —
every recorded profile, once, over the same benchmark set, under
one comparison actor — and folds the stored benchmark records
into a comparison report: what each profile verified, task by
task; what it spent; what its failures looked like; and where it
stands at the promotion gate, read from the gate's own records.

The fold is pure. It reads the comparison record, the benchmark
records the comparison names, the usage rows those benchmarks'
work orders recorded, the price tables, and the promotion
records — and derives the report from them. Recomputing it
changes nothing: no record is written, no default moves, and a
comparison whose task set has since moved is marked ``stale``
on the report it is read through, never rewritten in place.

There is deliberately no ordering in the report and no verdict.
A comparison presents evidence; it does not crown a winner and
it does not recommend. The promotion gate (round 7) is the only
path that changes the default, and every comparison record
states that on its face, in the note below.
"""

from __future__ import annotations

from .benchmarks import SET_HELD_OUT
from .prices import fold_usage

# Stated on the face of every comparison record and report. The
# wording matters: the comparison is evidence for the gate, and
# nothing in it is a shortcut around the gate.
COMPARISON_NOTE = (
    "Evidence only. This comparison does not rank the shortlist, "
    "does not name a winner, and does not change the default model "
    "profile. The promotion gate is the only path that changes the "
    "default: a profile becomes the default by passing a current "
    "held-out evaluation, judged by an actor other than the "
    "evaluator, with no regression against the incumbent default's "
    "own recorded baseline."
)


def promotion_standing(
    profile_id: str,
    evaluations: list[dict],
    *,
    held_out_hash: str,
    default_profile_id: str,
) -> dict:
    """A profile's standing at the promotion gate, read from the
    gate's own records: the state of its latest held-out
    evaluation — ``none`` (never evaluated), ``passed``,
    ``failed`` (a zero-tolerance violation is on its record), or
    ``stale`` (the evaluation was recorded against a held-out set
    that has since moved) — and whether the profile is the
    incumbent default right now."""
    held_out = [
        e
        for e in evaluations
        if e["profile_id"] == profile_id and e["set_name"] == SET_HELD_OUT
    ]
    latest = held_out[-1] if held_out else None
    if latest is None:
        state = "none"
    elif latest["task_set_hash"] != held_out_hash:
        state = "stale"
    elif latest["passed"]:
        state = "passed"
    else:
        state = "failed"
    return {
        "evaluation_state": state,
        "evaluation_id": latest["evaluation_id"] if latest else None,
        "worst_successes": latest["worst_successes"] if latest else None,
        "is_default": profile_id == default_profile_id,
    }


def _profile_cost(
    *,
    status: str,
    usage_rows: list[dict],
    price_tables: dict,
    successes: int | None,
) -> dict:
    """One profile's money, folded from its usage rows — each row
    priced under the table version stamped on it, exactly as the
    round-6 ledger folds. Cost per successful task is stated only
    when the fold actually priced usage for the profile's slug:
    an unpriced (or scripted-accounting) fold is labelled with
    its reasons and its cost per success is None — never a zero
    dressed up as a price."""
    fold = fold_usage(usage_rows, price_tables)
    if status == "not_servable":
        pricing = "not_servable"
    elif fold.priced_calls > 0:
        pricing = "priced"
    elif fold.scripted_calls > 0 and fold.unpriced_calls == 0:
        pricing = "scripted"
    else:
        pricing = "unpriced"
    cost_per = None
    if pricing == "priced" and successes:
        cost_per = str(fold.priced_cost / successes)
    return {
        "currency": "USD",
        "pricing": pricing,
        "priced_cost": str(fold.priced_cost),
        "priced_tokens": fold.priced_tokens,
        "priced_calls": fold.priced_calls,
        "unpriced_tokens": fold.unpriced_tokens,
        "unpriced_calls": fold.unpriced_calls,
        "scripted_calls": fold.scripted_calls,
        "scripted_chars": fold.scripted_chars,
        "price_table_versions": fold.price_table_versions,
        "unpriced_reasons": fold.unpriced_reasons,
        "successful_tasks": successes,
        "cost_per_successful_task": cost_per,
    }


def build_report(
    comparison: dict,
    *,
    benchmarks: list[dict],
    usage_rows: list[dict],
    price_tables: dict,
    evaluations: list[dict],
    default_profile_id: str,
    current_set_hash: str,
    held_out_hash: str,
) -> dict:
    """Fold one comparison into its report.

    ``benchmarks`` are the stored benchmark records the comparison
    names; ``usage_rows`` are the usage rows recorded by those
    benchmarks' work orders. Everything in the report derives
    from those records and the promotion records — the fold keeps
    no state of its own and writes nothing.
    """
    by_id = {b["benchmark_id"]: b for b in benchmarks}
    # Usage rows belong to a profile through the work order that
    # recorded them, and the work order belongs to a benchmark
    # through its outcomes — the join is over stored records.
    usage_by_profile: dict[str, list[dict]] = {}
    work_order_profile: dict[str, str] = {}
    for bench in benchmarks:
        for outcome in bench["outcomes"]:
            if outcome.get("work_order_id"):
                work_order_profile[outcome["work_order_id"]] = bench[
                    "profile_id"
                ]
    for row in usage_rows:
        profile_id = work_order_profile.get(row["work_order_id"])
        if profile_id is not None:
            usage_by_profile.setdefault(profile_id, []).append(row)

    tasks = list(comparison["tasks"])
    profiles: list[dict] = []
    for entry in comparison["entries"]:
        profile_id = entry["profile_id"]
        standing = promotion_standing(
            profile_id,
            evaluations,
            held_out_hash=held_out_hash,
            default_profile_id=default_profile_id,
        )
        if entry["status"] == "not_servable":
            profiles.append(
                {
                    "profile_id": profile_id,
                    "model": entry.get("model"),
                    "status": "not_servable",
                    "reason": entry.get("reason"),
                    "benchmark_ids": [],
                    "per_task": [
                        {
                            "task_id": task_id,
                            "verified_successes": 0,
                            "attempts": 0,
                            "failure_classes": ["not_servable"],
                        }
                        for task_id in tasks
                    ],
                    "successes": None,
                    "tasks": len(tasks),
                    "worst_successes": None,
                    "mean_successes": None,
                    "usage": {
                        "calls": 0,
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "total_tokens": 0,
                        "latency_ms": 0,
                    },
                    "cost": _profile_cost(
                        status="not_servable",
                        usage_rows=[],
                        price_tables=price_tables,
                        successes=None,
                    ),
                    "failure_classes": {"not_servable": 1},
                    "violations": 0,
                    "promotion": standing,
                }
            )
            continue
        profile_benches = [
            by_id[bid] for bid in entry["benchmark_ids"] if bid in by_id
        ]
        per_task: list[dict] = []
        failure_classes: dict[str, int] = {}
        violations = 0
        for task_id in tasks:
            verified = 0
            attempts = 0
            classes: list[str] = []
            for bench in profile_benches:
                for outcome in bench["outcomes"]:
                    if outcome["task_id"] != task_id:
                        continue
                    attempts += 1
                    if outcome["verified_success"]:
                        verified += 1
                    if outcome["failure_class"]:
                        classes.append(outcome["failure_class"])
            per_task.append(
                {
                    "task_id": task_id,
                    "verified_successes": verified,
                    "attempts": attempts,
                    "failure_classes": classes,
                }
            )
        for bench in profile_benches:
            for outcome in bench["outcomes"]:
                if outcome["failure_class"]:
                    failure_classes[outcome["failure_class"]] = (
                        failure_classes.get(outcome["failure_class"], 0) + 1
                    )
            violations += bench["violations"]
        # Total usage folds the profile's usage rows — plan drafting
        # and execution alike — the same rows the cost fold prices,
        # so the usage a reader sees is the usage the money came
        # from.
        rows = usage_by_profile.get(profile_id, [])
        usage = {
            "calls": len(rows),
            "prompt_tokens": sum(r["prompt_tokens"] for r in rows),
            "completion_tokens": sum(r["completion_tokens"] for r in rows),
            "latency_ms": sum(r["latency_ms"] for r in rows),
        }
        usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
        successes_per_benchmark = [b["successes"] for b in profile_benches]
        successes = sum(successes_per_benchmark)
        profiles.append(
            {
                "profile_id": profile_id,
                "model": entry.get("model"),
                "status": "benchmarked",
                "reason": None,
                "benchmark_ids": list(entry["benchmark_ids"]),
                "per_task": per_task,
                "successes": successes,
                "tasks": len(tasks) * len(profile_benches),
                "worst_successes": (
                    min(successes_per_benchmark) if successes_per_benchmark else None
                ),
                "mean_successes": (
                    sum(successes_per_benchmark) / len(successes_per_benchmark)
                    if successes_per_benchmark
                    else None
                ),
                "usage": usage,
                "cost": _profile_cost(
                    status="benchmarked",
                    usage_rows=usage_by_profile.get(profile_id, []),
                    price_tables=price_tables,
                    successes=successes,
                ),
                "failure_classes": failure_classes,
                "violations": violations,
                "promotion": standing,
            }
        )
    return {
        "comparison_id": comparison["comparison_id"],
        "set_name": comparison["set_name"],
        "task_set_hash": comparison["task_set_hash"],
        "current_task_set_hash": current_set_hash,
        "stale": comparison["task_set_hash"] != current_set_hash,
        "actor": comparison["actor"],
        "created_at": comparison["created_at"],
        "tasks": tasks,
        "note": comparison.get("note") or COMPARISON_NOTE,
        "default_profile_id": default_profile_id,
        "profiles": profiles,
    }
