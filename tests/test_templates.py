"""Round 12: the task library.

Templates are governed records: versioned like plans (an edit is
a new version), published by a second person (self-publish
refused on normalised names), and instantiated only from a
published version. Instantiation is ordinary intake with the
parameters validated on the way — a value that lands in a path
is a single path-segment token, so traversal, absolute paths,
and glob escapes refuse by name, and a caller restating a wider
scope than the pattern produces refuses as widening. The order
an instantiation creates is an ordinary order: the template's
substituted skeleton reaches the plan draft as labelled context,
the plan is still drafted and approved per order, and the review
packet carries the provenance. Health is a fold over the orders
instantiated from a version, computed on read.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import open_model_coding_agent.service as service_module
from open_model_coding_agent.api import create_app
from open_model_coding_agent.baseten import HttpResponse
from open_model_coding_agent.config import load_settings
from open_model_coding_agent.errors import GateError
from open_model_coding_agent.model_port import OfflineScriptedModel
from open_model_coding_agent.models import PlanStepIn, TemplateIn
from open_model_coding_agent.service import CodingService

GUARD_PARAMS = {
    "module": "inventory",
    "behaviour": "a reservation can commit more units than are available",
}


def template_body(**overrides) -> TemplateIn:
    body = {
        "name": "ledger-guard",
        "description": "Use when a ledger operation writes state unchecked.",
        "task_text": (
            "In workshop/{module}.py, an operation is missing its "
            "guard: {behaviour}. Add the guard before any state "
            "changes."
        ),
        "parameters": ["module", "behaviour"],
        "scope_pattern": ["workshop/{module}.py"],
        "steps": [
            PlanStepIn(
                intent="Find the unchecked operation in workshop/{module}.py",
                files=["workshop/{module}.py"],
            ),
            PlanStepIn(
                intent="Add the guard in workshop/{module}.py",
                files=["workshop/{module}.py"],
            ),
        ],
        "author": "Pat Lee",
    }
    body.update(overrides)
    return TemplateIn(**body)


def published(service, body=None) -> dict:
    record = service.create_template(body or template_body())
    return service.publish_template(
        record["name"], record["version"], actor="Second Person"
    )


# -- the shipped seeds -----------------------------------------------------


def test_seed_templates_ship_published_through_two_acts(service):
    records = service.list_templates()
    seeds = {r["name"]: r for r in records}
    assert set(seeds) == {"guard-clause-fix", "boundary-fix", "missing-feature"}
    for record in seeds.values():
        assert record["version"] == 1
        assert record["status"] == "published"
        # Separation of duties held for the seeds too: two actors.
        assert record["author"] == "Blueprint Seed"
        assert record["published_by"] == "Blueprint Seed Review"
    audit = service.template_audit("guard-clause-fix")
    assert [a["event"] for a in audit] == [
        "template_created",
        "template_published",
    ]
    assert audit[0]["actor"] == "Blueprint Seed"
    assert audit[1]["actor"] == "Blueprint Seed Review"
    assert audit[1]["detail"]["version"] == 1


# -- creation and versioning -------------------------------------------------


def test_create_template_versions_are_new_records(service):
    first = service.create_template(template_body())
    assert first["version"] == 1 and first["status"] == "draft"
    second = service.create_template(
        template_body(task_text="Changed wording for {module}: {behaviour}.")
    )
    assert second["version"] == 2
    # The first version is untouched by the second's creation.
    reread = service.get_template("ledger-guard", 1)
    assert "missing its guard" in reread["task_text"]
    assert service.get_template("ledger-guard")["version"] == 2


def test_create_template_shape_refusals(service):
    with pytest.raises(GateError) as exc:
        service.create_template(template_body(name="Not A Name"))
    assert exc.value.code == "template_name_invalid"

    # A declared parameter the patterns never use.
    with pytest.raises(GateError) as exc:
        service.create_template(
            template_body(parameters=["module", "behaviour", "unused"])
        )
    assert exc.value.code == "template_parameters_mismatch"

    # A placeholder used but never declared.
    with pytest.raises(GateError) as exc:
        service.create_template(
            template_body(task_text="Fix {module} and {other}: {behaviour}.")
        )
    assert exc.value.code == "template_parameters_mismatch"

    # A scope pattern that walks out of the repo.
    with pytest.raises(GateError) as exc:
        service.create_template(
            template_body(scope_pattern=["../workshop/{module}.py"])
        )
    assert exc.value.code == "template_scope_pattern_invalid"

    # A skeleton step naming a file the scope pattern does not.
    with pytest.raises(GateError) as exc:
        service.create_template(
            template_body(
                steps=[
                    PlanStepIn(
                        intent="Touch something else",
                        files=["workshop/orders.py"],
                    )
                ]
            )
        )
    assert exc.value.code == "template_steps_invalid"


# -- publishing ---------------------------------------------------------------


def test_publish_refuses_the_author_on_normalised_names(service):
    record = service.create_template(template_body())
    with pytest.raises(GateError) as exc:
        service.publish_template("ledger-guard", 1, actor="  pat   LEE ")
    assert exc.value.code == "template_self_publish"

    done = service.publish_template("ledger-guard", 1, actor="Second Person")
    assert done["status"] == "published"
    assert done["published_by"] == "Second Person"

    with pytest.raises(GateError) as exc:
        service.publish_template("ledger-guard", 1, actor="Third Person")
    assert exc.value.code == "template_already_published"

    with pytest.raises(GateError) as exc:
        service.publish_template("no-such", 1, actor="Second Person")
    assert exc.value.code == "unknown_template"
    assert record["author"] == "Pat Lee"


def test_unpublished_version_is_readable_but_not_instantiable(service):
    service.create_template(template_body())
    record = service.get_template("ledger-guard", 1)
    assert record is not None and record["status"] == "draft"
    with pytest.raises(GateError) as exc:
        service.instantiate_template(
            "ledger-guard", parameters=GUARD_PARAMS, actor="tester"
        )
    assert exc.value.code == "template_not_published"


def test_version_resolution_latest_published_wins(service):
    published(service)  # v1 published
    second = service.create_template(
        template_body(task_text="Second wording for {module}: {behaviour}.")
    )
    service.publish_template("ledger-guard", 2, actor="Second Person")
    service.create_template(
        template_body(task_text="Third wording for {module}: {behaviour}.")
    )  # v3 stays a draft

    order = service.instantiate_template(
        "ledger-guard", parameters=GUARD_PARAMS, actor="tester"
    )
    assert order["template"]["version"] == 2
    assert order["task_text"].startswith("Second wording")

    older = service.instantiate_template(
        "ledger-guard", version=1, parameters=GUARD_PARAMS, actor="tester"
    )
    assert older["template"]["version"] == 1

    with pytest.raises(GateError) as exc:
        service.instantiate_template(
            "ledger-guard", version=3, parameters=GUARD_PARAMS, actor="tester"
        )
    assert exc.value.code == "template_not_published"
    with pytest.raises(GateError) as exc:
        service.instantiate_template(
            "ledger-guard", version=9, parameters=GUARD_PARAMS, actor="tester"
        )
    assert exc.value.code == "unknown_template_version"
    with pytest.raises(GateError) as exc:
        service.instantiate_template(
            "no-such", parameters=GUARD_PARAMS, actor="tester"
        )
    assert exc.value.code == "unknown_template"
    assert second["version"] == 2


# -- parameter validation at intake --------------------------------------------


def test_parameter_values_are_validated_at_intake(service):
    published(service)
    with pytest.raises(GateError) as exc:
        service.instantiate_template(
            "ledger-guard", parameters={"module": "inventory"}, actor="tester"
        )
    assert exc.value.code == "template_parameter_missing"

    with pytest.raises(GateError) as exc:
        service.instantiate_template(
            "ledger-guard",
            parameters={**GUARD_PARAMS, "extra": "x"},
            actor="tester",
        )
    assert exc.value.code == "template_parameter_unknown"

    # Values that land in the scope pattern are path tokens: each
    # of these tries to redraw the pattern and refuses by name.
    for bad in ("../inventory", "a/b", "/inventory", "inv*", "..", "in ventory"):
        with pytest.raises(GateError) as exc:
            service.instantiate_template(
                "ledger-guard",
                parameters={**GUARD_PARAMS, "module": bad},
                actor="tester",
            )
        assert exc.value.code == "template_parameter_invalid", bad

    # A prose value may not masquerade as a placeholder.
    with pytest.raises(GateError) as exc:
        service.instantiate_template(
            "ledger-guard",
            parameters={**GUARD_PARAMS, "behaviour": "see {module}"},
            actor="tester",
        )
    assert exc.value.code == "template_parameter_invalid"


def test_a_restated_wider_scope_refuses_as_widening(service):
    published(service)
    with pytest.raises(GateError) as exc:
        service.instantiate_template(
            "ledger-guard",
            parameters=GUARD_PARAMS,
            actor="tester",
            file_scope=["workshop/inventory.py", "workshop/orders.py"],
        )
    assert exc.value.code == "template_scope_widening_refused"
    # Restating exactly the produced scope is accepted.
    order = service.instantiate_template(
        "ledger-guard",
        parameters=GUARD_PARAMS,
        actor="tester",
        file_scope=["workshop/inventory.py"],
    )
    assert order["file_scope"] == ["workshop/inventory.py"]


# -- instantiation: provenance, substitution, ordinary gates -------------------


def test_instantiation_substitutes_and_records_provenance(service):
    order = service.instantiate_template(
        "guard-clause-fix",
        parameters=GUARD_PARAMS,
        actor="tester",
        gold_task_id="reserve-overcommit",
    )
    wid = order["work_order_id"]
    assert order["file_scope"] == ["workshop/inventory.py"]
    assert "workshop/inventory.py" in order["task_text"]
    assert GUARD_PARAMS["behaviour"] in order["task_text"]
    provenance = order["template"]
    assert provenance["name"] == "guard-clause-fix"
    assert provenance["version"] == 1
    assert provenance["parameters"] == GUARD_PARAMS
    assert provenance["steps"][0]["files"] == ["workshop/inventory.py"]

    # Both trails name the act: the template's, and the order's.
    template_events = [
        (a["event"], a["detail"].get("work_order_id"))
        for a in service.template_audit("guard-clause-fix")
    ]
    assert ("template_instantiated", wid) in template_events
    created = [
        a for a in service.audit(wid) if a["event"] == "work_order_created"
    ]
    assert created[0]["detail"]["template"] == {
        "name": "guard-clause-fix",
        "version": 1,
    }

    # The packet a reviewer reads carries the provenance.
    packet = service.review_packet(wid)
    assert packet["template"]["name"] == "guard-clause-fix"
    detail = service.work_order_detail(wid)
    assert detail["work_order"]["template"]["version"] == 1


def test_instantiation_creates_no_plan_the_gates_still_stand(service):
    order = service.instantiate_template(
        "guard-clause-fix",
        parameters=GUARD_PARAMS,
        actor="tester",
        gold_task_id="reserve-overcommit",
    )
    wid = order["work_order_id"]
    assert order["status"] == "draft"
    # A template suggests a skeleton; it does not smuggle a plan.
    assert service.store.get_plan(wid) is None
    with pytest.raises(GateError) as exc:
        service.start_run(wid, actor="tester")
    assert exc.value.code == "plan_not_approved"

    plan = service.draft_plan(wid, actor="tester")
    assert plan["template_context"] == {
        "name": "guard-clause-fix",
        "version": 1,
    }
    service.decide_plan(wid, approve=True, actor="Reviewer")
    run = service.start_run(wid, actor="tester")
    assert run["status"] == "succeeded"
    assert run["verification"]["passed"] is True


def test_the_skeleton_reaches_an_adhoc_draft_as_context(service, tmp_path):
    repo = tmp_path / "repo"
    (repo / "workshop").mkdir(parents=True)
    (repo / "workshop" / "inventory.py").write_text("X = 1\n")
    published(service)
    order = service.instantiate_template(
        "ledger-guard",
        parameters=GUARD_PARAMS,
        actor="tester",
        target_path=str(repo),
    )
    plan = service.draft_plan(order["work_order_id"], actor="tester")
    # No gold steps compete here: the scripted draft is the
    # substituted skeleton, labelled with where it came from.
    assert [s["intent"] for s in plan["steps"]] == [
        "Find the unchecked operation in workshop/inventory.py",
        "Add the guard in workshop/inventory.py",
    ]
    assert plan["template_context"] == {"name": "ledger-guard", "version": 1}
    # And it is a draft like any other: unapproved, it runs nothing.
    with pytest.raises(GateError) as exc:
        service.start_run(order["work_order_id"], actor="tester")
    assert exc.value.code == "plan_not_approved"


# -- health: a fold over the instantiated orders -------------------------------


def _gold_journey(service, *, accept: bool | None):
    order = service.instantiate_template(
        "guard-clause-fix",
        parameters=GUARD_PARAMS,
        actor="tester",
        gold_task_id="reserve-overcommit",
    )
    wid = order["work_order_id"]
    service.draft_plan(wid, actor="tester")
    service.decide_plan(wid, approve=True, actor="Reviewer")
    run = service.start_run(wid, actor="tester")
    if accept is not None:
        service.decide_review(
            wid,
            accept=accept,
            actor="Reviewer",
            reason=None if accept else "not the shape",
        )
    return wid, run


def test_health_folds_orders_runs_and_reviews(service, monkeypatch):
    _gold_journey(service, accept=True)
    _gold_journey(service, accept=False)

    # A third order whose run fails verification: the agent's
    # edit does not fix the defect, and the fold names the class.
    order = service.instantiate_template(
        "guard-clause-fix",
        parameters=GUARD_PARAMS,
        actor="tester",
        gold_task_id="reserve-overcommit",
    )
    wid = order["work_order_id"]
    service.draft_plan(wid, actor="tester")
    service.decide_plan(wid, approve=True, actor="Reviewer")
    bad_script = {
        "reserve-overcommit": [
            {
                "path": "workshop/inventory.py",
                "old": "        self._reserved[sku] = self._reserved.get(sku, 0) + qty\n        return True",
                "new": "        self._reserved[sku] = self._reserved.get(sku, 0) + qty\n        return True  # looks fine",
            }
        ]
    }
    monkeypatch.setattr(
        service_module,
        "make_model",
        lambda profile_id, scripts=None, **_kw: OfflineScriptedModel(
            scripts=bad_script
        ),
    )
    failed = service.start_run(wid, actor="tester")
    assert failed["status"] == "failed"

    health = service.template_health("guard-clause-fix", 1)
    assert health["orders_created"] == 3
    assert health["runs"] == 3
    assert health["runs_succeeded"] == 2
    assert health["failures_by_class"] == {"verification_failed": 1}
    assert health["reviews"] == {
        "accepted": 1,
        "rejected": 1,
        "changes_requested": 0,
    }
    assert health["acceptance_rate"] == 0.5
    # Offline usage is never priced: counted, kept out of the money.
    assert health["priced_successes"] == 0
    assert health["median_cost_per_success"] is None
    # Another version's fold is untouched by this one's orders.
    other = service.template_health("boundary-fix", 1)
    assert other["orders_created"] == 0 and other["runs"] == 0


# -- priced health over the live path ------------------------------------------


def _write_table(prices_dir: Path, version: int, prices: dict) -> None:
    prices_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "version": version,
        "currency": "USD",
        "as_of": "2026-10-19",
        "source": "test-recorded prices",
        "prices": prices,
    }
    (prices_dir / f"table-v{version}.json").write_text(json.dumps(record))


class _FakeTransport:
    def __init__(self, script):
        self.script = list(script)
        self.requests: list[dict] = []

    def post(self, url, *, headers, json_body, timeout):
        self.requests.append({"url": url, "json": json_body})
        return self.script.pop(0)


def _chat(content=None, tool_calls=None, usage=(120, 40)):
    message: dict = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    body = {
        "choices": [{"message": message}],
        "usage": {
            "prompt_tokens": usage[0],
            "completion_tokens": usage[1],
            "total_tokens": usage[0] + usage[1],
        },
    }
    return HttpResponse(200, json.dumps(body))


def _tool_call(call_id, name, args):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def _gold_script(service):
    task = service.gold.get("reserve-overcommit")
    calls = [_tool_call("c-read", "read_file", {"path": task.file_scope[0]})]
    for index, edit in enumerate(task.scripted_edits):
        calls.append(
            _tool_call(
                f"c-edit-{index}",
                "edit_file",
                {"path": edit["path"], "old": edit["old"], "new": edit["new"]},
            )
        )
    return [
        _chat(
            content=json.dumps(
                {"approach": task.plan_approach, "steps": task.plan_steps}
            )
        ),
        _chat(tool_calls=calls),
        _chat(content="the work is done"),
    ]


def test_health_median_priced_cost_over_live_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("BASETEN_API_KEY", "test-key")
    for name in ("BASETEN_MODEL", "BASETEN_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    prices = tmp_path / "prices"
    _write_table(
        prices, 1, {"zai-org/GLM-5.2": {"input_per_mtok": "0.30", "output_per_mtok": "0.90"}}
    )
    settings = load_settings(
        sqlite_path=str(tmp_path / "db.sqlite"),
        targets_dir=tmp_path / "targets",
        runs_dir=tmp_path / "runs",
        artifacts_dir=tmp_path / "artifacts",
        model_profile="baseten-glm",
        prices_dir=prices,
    )
    service = CodingService(settings)
    transport = _FakeTransport(_gold_script(service) + _gold_script(service))
    settings.model_transport = transport

    for _ in range(2):
        order = service.instantiate_template(
            "guard-clause-fix",
            parameters=GUARD_PARAMS,
            actor="tester",
            gold_task_id="reserve-overcommit",
        )
        wid = order["work_order_id"]
        service.draft_plan(wid, actor="tester")
        service.decide_plan(wid, approve=True, actor="Reviewer")
        run = service.start_run(wid, actor="tester")
        assert run["status"] == "succeeded"

    # The live planner was shown the skeleton, labelled as the
    # template's — draft context, in the first request it made.
    first_prompt = json.dumps(transport.requests[0]["json"])
    assert "guard-clause-fix" in first_prompt
    assert "draft context" in first_prompt

    health = service.template_health("guard-clause-fix", 1)
    assert health["orders_created"] == 2
    assert health["runs_succeeded"] == 2
    assert health["priced_successes"] == 2
    # Each run: 3 calls at (120 in, 40 out) priced 0.30/0.90 per
    # million = 3 x $0.000072; the median of two equal costs is it.
    assert Decimal(health["median_cost_per_success"]) == Decimal("0.000216")


# -- the API surface ------------------------------------------------------------


def test_templates_api_round_trip(settings):
    client = TestClient(create_app(settings))
    listing = client.get("/api/templates")
    assert listing.status_code == 200
    names = {r["name"] for r in listing.json()}
    assert {"guard-clause-fix", "boundary-fix", "missing-feature"} <= names

    created = client.post(
        "/api/templates",
        json={
            "name": "api-shape",
            "description": "Made through the API for this test.",
            "task_text": "Fix {module}: {behaviour}.",
            "parameters": ["module", "behaviour"],
            "scope_pattern": ["workshop/{module}.py"],
            "steps": [
                {"intent": "Fix it in workshop/{module}.py",
                 "files": ["workshop/{module}.py"]}
            ],
            "author": "Api Author",
        },
    )
    assert created.status_code == 201
    assert created.json()["status"] == "draft"

    refused = client.post(
        "/api/templates/api-shape/versions/1/publish",
        json={"actor": "api author"},
    )
    assert refused.status_code == 409
    assert refused.json()["code"] == "template_self_publish"

    published = client.post(
        "/api/templates/api-shape/versions/1/publish",
        json={"actor": "Api Reviewer"},
    )
    assert published.status_code == 200

    missing = client.post(
        "/api/templates/api-shape/instantiate",
        json={"parameters": {"module": "inventory"}, "actor": "tester"},
    )
    assert missing.status_code == 409
    assert missing.json()["code"] == "template_parameter_missing"

    order = client.post(
        "/api/templates/api-shape/instantiate",
        json={"parameters": GUARD_PARAMS, "actor": "tester"},
    )
    assert order.status_code == 201
    assert order.json()["template"]["name"] == "api-shape"

    one = client.get("/api/templates/api-shape")
    assert one.status_code == 200
    assert one.json()["health"]["orders_created"] == 1
    audit = client.get("/api/templates/api-shape/audit")
    assert [a["event"] for a in audit.json()] == [
        "template_created",
        "template_published",
        "template_instantiated",
    ]
    assert client.get("/api/templates/nope").status_code == 404
