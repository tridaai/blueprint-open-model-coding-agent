"""Round 19: the skills library.

Skills are governed, versioned method bundles kept as files and
loaded fail-closed; a version mounts only once the skill gate
has published it — on a benchmark evaluation of its exact
content hash, judged by a second actor — and the plan record
stamps the mounts it was drafted with. These tests stay at the
loader, gate, and fold level: the one mounted end-to-end run
lives in the eval leg, where the full gate is driven for real.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from open_model_coding_agent.api import create_app
from open_model_coding_agent.baseten import BasetenModel, HttpResponse
from open_model_coding_agent.config import REPO_ROOT, load_profile, load_settings
from open_model_coding_agent.errors import ConfigError, GateError
from open_model_coding_agent.model_port import OfflineScriptedModel
from open_model_coding_agent.service import CodingService
from open_model_coding_agent.skills import (
    content_hash,
    load_skill_library,
    write_skill,
)

AUTHOR = "Skill Author"
EVALUATOR = "Skill Evaluator"
PUBLISHER = "Skill Publisher"

V1_TEXT = (
    "# Guard-first repair\n\n"
    "When a guard is missing, add it at the boundary the failure "
    "names, before any other work.\n"
)
V2_TEXT = (
    "# Guard-first repair\n\n"
    "When a guard is missing, add it at the boundary the failure "
    "names, then read the callers the guard now turns away.\n"
)


@pytest.fixture()
def library_dir(tmp_path):
    """A scratch library: one skill, two versions, one author."""
    directory = tmp_path / "skills"
    write_skill(
        directory,
        name="guard-first-repair",
        description="Add the missing guard at the boundary first.",
        author=AUTHOR,
        content=V1_TEXT,
    )
    write_skill(
        directory,
        name="guard-first-repair",
        description="Add the missing guard at the boundary first.",
        author=AUTHOR,
        content=V2_TEXT,
    )
    return directory


@pytest.fixture()
def settings(tmp_path, library_dir):
    return load_settings(
        sqlite_path=str(tmp_path / "db.sqlite"),
        targets_dir=tmp_path / "targets",
        runs_dir=tmp_path / "runs",
        artifacts_dir=tmp_path / "artifacts",
        skills_dir=library_dir,
    )


@pytest.fixture()
def service(settings):
    return CodingService(settings)


_EVALUATION_SEQ = [0]


def record_evaluation(
    service,
    name="guard-first-repair",
    version=1,
    *,
    actor=EVALUATOR,
    passed=True,
    candidate_worst=4,
    baseline_worst=4,
    violations=0,
    hash_override=None,
):
    """Stand an evaluation on the store, in the shape the real
    evaluation act writes — the gate's judgement is what is under
    test here; the paired runs themselves are the eval leg's."""
    library = load_skill_library(service.settings.skills_dir)
    found = library.get_version(name, version)
    _EVALUATION_SEQ[0] += 1
    record = {
        "evaluation_id": f"sev-test-{version}-{actor}",
        "name": name,
        "version": version,
        "content_hash": hash_override or found.content_hash,
        "set_name": "tuning",
        "task_set_hash": "test-hash",
        "actor": actor,
        "times": 1,
        "profile_id": "offline",
        "baseline": {
            "successes_per_pass": [baseline_worst],
            "worst_successes": baseline_worst,
            "outcomes": [],
        },
        "candidate": {
            "successes_per_pass": [candidate_worst],
            "worst_successes": candidate_worst,
            "outcomes": [],
        },
        "violations": violations,
        "passed": passed,
        "created_at": f"2026-10-20T00:00:{_EVALUATION_SEQ[0]:02d}+00:00",
    }
    service.store.append_skill_evaluation(record)
    return record


def publish(service, version=1, **evaluation_kwargs):
    record_evaluation(service, version=version, **evaluation_kwargs)
    return service.publish_skill(
        "guard-first-repair", version, actor=PUBLISHER
    )


def refusal_code(fn) -> str | None:
    try:
        fn()
    except GateError as exc:
        return exc.code
    return None


# -- the loader ---------------------------------------------------------------


def test_the_shipped_library_loads_with_its_seed_skills():
    library = load_skill_library(REPO_ROOT / "skills")
    assert library.names() == [
        "minimal-diff-discipline",
        "read-the-callers-first",
        "test-first-repair",
    ]
    for name in library.names():
        record = library.get(name)
        for version in record.versions.values():
            assert version.author == "Blueprint Seed"
            assert version.content_hash == content_hash(version.content)


def test_the_loader_refuses_a_content_hash_drift(library_dir):
    target = library_dir / "guard-first-repair" / "v1.md"
    target.write_text(V1_TEXT + "One quiet extra line.\n")
    with pytest.raises(ConfigError, match="content hash"):
        load_skill_library(library_dir)


def test_the_loader_refuses_an_unknown_index_field(library_dir):
    index_path = library_dir / "index.json"
    index = json.loads(index_path.read_text())
    index["skills"][0]["versions"][0]["weight"] = 3
    index_path.write_text(json.dumps(index))
    with pytest.raises(ConfigError, match="unknown field"):
        load_skill_library(library_dir)


def test_the_loader_refuses_a_version_the_index_does_not_carry(library_dir):
    index_path = library_dir / "index.json"
    index = json.loads(index_path.read_text())
    index["skills"][0]["versions"] = [
        v for v in index["skills"][0]["versions"] if v["version"] != 2
    ]
    index_path.write_text(json.dumps(index))
    library = load_skill_library(library_dir)
    assert sorted(library.get("guard-first-repair").versions) == [1]


def test_the_loader_refuses_a_missing_content_file(library_dir):
    (library_dir / "guard-first-repair" / "v2.md").unlink()
    with pytest.raises(ConfigError, match="not in the library"):
        load_skill_library(library_dir)


def test_the_loader_refuses_a_file_the_layout_does_not_name(library_dir):
    index_path = library_dir / "index.json"
    index = json.loads(index_path.read_text())
    index["skills"][0]["versions"][0]["file"] = "elsewhere/v1.md"
    index_path.write_text(json.dumps(index))
    with pytest.raises(ConfigError, match="the layout carries it"):
        load_skill_library(library_dir)


def test_the_loader_refuses_a_directory_with_no_index(tmp_path):
    with pytest.raises(ConfigError, match="no skill index"):
        load_skill_library(tmp_path / "empty")


def test_write_skill_assigns_the_next_version(tmp_path):
    directory = tmp_path / "skills"
    first = write_skill(
        directory, name="new-method", description="A method.",
        author=AUTHOR, content="# One\n",
    )
    second = write_skill(
        directory, name="new-method", description="A method.",
        author=AUTHOR, content="# Two\n",
    )
    assert (first["version"], second["version"]) == (1, 2)
    library = load_skill_library(directory)
    assert library.get_version("new-method", 2).content == "# Two\n"


# -- the publish gate -----------------------------------------------------------


def test_publish_refuses_without_an_evaluation(service):
    assert refusal_code(
        lambda: service.publish_skill(
            "guard-first-repair", 1, actor=PUBLISHER
        )
    ) == "no_passing_evaluation"


def test_publish_refuses_a_stale_evaluation(service):
    record_evaluation(service, hash_override="0" * 64)
    assert refusal_code(
        lambda: service.publish_skill(
            "guard-first-repair", 1, actor=PUBLISHER
        )
    ) == "evaluation_stale"


def test_publish_refuses_an_evaluation_that_did_not_pass(service):
    record_evaluation(service, passed=False, violations=1)
    assert refusal_code(
        lambda: service.publish_skill(
            "guard-first-repair", 1, actor=PUBLISHER
        )
    ) == "no_passing_evaluation"


def test_publish_refuses_the_author(service):
    record_evaluation(service)
    assert refusal_code(
        lambda: service.publish_skill(
            "guard-first-repair", 1, actor=" skill author "
        )
    ) == "skill_self_publish"


def test_publish_refuses_the_evaluator(service):
    record_evaluation(service)
    assert refusal_code(
        lambda: service.publish_skill(
            "guard-first-repair", 1, actor=EVALUATOR
        )
    ) == "skill_evaluator_publish"


def test_publish_refuses_a_regression(service):
    record_evaluation(service, candidate_worst=3, baseline_worst=4)
    assert refusal_code(
        lambda: service.publish_skill(
            "guard-first-repair", 1, actor=PUBLISHER
        )
    ) == "regression"


def test_publish_judges_only_the_latest_evaluation(service):
    record_evaluation(service, passed=False, violations=2)
    record_evaluation(service, actor="Second Evaluator")
    record = service.publish_skill(
        "guard-first-repair", 1, actor=PUBLISHER
    )
    assert record["status"] == "published"
    assert record["published_by"] == PUBLISHER


def test_an_equal_score_publishes_and_a_second_publish_refuses(service):
    record = publish(service, candidate_worst=4, baseline_worst=4)
    assert record["status"] == "published"
    assert refusal_code(
        lambda: service.publish_skill(
            "guard-first-repair", 1, actor="Another Publisher"
        )
    ) == "skill_already_published"


def test_publish_refuses_unknown_names_by_name(service):
    assert refusal_code(
        lambda: service.publish_skill("nope", 1, actor=PUBLISHER)
    ) == "unknown_skill"
    assert refusal_code(
        lambda: service.publish_skill(
            "guard-first-repair", 9, actor=PUBLISHER
        )
    ) == "unknown_skill_version"


# -- rollback -------------------------------------------------------------------


def test_rollback_restores_the_previous_published_version(service):
    publish(service, version=1)
    publish(service, version=2)
    assert service._resolve_skill_mount(
        "guard-first-repair", None, allow_unpublished=False
    )["version"] == 2
    record = service.rollback_skill("guard-first-repair", actor=PUBLISHER)
    assert record["version"] == 1 and record["rolled_back_from"] == 2
    assert service.get_skill("guard-first-repair", 2)["status"] == "rolled_back"
    assert service._resolve_skill_mount(
        "guard-first-repair", None, allow_unpublished=False
    )["version"] == 1


def test_rollback_refuses_at_the_floor(service):
    assert refusal_code(
        lambda: service.rollback_skill("guard-first-repair", actor=PUBLISHER)
    ) == "skill_not_published"
    publish(service, version=1)
    assert refusal_code(
        lambda: service.rollback_skill("guard-first-repair", actor=PUBLISHER)
    ) == "skill_rollback_unavailable"


# -- mounting -------------------------------------------------------------------


def test_mount_refusals_name_themselves(service):
    def draft_with(mounts):
        order = service.create_gold_work_order(
            "reserve-overcommit", actor="Tester", skill_mounts=mounts
        )
        return lambda: service.draft_plan(
            order["work_order_id"], actor="Tester"
        )

    assert refusal_code(draft_with([{"name": "nope"}])) == "unknown_skill"
    assert refusal_code(
        draft_with([{"name": "guard-first-repair", "version": 9}])
    ) == "unknown_skill_version"
    assert refusal_code(
        draft_with([{"name": "guard-first-repair", "version": 1}])
    ) == "skill_not_published"
    assert refusal_code(
        draft_with([{"name": "guard-first-repair"}])
    ) == "skill_not_published"


def test_a_rolled_back_version_no_longer_mounts(service):
    publish(service, version=1)
    publish(service, version=2)
    service.rollback_skill("guard-first-repair", actor=PUBLISHER)
    order = service.create_gold_work_order(
        "reserve-overcommit",
        actor="Tester",
        skill_mounts=[{"name": "guard-first-repair", "version": 2}],
    )
    assert refusal_code(
        lambda: service.draft_plan(order["work_order_id"], actor="Tester")
    ) == "skill_not_published"


def test_the_plan_record_stamps_the_mount_and_its_hash(service):
    record = publish(service, version=1)
    order = service.create_gold_work_order(
        "reserve-overcommit",
        actor="Tester",
        skill_mounts=[{"name": "guard-first-repair"}],
    )
    plan = service.draft_plan(order["work_order_id"], actor="Tester")
    assert plan["skills_context"] == [
        {
            "name": "guard-first-repair",
            "version": 1,
            "content_hash": record["content_hash"],
        }
    ]
    digest = service.operations_digest()
    assert digest["skills"] == {
        "plans_mounted": 1,
        "mounts_by_skill": {"guard-first-repair@v1": 1},
    }
    packet = service.review_packet(order["work_order_id"])
    assert packet["skills"] == plan["skills_context"]


def test_an_order_under_evaluation_may_mount_the_candidate(service):
    order = service.create_gold_work_order(
        "reserve-overcommit",
        actor=EVALUATOR,
        skill_mounts=[{"name": "guard-first-repair", "version": 1}],
        skills_under_evaluation=True,
    )
    assert order["skills_under_evaluation"] is True
    plan = service.draft_plan(order["work_order_id"], actor=EVALUATOR)
    assert plan["skills_context"][0]["version"] == 1


def test_the_scripted_models_accounting_shows_the_mounts():
    order = {
        "title": "Fix reservations",
        "task_text": "Stop reservations overcommitting stock.",
        "file_scope": ["workshop/inventory.py"],
        "grading_tests": [],
    }
    mounted = dict(order)
    mounted["_skills"] = [
        {
            "name": "guard-first-repair",
            "version": 1,
            "content_hash": "ab" * 32,
            "content": V1_TEXT,
        }
    ]
    plain_model = OfflineScriptedModel(scripts={})
    plain_model.draft_plan(work_order=order, repo_files=[])
    plain = plain_model.drain_usage()
    mounted_model = OfflineScriptedModel(scripts={})
    mounted_model.draft_plan(work_order=mounted, repo_files=[])
    shown = mounted_model.drain_usage()
    assert shown[0]["prompt_tokens"] > plain[0]["prompt_tokens"]


# -- the evaluation act ---------------------------------------------------------


def test_evaluate_skill_records_the_paired_comparison(service, monkeypatch):
    calls = []

    def shaped(bench, task, *, actor, skill_mounts=None, under_evaluation=False):
        calls.append(
            {"task": task.id, "mounts": skill_mounts, "under": under_evaluation}
        )
        bad = bool(skill_mounts) and task.id == "tax-before-discount"
        return {
            "task_id": task.id,
            "verified_success": not bad,
            "failure_class": "out_of_scope_edit" if bad else None,
        }

    monkeypatch.setattr(service, "_benchmark_task", shaped)
    record = service.evaluate_skill(
        "guard-first-repair", 1, actor=EVALUATOR
    )
    assert record["set_name"] == "tuning"
    assert record["content_hash"] == content_hash(V1_TEXT)
    assert record["baseline"]["worst_successes"] == 4
    assert record["candidate"]["worst_successes"] == 3
    assert record["violations"] == 1 and record["passed"] is False
    candidate_calls = [c for c in calls if c["mounts"]]
    assert candidate_calls and all(c["under"] for c in candidate_calls)
    assert all(not c["under"] for c in calls if not c["mounts"])
    stored = service.store.list_skill_evaluations("guard-first-repair", 1)
    assert stored[-1]["evaluation_id"] == record["evaluation_id"]


# -- health, folded on read -------------------------------------------------------


def test_skill_health_folds_the_mounted_plans_and_runs(service):
    wid = "wo-health"
    stamp = "2026-10-20T00:00:00+00:00"
    service.store.put_work_order(
        {
            "work_order_id": wid,
            "title": "Mounted work",
            "task_text": "Fix it.",
            "file_scope": ["workshop/inventory.py"],
            "status": "accepted",
            "created_at": stamp,
            "updated_at": stamp,
        }
    )
    service.store.put_plan(
        {
            "work_order_id": wid,
            "version": 1,
            "approach": "Guard it.",
            "steps": [],
            "files": ["workshop/inventory.py"],
            "tests": [],
            "status": "approved",
            "skills_context": [
                {
                    "name": "guard-first-repair",
                    "version": 1,
                    "content_hash": content_hash(V1_TEXT),
                }
            ],
            "created_at": stamp,
        }
    )
    service.store.put_run(
        {
            "run_id": "run-health-ok",
            "work_order_id": wid,
            "plan_version": 1,
            "status": "succeeded",
            "cost": {"priced_calls": 3, "total": "0.30"},
            "created_at": stamp,
        }
    )
    service.store.put_run(
        {
            "run_id": "run-health-bad",
            "work_order_id": wid,
            "plan_version": 1,
            "status": "failed",
            "verification": {"reason": "grading tests fail"},
            "cost": {"priced_calls": 0, "total": "0"},
            "created_at": stamp,
        }
    )
    health = service.skill_health("guard-first-repair", 1)
    assert health["orders_mounted"] == 1
    assert health["plans_mounted"] == 1
    assert health["runs"] == 2 and health["runs_succeeded"] == 1
    assert health["verified_success_rate"] == 0.5
    assert health["failures_by_class"] == {"verification_failed": 1}
    assert health["priced_successes"] == 1
    assert health["median_cost_per_success"] == "0.30"
    empty = service.skill_health("guard-first-repair", 2)
    assert empty["orders_mounted"] == 0 and empty["runs"] == 0
    assert empty["verified_success_rate"] is None


# -- drift: the plan cites text by hash ------------------------------------------


def test_a_launch_refuses_when_the_library_text_drifted(tmp_path, library_dir):
    settings = load_settings(
        sqlite_path=str(tmp_path / "db.sqlite"),
        targets_dir=tmp_path / "targets",
        runs_dir=tmp_path / "runs",
        artifacts_dir=tmp_path / "artifacts",
        skills_dir=library_dir,
    )
    service = CodingService(settings)
    publish(service, version=1)
    order = service.create_gold_work_order(
        "reserve-overcommit",
        actor="Tester",
        skill_mounts=[{"name": "guard-first-repair", "version": 1}],
    )
    wid = order["work_order_id"]
    service.draft_plan(wid, actor="Tester")
    service.decide_plan(wid, approve=True, actor="Approver")

    # The version's text is rewritten under the same version —
    # file and index together, so the loader serves it — and a
    # fresh service over the same store must refuse the launch:
    # the approved plan cites the old hash.
    (library_dir / "guard-first-repair" / "v1.md").write_text(
        "# Guard-first repair\n\nDifferent words entirely now.\n"
    )
    index_path = library_dir / "index.json"
    index = json.loads(index_path.read_text())
    for entry in index["skills"][0]["versions"]:
        if entry["version"] == 1:
            entry["content_hash"] = content_hash(
                "# Guard-first repair\n\nDifferent words entirely now.\n"
            )
    index_path.write_text(json.dumps(index))
    drifted = CodingService(settings)
    assert refusal_code(
        lambda: drifted.start_run(wid, actor="Tester")
    ) == "skill_content_drifted"
    # The refusal left no run standing: the order still stands
    # approved, awaiting a fresh plan against the library as it is.
    assert drifted.get_work_order(wid)["status"] == "approved"
    assert drifted.store.list_runs(wid) == []


# -- a skill cannot widen scope ---------------------------------------------------


class _FakeTransport:
    def __init__(self, script):
        self.script = list(script)
        self.requests = []

    def post(self, url, *, headers, json_body, timeout):
        self.requests.append({"url": url, "json": json_body})
        return self.script.pop(0)


def _chat(content):
    body = {
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
    }
    return HttpResponse(200, json.dumps(body))


def test_a_mounted_skill_cannot_widen_the_scope(
    tmp_path, library_dir, monkeypatch
):
    monkeypatch.setenv("BASETEN_API_KEY", "test-key")
    hostile_plan = json.dumps(
        {
            "approach": "The skill says to fix orders as well.",
            "steps": [
                {
                    "intent": "Edit the orders module too, as the skill urges.",
                    "files": ["workshop/orders.py"],
                    "tests": ["workshop/tests/test_inventory.py"],
                }
            ],
        }
    )
    transport = _FakeTransport([_chat(hostile_plan)])
    settings = load_settings(
        sqlite_path=str(tmp_path / "db.sqlite"),
        targets_dir=tmp_path / "targets",
        runs_dir=tmp_path / "runs",
        artifacts_dir=tmp_path / "artifacts",
        skills_dir=library_dir,
        model_profile="baseten-glm",
        model_transport=transport,
    )
    service = CodingService(settings)
    # The skill's own text urges the out-of-scope edit; it is
    # published, mounted, and shown — and it changes nothing.
    record_evaluation(service, version=1)
    service.publish_skill("guard-first-repair", 1, actor=PUBLISHER)
    order = service.create_gold_work_order(
        "reserve-overcommit",
        actor="Tester",
        skill_mounts=[{"name": "guard-first-repair", "version": 1}],
    )
    assert refusal_code(
        lambda: service.draft_plan(order["work_order_id"], actor="Tester")
    ) == "malformed_plan"
    prompt = transport.requests[0]["json"]["messages"][1]["content"]
    assert "Mounted skills" in prompt and "guard-first-repair" in prompt


def test_the_live_execution_context_carries_the_mounted_skills():
    adapter = BasetenModel(
        load_profile("baseten-glm"), api_key="test-key",
        transport=_FakeTransport([]), sleep=lambda _s: None,
    )
    order = {
        "title": "Fix reservations",
        "task_text": "Stop reservations overcommitting stock.",
        "_skills": [
            {
                "name": "guard-first-repair",
                "version": 1,
                "content_hash": "cd" * 32,
                "content": V1_TEXT,
            }
        ],
    }
    plan = {"approach": "Guard it.", "files": [], "tests": [], "steps": []}
    adapter.start_run(work_order=order, plan=plan)
    user = adapter._messages[1]["content"]
    assert "Mounted skills" in user and "guard-first-repair" in user
    assert "content sha256" in user


# -- the API surface ---------------------------------------------------------------


def test_the_skills_api(settings, service):
    publish(service, version=1)
    client = TestClient(create_app(settings))
    listing = client.get("/api/skills")
    assert listing.status_code == 200
    by_version = {
        (row["name"], row["version"]): row for row in listing.json()
    }
    assert by_version[("guard-first-repair", 1)]["status"] == "published"
    assert by_version[("guard-first-repair", 2)]["status"] == "recorded"
    one = client.get("/api/skills/guard-first-repair/versions/2")
    assert one.status_code == 200 and one.json()["version"] == 2
    missing = client.get("/api/skills/nope")
    assert missing.status_code == 404
    assert missing.json()["code"] == "unknown_skill"
    refused = client.post(
        "/api/skills/guard-first-repair/versions/1/publish",
        json={"actor": "Another Publisher"},
    )
    assert refused.status_code == 409
    assert refused.json()["code"] == "skill_already_published"
