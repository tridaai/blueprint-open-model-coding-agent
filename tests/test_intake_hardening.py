"""Intake and decision hardening, found driving the API and console
end to end: an ad-hoc order with no target ran against the server's
own working directory (copying .env and the runs directory into the
workspace), scopes outside the repo were accepted at intake, blank
actors recorded decisions under no name, a rejection could close an
order with no reason, and the console rendered the audit trail's
actor unescaped."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from open_model_coding_agent.api import create_app
from open_model_coding_agent.errors import GateError
from open_model_coding_agent.models import WorkOrderIn

REPO_ROOT = Path(__file__).resolve().parents[1]


def _adhoc(target_path: str = "", **overrides) -> WorkOrderIn:
    fields = {
        "title": "Ad-hoc fix",
        "task_text": "Fix the module.",
        "file_scope": ["pkg/m.py"],
        "target_path": target_path,
    }
    fields.update(overrides)
    return WorkOrderIn(**fields)


def _refusal(fn, *args, **kwargs) -> tuple[str, str]:
    with pytest.raises(GateError) as exc:
        fn(*args, **kwargs)
    return exc.value.code, exc.value.message


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "adhoc-repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "m.py").write_text("def f(x):\n    return x\n")
    return root


# -- ad-hoc targets ------------------------------------------------------------


def test_an_adhoc_order_without_a_target_refuses_at_intake(service):
    code, message = _refusal(service.create_work_order, _adhoc(""))
    assert code == "invalid_work_order"
    assert "names no target repo" in message
    assert service.list_work_orders() == []


@pytest.mark.parametrize(
    "target, phrase",
    [
        ("relative/repo", "is relative"),
        ("/no/such/repo/anywhere", "does not exist on disk"),
    ],
)
def test_relative_and_missing_targets_refuse_at_intake(service, target, phrase):
    code, message = _refusal(service.create_work_order, _adhoc(target))
    assert code == "invalid_work_order"
    assert phrase in message


def test_a_target_holding_the_shells_data_refuses(service):
    # The runs directory sits inside the target: a run would copy
    # every run, its own included, into the model's workspace.
    holder = Path(service.settings.runs_dir).parent
    holder.mkdir(parents=True, exist_ok=True)
    code, message = _refusal(service.create_work_order, _adhoc(str(holder)))
    assert code == "invalid_work_order"
    assert "holds the shell's own data directory" in message


def test_the_launch_refuses_a_stored_order_with_an_empty_target(service, repo):
    # A record from before intake checked targets: the run refuses
    # by name and copies nothing.
    order = service.create_work_order(_adhoc(str(repo)))
    wid = order["work_order_id"]
    service.draft_plan(wid, actor="tester")
    stored = service.store.get_work_order(wid)
    stored["target_path"] = ""
    service.store.put_work_order(stored)
    service.decide_plan(wid, approve=True, actor="Reviewer")
    code, _message = _refusal(service.start_run, wid, actor="tester")
    assert code == "target_missing"
    assert not any(Path(service.settings.runs_dir).glob("*/workspace"))


def test_a_clean_adhoc_order_still_lands(service, repo):
    order = service.create_work_order(_adhoc(str(repo)))
    assert order["status"] == "draft"
    assert order["target_path"] == str(repo)


# -- scope and fields -----------------------------------------------------------


@pytest.mark.parametrize(
    "scope",
    [["../outside.py"], ["/etc/passwd"], ["pkg/../../x.py"], ["pkg/*.py"]],
)
def test_scope_entries_outside_the_repo_refuse(service, repo, scope):
    code, message = _refusal(
        service.create_work_order, _adhoc(str(repo), file_scope=scope)
    )
    assert code == "invalid_work_order"
    assert "file_scope entry" in message


def test_grading_paths_outside_the_repo_refuse(service, repo):
    code, message = _refusal(
        service.create_work_order,
        _adhoc(str(repo), grading_tests=["../tests/test_x.py"]),
    )
    assert code == "invalid_work_order"
    assert "grading_tests entry" in message


def test_missing_fields_refuse_by_name(service, repo):
    code, message = _refusal(
        service.create_work_order,
        _adhoc(str(repo), title=" ", task_text="", file_scope=[]),
    )
    assert code == "invalid_work_order"
    assert "title, task_text, file_scope" in message


def test_an_unknown_gold_task_is_named_before_the_shape(service):
    code, _message = _refusal(
        service.create_work_order,
        WorkOrderIn(title="", task_text="", file_scope=[], gold_task_id="nope"),
    )
    assert code == "unknown_gold_task"


def test_template_instantiation_without_a_target_refuses(service):
    code, _message = _refusal(
        service.instantiate_template,
        "guard-clause-fix",
        parameters={"module": "inventory", "behaviour": "x"},
        actor="tester",
    )
    assert code == "invalid_work_order"


# -- review rejection -----------------------------------------------------------


def test_a_rejection_without_a_reason_refuses(service, approved_order):
    wid = approved_order["work_order_id"]
    service.start_run(wid)
    for reason in (None, "", "   "):
        code, _message = _refusal(
            service.decide_review, wid, accept=False, actor="Reviewer",
            reason=reason,
        )
        assert code == "reject_reason_required"
    assert service.get_work_order(wid)["status"] == "succeeded"
    final = service.decide_review(
        wid, accept=False, actor="Reviewer", reason="wrong layer"
    )
    assert final["status"] == "rejected"


# -- the API surface ------------------------------------------------------------


def test_blank_and_padded_actors_through_the_api(settings):
    client = TestClient(create_app(settings))
    wid = client.post(
        "/api/work-orders/from-gold/reserve-overcommit"
    ).json()["work_order_id"]
    assert client.post(f"/api/work-orders/{wid}/plan").status_code == 200
    for actor in ("", "   "):
        blank = client.post(
            f"/api/work-orders/{wid}/plan/decide",
            json={"approve": True, "actor": actor},
        )
        assert blank.status_code == 422
    approved = client.post(
        f"/api/work-orders/{wid}/plan/decide",
        json={"approve": True, "actor": "  Alice  "},
    )
    assert approved.status_code == 200
    assert approved.json()["decided_by"] == "Alice"


def test_the_console_escapes_the_audit_trail():
    html = (
        REPO_ROOT / "src" / "open_model_coding_agent" / "static" / "index.html"
    ).read_text()
    assert "by ${a.actor}" not in html
    assert "by ${esc(a.actor)}" in html
    # The console names who it acts as, rather than one fixed reviewer
    # that dual control could never tell apart from a second one.
    assert 'id="actor"' in html
