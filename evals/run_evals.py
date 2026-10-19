"""Eval gate: the gold task suite, graded end to end.

Layers on the offline model (the labelled scripted model), plus
the live path's code driven through a fake transport replaying
recorded-shape responses (no network, no key):

1. CONSTRUCTION — the suite is gold-by-construction: scripted-edit
   anchors occur exactly once in each broken file, and applying the
   edits reproduces the reference fix byte for byte.
2. GOLD VALIDITY — for every task, the grading tests FAIL on the
   broken state and PASS on the reference-fixed state, run by pytest
   in scratch trees. A task whose tests pass on the broken state
   would grade nothing.
3. END TO END — every task runs through the real service: work order,
   plan, a gate check (execution before approval must refuse), plan
   approval, the sandboxed run, verification on a pristine copy, and
   review acceptance exporting the .patch artifact.
4. PLAN DEPTH (round 3) — a staleness leg (drift inside an approved
   plan's scope refuses the run as stale_plan; a fresh plan runs)
   and an amendment leg (the new version carries its diff, the old
   approval does not carry, the amended plan runs on its own
   approval).
5. TOOL DEPTH (round 4) — a recovery leg: a model whose first edit
   reaches for too broad an anchor is refused anchor_ambiguous as
   data, narrows the anchor, and completes the repair; the refusal
   and the successful edit's anchor digests are on the audit trail
   as stored records.
6. FAILURE RECOVERY (round 5) — an interruption leg: a run whose
   model dies after its first edit lands INTERRUPTED with the last
   checkpoint named, then resumes to a verified success without
   re-applying the landed edit.
7. BUDGETS + COST LEDGER (round 6) — a costing leg: a live-path run
   priced end to end under a price table the eval records the way a
   deployment would (verified numbers, published as a version),
   split plan vs execution; the profile fold prints cost per
   successful task; and an offline run folds as scripted accounting
   — counted, never priced.

Zero-tolerance counters: edits that landed outside the approved
scope, and runs marked SUCCEEDED without a passing verification.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

import open_model_coding_agent.service as service_module  # noqa: E402
from open_model_coding_agent.baseten import HttpResponse  # noqa: E402
from open_model_coding_agent.config import load_settings  # noqa: E402
from open_model_coding_agent.errors import GateError  # noqa: E402
from open_model_coding_agent.gold import GoldSuite  # noqa: E402
from open_model_coding_agent.model_port import (  # noqa: E402
    OfflineScriptedModel,
    PlanDraft,
    PlanStepDraft,
    Step,
)
from open_model_coding_agent.service import CodingService  # noqa: E402
from open_model_coding_agent.tools import SandboxTools  # noqa: E402

_IGNORE = shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc")


def grade_tree(suite: GoldSuite, task, *, fixed: bool, scratch: Path) -> bool:
    """Run a task's grading tests against its broken (or fixed) tree."""
    tree = scratch / f"{task.id}-{'fixed' if fixed else 'broken'}"
    shutil.copytree(REPO_ROOT / "workshop", tree / "workshop", ignore=_IGNORE)
    overlay = task.fixed_files() if fixed else task.broken_files()
    for rel, content in overlay.items():
        dest = tree / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content)
    staged = tree / task.grading_relpath()
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_text(task.grading_source())
    tools = SandboxTools(tree, scope=[], test_timeout=120)
    return tools.run_tests([task.grading_relpath()]).ok


class _FakeTransport:
    """Replays recorded-shape Chat Completions responses. The live
    adapter's code path runs end to end; the network never does."""

    def __init__(self, script):
        self.script = list(script)

    def post(self, url, *, headers, json_body, timeout):
        return self.script.pop(0)


def _chat(content=None, tool_calls=None):
    message: dict = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    body = {
        "choices": [{"message": message}],
        "usage": {"prompt_tokens": 120, "completion_tokens": 40, "total_tokens": 160},
    }
    return HttpResponse(200, json.dumps(body))


def _tool_call(call_id, name, args):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def live_leg(suite: GoldSuite, scratch: Path, check) -> None:
    """One gold task through the Baseten adapter's code path: plan
    over the wire shape, tool calls back, verification in code, and
    the provider's usage recorded against the run."""
    task = suite.get("reserve-overcommit")
    calls = [_tool_call("c-read", "read_file", {"path": task.file_scope[0]})]
    for index, edit in enumerate(task.scripted_edits):
        calls.append(
            _tool_call(
                f"c-edit-{index}",
                "edit_file",
                {"path": edit["path"], "old": edit["old"], "new": edit["new"]},
            )
        )
    script = [
        _chat(
            content=json.dumps(
                {"approach": task.plan_approach, "steps": task.plan_steps}
            )
        ),
        _chat(tool_calls=calls),
        _chat(content="the repair is complete"),
    ]
    # A placeholder key for the leg's duration: the fake transport
    # means it is never sent anywhere, but the live profile rightly
    # refuses to build its adapter without one present.
    os.environ["BASETEN_API_KEY"] = "eval-fake-key"
    try:
        settings = load_settings(
            sqlite_path=str(scratch / "live" / "codingagent.db"),
            targets_dir=scratch / "live" / "targets",
            runs_dir=scratch / "live" / "runs",
            artifacts_dir=scratch / "live" / "artifacts",
            model_profile="baseten-glm",
        )
        settings.model_transport = _FakeTransport(script)
        service = CodingService(settings)
        order = service.create_gold_work_order(task.id, actor="evals")
        wid = order["work_order_id"]
        plan = service.draft_plan(wid, actor="evals")
        check(
            "live path: plan drafted by the adapter",
            plan["model_profile"] == "baseten-glm"
            and plan["files"] == task.file_scope,
            f"profile {plan['model_profile']}; files {plan['files']}",
        )
        service.decide_plan(wid, approve=True, actor="Eval Reviewer")
        run = service.start_run(wid, actor="evals")
    finally:
        del os.environ["BASETEN_API_KEY"]
    check(
        "live path: run verified",
        run["status"] == "succeeded" and run["verification"].get("passed"),
        f"run {run['status']}; {run['verification'].get('reason')}",
    )
    usage = service.store.list_usage(work_order_id=wid)
    api_rows = [u for u in usage if u["source"] == "api"]
    run_rows = [u for u in api_rows if u["run_id"] == run["run_id"]]
    check(
        "live path: usage recorded from the API shape",
        len(api_rows) == len(usage) and len(run_rows) >= 2
        and all(u["model"] == "zai-org/GLM-5" for u in api_rows)
        and run["usage_totals"]["prompt_tokens"] > 0,
        f"{len(api_rows)} api calls recorded ({len(run_rows)} on the run), "
        f"all under zai-org/GLM-5",
    )


def _offline_service(scratch: Path, leg: str) -> CodingService:
    settings = load_settings(
        sqlite_path=str(scratch / leg / "codingagent.db"),
        targets_dir=scratch / leg / "targets",
        runs_dir=scratch / leg / "runs",
        artifacts_dir=scratch / leg / "artifacts",
    )
    return CodingService(settings)


def staleness_leg(suite: GoldSuite, scratch: Path, check) -> None:
    """Round 3: an approved plan is bound to the repo it was approved
    against. Drift inside the scope refuses the run as stale_plan and
    sends the order back to draft; a fresh plan then runs."""
    task = suite.get("tax-before-discount")
    service = _offline_service(scratch, "staleness")
    order = service.create_gold_work_order(task.id, actor="evals")
    wid = order["work_order_id"]
    service.draft_plan(wid, actor="evals")
    service.decide_plan(wid, approve=True, actor="Eval Reviewer")
    target_file = Path(order["target_path"]) / task.file_scope[0]
    target_file.write_text(target_file.read_text() + "\n# someone else was here\n")
    code = None
    try:
        service.start_run(wid, actor="evals")
    except GateError as exc:
        code = exc.code
    check(
        "staleness: drift inside the scope refuses the run",
        code == "stale_plan"
        and service.get_work_order(wid)["status"] == "draft"
        and any(
            a["event"] == "stale_plan_refused" for a in service.audit(wid)
        ),
        f"refusal {code}; order back at draft with the refusal recorded",
    )
    service.draft_plan(wid, actor="evals")
    service.decide_plan(wid, approve=True, actor="Eval Reviewer")
    run = service.start_run(wid, actor="evals")
    check(
        "staleness: a fresh plan runs against the moved repo",
        run["status"] == "succeeded" and run["plan_version"] == 2,
        f"run {run['status']} on plan v{run['plan_version']}",
    )


def amendment_leg(suite: GoldSuite, scratch: Path, check) -> None:
    """Round 3: an amendment is a new version with a recorded diff,
    and the approval binds the version — v1's approval does not carry
    to v2, naming v1 refuses as plan_superseded, and v2 runs only on
    its own approval."""
    task = suite.get("low-stock-report")
    service = _offline_service(scratch, "amendment")
    order = service.create_gold_work_order(task.id, actor="evals")
    wid = order["work_order_id"]
    service.draft_plan(wid, actor="evals")
    service.decide_plan(wid, approve=True, actor="Eval Reviewer")
    grading = task.grading_relpath()
    amended = service.amend_plan(
        wid,
        draft=PlanDraft(
            approach="The same implementation, planned in two steps.",
            steps=[
                PlanStepDraft(
                    intent="Read StockLedger.low_stock and the available() "
                    "read it must be built from.",
                    files=list(task.file_scope),
                    tests=[grading],
                ),
                PlanStepDraft(
                    intent=task.plan_steps[0]["intent"],
                    files=list(task.file_scope),
                    tests=[grading],
                ),
            ],
        ),
        actor="Eval Lead",
    )
    diff = amended["diff_from_previous"] or {}
    check(
        "amendment: the new version carries its recorded diff",
        amended["version"] == 2
        and diff.get("from_version") == 1
        and diff.get("to_version") == 2
        and len(diff.get("steps_added") or []) == 1,
        f"v1 -> v2; steps added {len(diff.get('steps_added') or [])}",
    )
    codes = {}
    for label, kwargs in (("pinned", {"plan_version": 1}), ("plain", {})):
        try:
            service.start_run(wid, actor="evals", **kwargs)
        except GateError as exc:
            codes[label] = exc.code
    check(
        "amendment: the old approval does not carry to the new version",
        codes.get("pinned") == "plan_superseded"
        and codes.get("plain") == "plan_not_approved",
        f"pinned v1: {codes.get('pinned')}; unpinned: {codes.get('plain')}",
    )
    service.decide_plan(wid, approve=True, actor="Eval Reviewer")
    run = service.start_run(wid, actor="evals")
    check(
        "amendment: the amended plan runs on its own approval",
        run["status"] == "succeeded" and run["plan_version"] == 2,
        f"run {run['status']} on plan v{run['plan_version']}",
    )


class _AmbiguousFirstModel(OfflineScriptedModel):
    """The scripted model, but its first edit reaches for an anchor
    ('return') that occurs all over the file: the tool refuses it as
    anchor_ambiguous, and the run must recover by narrowing to the
    repair's real anchor — refusals as data, not retries in the dark."""

    def start_run(self, *, work_order, plan):
        super().start_run(work_order=work_order, plan=plan)
        reads = [s for s in self._queue if s.tool == "read_file"]
        rest = [s for s in self._queue if s.tool != "read_file"]
        target = plan["files"][0]
        self._queue = (
            reads
            + [Step("edit_file", {"path": target, "old": "return", "new": "return"})]
            + rest
        )


def tool_depth_leg(suite: GoldSuite, scratch: Path, check) -> None:
    """Round 4: an ambiguous first edit refuses by name, is recorded
    verbatim on the audit trail, and the run still lands the repair
    with the narrowed anchor."""
    task = suite.get("bulk-tier-boundary")
    original = service_module.make_model
    service_module.make_model = (
        lambda profile_id, scripts=None, **_kw: _AmbiguousFirstModel(
            scripts=scripts
        )
    )
    try:
        service = _offline_service(scratch, "tool-depth")
        order = service.create_gold_work_order(task.id, actor="evals")
        wid = order["work_order_id"]
        service.draft_plan(wid, actor="evals")
        service.decide_plan(wid, approve=True, actor="Eval Reviewer")
        run = service.start_run(wid, actor="evals")
    finally:
        service_module.make_model = original
    check(
        "tool depth: the run recovers and verifies",
        run["status"] == "succeeded"
        and run["verification"].get("passed")
        and run["changed_files"] == task.file_scope,
        f"run {run['status']}; changed {run['changed_files']}",
    )
    calls = [
        a["detail"] for a in service.audit(wid) if a["event"] == "tool_call"
    ]
    edits = [c for c in calls if c["tool"] == "edit_file"]
    refusal = edits[0].get("result") or {}
    landed = edits[1].get("result") or {}
    check(
        "tool depth: the refusal is a stored record with its name",
        edits[0]["refused"] is True
        and refusal.get("code") == "anchor_ambiguous"
        and "anchor occurs" in refusal.get("output", ""),
        f"first edit refused as {refusal.get('code')}",
    )
    detail = landed.get("detail") or {}
    check(
        "tool depth: the landed edit records its anchor and digests",
        landed.get("ok") is True
        and bool(detail.get("anchor_sha256"))
        and detail.get("before_sha256") != detail.get("after_sha256"),
        "anchor + before/after digests on the successful edit's record",
    )


class _DiesAfterFirstEdit(OfflineScriptedModel):
    """The scripted model until its third step request, which raises
    the way a dying process does — after the read and the landed
    edit, before the finish. The dying call consumes nothing."""

    def __init__(self, scripts=None):
        super().__init__(scripts=scripts)
        self._calls = 0

    def next_step(self, *, transcript):
        self._calls += 1
        if self._calls >= 3:
            raise RuntimeError("simulated process death")
        return super().next_step(transcript=transcript)


def recovery_leg(suite: GoldSuite, scratch: Path, check) -> None:
    """Round 5: a gold run dies after its first edit, lands
    INTERRUPTED with its last checkpoint named, and resumes to a
    verified success without re-applying the landed edit."""
    task = suite.get("reserve-overcommit")
    original = service_module.make_model
    service_module.make_model = (
        lambda profile_id, scripts=None, **_kw: _DiesAfterFirstEdit(scripts)
    )
    try:
        service = _offline_service(scratch, "recovery")
        order = service.create_gold_work_order(task.id, actor="evals")
        wid = order["work_order_id"]
        service.draft_plan(wid, actor="evals")
        service.decide_plan(wid, approve=True, actor="Eval Reviewer")
        run = service.start_run(wid, actor="evals")
    finally:
        service_module.make_model = original
    check(
        "recovery: the dead run lands interrupted, checkpoint named",
        run["status"] == "interrupted" and run["last_checkpoint"] is not None,
        f"run {run['status']}; last checkpoint {run['last_checkpoint']}",
    )
    resumed = service.resume_run(wid, run["run_id"], actor="evals")
    check(
        "recovery: the resume completes and verifies",
        resumed["status"] == "succeeded"
        and resumed["verification"].get("passed"),
        f"run {resumed['status']}; {resumed['verification'].get('reason')}",
    )
    edits = [
        a
        for a in service.audit(wid)
        if a["event"] == "tool_call" and a["detail"].get("tool") == "edit_file"
    ]
    check(
        "recovery: the landed edit was not re-applied",
        len(edits) == 1,
        f"edit_file tool calls across both attempts: {len(edits)}",
    )


def cost_leg(suite: GoldSuite, scratch: Path, check) -> None:
    """Round 6: a live-path run priced end to end, and the suite
    fold printed. The eval records its own price table the way a
    deployment does — numbers it stands behind, published as a
    version — because the repo's shipped table deliberately carries
    unverified (null) prices. Usage per response is the recorded
    shape: 120 prompt + 40 completion tokens; at $0.30/$0.90 per
    million, one call costs exactly $0.000072."""
    task = suite.get("reserve-overcommit")
    prices_dir = scratch / "cost" / "prices"
    prices_dir.mkdir(parents=True)
    (prices_dir / "table-v1.json").write_text(
        json.dumps(
            {
                "version": 1,
                "currency": "USD",
                "as_of": "2026-10-19",
                "source": "eval-recorded prices (a deployment's verified table)",
                "prices": {
                    "zai-org/GLM-5": {
                        "input_per_mtok": "0.30",
                        "output_per_mtok": "0.90",
                    }
                },
            }
        )
    )
    calls = [_tool_call("c-read", "read_file", {"path": task.file_scope[0]})]
    for index, edit in enumerate(task.scripted_edits):
        calls.append(
            _tool_call(
                f"c-edit-{index}",
                "edit_file",
                {"path": edit["path"], "old": edit["old"], "new": edit["new"]},
            )
        )
    script = [
        _chat(
            content=json.dumps(
                {"approach": task.plan_approach, "steps": task.plan_steps}
            )
        ),
        _chat(tool_calls=calls),
        _chat(content="the repair is complete"),
    ]
    os.environ["BASETEN_API_KEY"] = "eval-fake-key"
    try:
        settings = load_settings(
            sqlite_path=str(scratch / "cost" / "codingagent.db"),
            targets_dir=scratch / "cost" / "targets",
            runs_dir=scratch / "cost" / "runs",
            artifacts_dir=scratch / "cost" / "artifacts",
            model_profile="baseten-glm",
            prices_dir=prices_dir,
        )
        settings.model_transport = _FakeTransport(script)
        service = CodingService(settings)
        order = service.create_gold_work_order(task.id, actor="evals")
        wid = order["work_order_id"]
        service.draft_plan(wid, actor="evals")
        service.decide_plan(wid, approve=True, actor="Eval Reviewer")
        run = service.start_run(wid, actor="evals")
    finally:
        del os.environ["BASETEN_API_KEY"]
    cost = run["cost"]
    check(
        "cost: the live run is priced, plan split from execution",
        run["status"] == "succeeded"
        and Decimal(cost["plan"]) == Decimal("0.000072")
        and Decimal(cost["execution"]) == Decimal("0.000144")
        and Decimal(cost["total"]) == Decimal("0.000216")
        and cost["price_table_versions"] == [1],
        f"plan ${cost['plan']} + execution ${cost['execution']} "
        f"= ${cost['total']} under price table v1",
    )
    report = service.cost_report()
    (profile,) = [p for p in report["profiles"] if p["profile_id"] == "baseten-glm"]
    check(
        "cost: cost per successful task folds the profile's spend",
        profile["runs"] == 1
        and profile["successful_tasks"] == 1
        and Decimal(profile["cost_per_successful_task"]) == Decimal(cost["total"]),
        f"baseten-glm: {profile['runs']} run(s), "
        f"{profile['successful_tasks']} verified success(es), "
        f"${profile['priced_cost']} priced spend, "
        f"${profile['cost_per_successful_task']} per successful task",
    )
    print(
        f"  suite fold: baseten-glm cost per successful task "
        f"${profile['cost_per_successful_task']} "
        f"(priced tokens {profile['priced_tokens']}, "
        f"unpriced {profile['unpriced_tokens']})"
    )

    offline = _offline_service(scratch, "cost-offline")
    order = offline.create_gold_work_order(task.id, actor="evals")
    wid = order["work_order_id"]
    offline.draft_plan(wid, actor="evals")
    offline.decide_plan(wid, approve=True, actor="Eval Reviewer")
    orun = offline.start_run(wid, actor="evals")
    oreport = offline.cost_report()
    (oprofile,) = [p for p in oreport["profiles"] if p["profile_id"] == "offline"]
    check(
        "cost: offline spend folds as scripted, never priced",
        orun["status"] == "succeeded"
        and Decimal(oprofile["priced_cost"]) == 0
        and oprofile["scripted_chars"] > 0
        and oprofile["successful_tasks"] == 1
        and oprofile["unpriced_reasons"] == ["scripted_accounting"],
        f"offline: {oprofile['scripted_chars']} scripted chars counted, "
        f"$0 priced, {oprofile['successful_tasks']} verified success",
    )


def main() -> int:
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))
        print(f"  {'OK ' if ok else 'FAIL'} {name} — {detail}")

    suite = GoldSuite(REPO_ROOT / "evals" / "gold")
    problems = suite.check_construction()
    check(
        "construction",
        not problems,
        "; ".join(problems) or f"{len(suite.tasks)} tasks, anchors unique, "
        "edits reproduce the reference fixes",
    )

    scratch = Path(tempfile.mkdtemp(prefix="coding-agent-evals-"))
    counters = {"out_of_scope_edits": 0, "unverified_successes": 0}

    for task in suite.tasks:
        broken_ok = grade_tree(suite, task, fixed=False, scratch=scratch)
        fixed_ok = grade_tree(suite, task, fixed=True, scratch=scratch)
        check(
            f"{task.id}: grading bites",
            (not broken_ok) and fixed_ok,
            f"broken tree passes grading: {broken_ok} (want False); "
            f"fixed tree passes: {fixed_ok} (want True)",
        )

        settings = load_settings(
            sqlite_path=str(scratch / task.id / "codingagent.db"),
            targets_dir=scratch / task.id / "targets",
            runs_dir=scratch / task.id / "runs",
            artifacts_dir=scratch / task.id / "artifacts",
        )
        service = CodingService(settings)
        order = service.create_gold_work_order(task.id, actor="evals")
        wid = order["work_order_id"]
        service.draft_plan(wid, actor="evals")
        gate_refused = False
        try:
            service.start_run(wid, actor="evals")
        except GateError as exc:
            gate_refused = exc.code == "plan_not_approved"
        check(
            f"{task.id}: gate holds before approval",
            gate_refused,
            "execution from an unapproved plan refused as plan_not_approved",
        )
        service.decide_plan(wid, approve=True, actor="Eval Reviewer")
        run = service.start_run(wid, actor="evals")
        landed = [f for f in run["changed_files"] if f not in task.file_scope]
        counters["out_of_scope_edits"] += len(landed)
        if run["status"] == "succeeded" and not run["verification"].get("passed"):
            counters["unverified_successes"] += 1
        check(
            f"{task.id}: run verified",
            run["status"] == "succeeded" and run["verification"].get("passed"),
            f"run {run['status']}; {run['verification'].get('reason')}",
        )
        check(
            f"{task.id}: patch in scope",
            not landed and set(run["changed_files"]) <= set(task.file_scope),
            f"changed: {run['changed_files']}",
        )
        final = service.decide_review(wid, accept=True, actor="Eval Reviewer")
        artifact = settings.artifacts_dir / f"{wid}.patch"
        check(
            f"{task.id}: review exports the patch",
            final["status"] == "accepted"
            and artifact.is_file()
            and artifact.read_text() == run["patch"]
            and len(run["patch"]) > 0,
            f"final status {final['status']}; artifact {artifact.name}",
        )

    live_leg(suite, scratch, check)
    staleness_leg(suite, scratch, check)
    amendment_leg(suite, scratch, check)
    tool_depth_leg(suite, scratch, check)
    recovery_leg(suite, scratch, check)
    cost_leg(suite, scratch, check)

    print(f"out-of-scope edits landed: {counters['out_of_scope_edits']} (gate 0)")
    print(f"unverified successes: {counters['unverified_successes']} (gate 0)")
    ok = all(c[1] for c in checks) and all(v == 0 for v in counters.values())
    print(f"EVALS: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
