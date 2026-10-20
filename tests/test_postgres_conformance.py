"""PostgreSQL conformance (round 20): the production store, exercised.

The unit suite builds every test its own SQLite store in a tmp
directory, so it cannot prove the Postgres adapter. This leg can.
It is env-gated on CODINGAGENT_TEST_DATABASE_URL naming a scratch
database whose name says what it is (it must contain "test"):
when the variable is unset every test here skips, and when it is
set each test first empties the scratch database — dropping only
this blueprint's own tables, the set its migrations create, plus
the migration ledger — so the store's own migration runner
rebuilds the schema from empty on construction.

The guards are the point, not hygiene theatre. The wipe refuses,
loudly, when the named database does not look like a test database,
and when it is the same database the application itself is
configured with (DATABASE_URL): wiping the deployment's real store
from a test run would be a disaster dressed as housekeeping.

What the leg proves, against the real wire:

- the full migration chain (0001–0010, both dialects' shared
  schema) applies from empty, and the newest tables — task
  templates (0007), run challenges (0008), batches (0009), the
  skills library (0010) — round-trip like the core ones;
- the payload-TEXT invariant: entity payloads are TEXT columns
  on Postgres exactly as on SQLite, and a raw read comes back a
  string the store parses itself — never a driver-parsed object.
  (The sibling blueprints' release rounds each found a store
  that had never actually run on Postgres because its reads
  assumed the SQLite wire shape; this schema carries payloads
  as TEXT by construction, and this leg is the regression test
  that keeps the invariant proved rather than assumed.)
- the hosted deployment profile boots against the real store:
  settings load under DEPLOYMENT_PROFILE=hosted with DATABASE_URL
  set, and one gold task serves end to end through a live model
  profile over a fake transport — the production shape, with
  only the wire faked — and a second service, built fresh,
  reads the outcome back from the database.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from open_model_coding_agent.baseten import HttpResponse
from open_model_coding_agent.config import load_settings
from open_model_coding_agent.gold import GoldSuite
from open_model_coding_agent.models import Batch, Plan, Run, TaskTemplate, WorkOrder
from open_model_coding_agent.service import CodingService
from open_model_coding_agent.store import SqlStore, utcnow

REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_DSN = os.environ.get("CODINGAGENT_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DSN,
    reason="CODINGAGENT_TEST_DATABASE_URL not set — the Postgres "
    "conformance leg runs only against an explicitly named "
    "scratch database",
)


# -- the scratch database ---------------------------------------------------


def _database_identity(dsn: str) -> tuple:
    """(host, port, dbname) a DSN points at, with libpq defaults."""
    from psycopg.conninfo import conninfo_to_dict

    info = conninfo_to_dict(dsn)
    return (
        info.get("host", "localhost"),
        str(info.get("port", "5432")),
        info.get("dbname"),
    )


def _own_tables() -> list[str]:
    """The tables this blueprint's Postgres migrations create —
    derived from the migration files themselves, so the wipe can
    never drift from the schema it protects."""
    names: set[str] = set()
    for path in (REPO_ROOT / "migrations").glob("[0-9]*.sql"):
        names.update(
            re.findall(r"CREATE TABLE IF NOT EXISTS\s+(\w+)", path.read_text())
        )
    return sorted(names | {"schema_migrations"})


def _fresh_scratch_database(dsn: str) -> None:
    """Empty the scratch database, guarded. Only this blueprint's
    own tables are dropped — never a blanket wipe of the public
    schema — so the store's migration runner rebuilds from empty
    and a table the blueprint does not own survives the leg."""
    import psycopg

    _host, _port, dbname = _database_identity(dsn)
    if not dbname or "test" not in dbname.lower():
        raise RuntimeError(
            f"CODINGAGENT_TEST_DATABASE_URL names database {dbname!r}, "
            "which does not look like a test database; refusing to "
            "run the conformance leg against it. Point it at a "
            "scratch database with 'test' in its name."
        )
    app_url = os.environ.get("DATABASE_URL")
    if app_url:
        try:
            same = _database_identity(app_url) == _database_identity(dsn)
        except Exception:
            same = False  # not a Postgres DSN — cannot name this database
        if same:
            raise RuntimeError(
                "CODINGAGENT_TEST_DATABASE_URL points at the "
                "application's own DATABASE_URL database; refusing "
                "to wipe it. Point CODINGAGENT_TEST_DATABASE_URL at "
                "a scratch database."
            )
    with psycopg.connect(dsn, autocommit=True) as conn:
        for table in _own_tables():
            conn.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')


@pytest.fixture(autouse=True)
def _fresh_database():
    _fresh_scratch_database(TEST_DSN)


def _pg_settings(tmp_path, **overrides):
    base = {
        "database_url": TEST_DSN,
        "targets_dir": tmp_path / "targets",
        "runs_dir": tmp_path / "runs",
        "artifacts_dir": tmp_path / "artifacts",
    }
    base.update(overrides)
    return load_settings(**base)


# -- the migration chain ------------------------------------------------------


def test_the_full_migration_chain_applies_from_empty(tmp_path):
    settings = _pg_settings(tmp_path)
    store = SqlStore(settings)
    status = store.migration_status()
    on_disk = sorted(
        path.stem for path in (REPO_ROOT / "migrations").glob("[0-9]*.sql")
    )
    assert status["applied"] == on_disk
    assert status["current"] is True and status["behind"] == []
    # The newest rounds' tables stand in the rebuilt schema —
    # templates (0007), challenges (0008), batches (0009), the
    # skills library's pair (0010) — alongside the round-1 core.
    import psycopg

    with psycopg.connect(TEST_DSN) as conn:
        present = {
            row[0]
            for row in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            )
        }
    for table in (
        "work_orders",
        "plans",
        "runs",
        "audit",
        "model_usage",
        "run_checkpoints",
        "task_templates",
        "run_challenges",
        "batches",
        "skill_evaluations",
        "skill_events",
    ):
        assert table in present


def test_payload_columns_are_text_and_read_back_as_strings(tmp_path):
    """The invariant the schema is built on: payloads are TEXT on
    Postgres, so a raw read is a string in both dialects and the
    store's own parsing is the only parsing."""
    import psycopg
    from psycopg.rows import dict_row

    store = SqlStore(_pg_settings(tmp_path))
    order = WorkOrder(
        work_order_id="wo-conformance-text",
        title="Payload shape",
        task_text="the payload column carries JSON as text",
        file_scope=["workshop/inventory.py"],
        created_at=utcnow(),
        updated_at=utcnow(),
    ).model_dump()
    store.put_work_order(order)

    with psycopg.connect(TEST_DSN, row_factory=dict_row) as conn:
        types = {
            (row["table_name"], row["column_name"]): row["data_type"]
            for row in conn.execute(
                "SELECT table_name, column_name, data_type FROM "
                "information_schema.columns WHERE table_schema = "
                "'public' AND column_name = 'payload'"
            )
        }
        raw = conn.execute(
            "SELECT payload FROM work_orders WHERE work_order_id = %s",
            ("wo-conformance-text",),
        ).fetchone()["payload"]
    assert types and all(kind == "text" for kind in types.values()), types
    assert isinstance(raw, str)
    assert json.loads(raw)["work_order_id"] == "wo-conformance-text"


# -- round-trips across every table family -----------------------------------


def test_store_round_trips_on_postgres(tmp_path):
    store = SqlStore(_pg_settings(tmp_path))
    now = utcnow()

    # Work orders: insert, read back equal, update in place, list.
    order = WorkOrder(
        work_order_id="wo-conformance-1",
        title="Conformance order",
        task_text="Ünïcode task text — payloads carry text as text",
        file_scope=["workshop/inventory.py"],
        gold_task_id="reserve-overcommit",
        requested_by="conformance",
        created_at=now,
        updated_at=now,
    ).model_dump()
    store.put_work_order(order)
    assert store.get_work_order("wo-conformance-1") == order
    order["status"] = "plan_ready"
    store.put_work_order(order)
    assert store.get_work_order("wo-conformance-1")["status"] == "plan_ready"
    assert [o["work_order_id"] for o in store.list_work_orders()] == [
        "wo-conformance-1"
    ]

    # Plans: versions stand side by side; latest wins by default.
    for version in (1, 2):
        store.put_plan(
            Plan(
                work_order_id="wo-conformance-1",
                version=version,
                approach=f"approach v{version}",
                created_at=now,
            ).model_dump()
        )
    assert store.get_plan("wo-conformance-1")["version"] == 2
    assert store.get_plan("wo-conformance-1", 1)["approach"] == "approach v1"
    assert [p["version"] for p in store.list_plans("wo-conformance-1")] == [1, 2]

    # Runs: every read shape the service folds over.
    run = Run(
        run_id="run-conformance-1",
        work_order_id="wo-conformance-1",
        plan_version=2,
        model_profile="offline",
        status="succeeded",
        verification={"passed": True, "reason": "conformance"},
        created_at=now,
    ).model_dump()
    store.put_run(run)
    assert store.get_run("run-conformance-1") == run
    assert store.latest_run("wo-conformance-1")["run_id"] == "run-conformance-1"
    assert [r["run_id"] for r in store.all_runs()] == ["run-conformance-1"]
    assert store.runs_with_status("succeeded") != []
    assert store.runs_with_status("failed") == []

    # Checkpoints: append-only, read back in index order.
    for index in (0, 1):
        store.append_checkpoint(
            {
                "checkpoint_id": f"ckpt-conformance-{index}",
                "run_id": "run-conformance-1",
                "work_order_id": "wo-conformance-1",
                "index": index,
                "kind": "step",
                "created_at": now,
            }
        )
    assert [
        c["checkpoint_id"] for c in store.list_checkpoints("run-conformance-1")
    ] == ["ckpt-conformance-0", "ckpt-conformance-1"]

    # Usage: rows land with their context and fold by either key.
    store.append_usage(
        {
            "run_id": "run-conformance-1",
            "work_order_id": "wo-conformance-1",
            "profile_id": "offline",
            "model": None,
            "source": "scripted",
            "unit": "chars",
            "prompt_tokens": 120,
            "completion_tokens": 40,
            "latency_ms": 3,
            "price_table_version": 2,
        }
    )
    assert len(store.list_usage(run_id="run-conformance-1")) == 1
    assert len(store.list_usage(work_order_id="wo-conformance-1")) == 1
    assert store.list_usage(run_id="run-other") == []

    # Benchmarks, promotion evaluations, promotion events.
    store.append_benchmark_run(
        {
            "benchmark_id": "bench-conformance",
            "profile_id": "offline",
            "set_name": "held_out",
            "task_set_hash": "hash",
            "actor": "conformance",
            "created_at": now,
        }
    )
    assert store.list_benchmark_runs("offline")[0]["benchmark_id"] == (
        "bench-conformance"
    )
    store.append_promotion_evaluation(
        {
            "evaluation_id": "eval-conformance",
            "profile_id": "offline",
            "set_name": "held_out",
            "task_set_hash": "hash",
            "actor": "conformance",
            "created_at": now,
        }
    )
    assert store.list_promotion_evaluations()[0]["evaluation_id"] == (
        "eval-conformance"
    )
    store.append_promotion_event(
        {
            "event_id": "evt-conformance",
            "kind": "promotion",
            "from_profile": None,
            "to_profile": "offline",
            "actor": "conformance",
            "created_at": now,
        }
    )
    assert store.list_promotion_events()[0]["event_id"] == "evt-conformance"

    # Comparisons.
    store.append_comparison(
        {
            "comparison_id": "cmp-conformance",
            "set_name": "held_out",
            "task_set_hash": "hash",
            "actor": "conformance",
            "created_at": now,
        }
    )
    assert store.get_comparison("cmp-conformance")["set_name"] == "held_out"
    assert len(store.list_comparisons()) == 1

    # Task templates (0007): create, then the one governed flip.
    template = TaskTemplate(
        template_id="tpl-conformance",
        name="conformance-template",
        version=1,
        description="a template the leg carries",
        task_text="fix {module}",
        parameters=["module"],
        scope_pattern=["workshop/{module}.py"],
        author="conformance",
        created_at=now,
    ).model_dump()
    store.put_template(template)
    assert store.get_template("conformance-template", 1)["status"] == "draft"
    template["status"] = "published"
    template["published_by"] = "another-actor"
    store.put_template(template)
    assert store.get_template("conformance-template")["status"] == "published"

    # Run challenges (0008): write, read by order and by run.
    store.put_challenge(
        {
            "challenge_id": "challenge-conformance",
            "run_id": "run-conformance-1",
            "work_order_id": "wo-conformance-1",
            "rule": "single_source_figures",
            "severity": "advisory",
            "current_severity": "advisory",
            "status": "open",
            "evidence": {"figure": "42"},
            "evidence_key": "conf-key",
            "disposition": None,
            "created_at": now,
        }
    )
    assert store.get_challenge("challenge-conformance")["rule"] == (
        "single_source_figures"
    )
    assert len(store.list_challenges(work_order_id="wo-conformance-1")) == 1
    assert len(store.list_challenges(run_id="run-conformance-1")) == 1

    # Batches (0009): submission record, then an act's rewrite.
    batch = Batch(
        batch_id="batch-conformance",
        name="conformance batch",
        actor="conformance",
        budget={"max_model_calls": 10},
        items=[{"index": 0, "state": "pending"}],
        created_at=now,
        updated_at=now,
    ).model_dump()
    store.put_batch(batch)
    assert store.get_batch("batch-conformance")["name"] == "conformance batch"
    batch["status"] = "cancelled"
    store.put_batch(batch)
    assert store.list_batches()[0]["status"] == "cancelled"

    # The skills library (0010): evaluations and events.
    store.append_skill_evaluation(
        {
            "evaluation_id": "sev-conformance",
            "name": "test-first-repair",
            "version": 1,
            "content_hash": "hash",
            "actor": "conformance",
            "passed": True,
            "created_at": now,
        }
    )
    assert store.list_skill_evaluations("test-first-repair", 1)[0][
        "evaluation_id"
    ] == "sev-conformance"
    store.append_skill_event(
        {
            "event_id": "ske-conformance",
            "kind": "published",
            "name": "test-first-repair",
            "version": 1,
            "actor": "conformance",
            "created_at": now,
        }
    )
    assert store.list_skill_events("test-first-repair")[0]["event_id"] == (
        "ske-conformance"
    )

    # Audit: per-entity reads and the cross-entity event fold.
    store.append_audit(
        "work_order", "wo-conformance-1", "work_order_created",
        "conformance", {"via": "postgres leg"},
    )
    store.append_audit(
        "work_order", "wo-conformance-1", "plan_approved",
        "conformance", {},
    )
    assert [row["event"] for row in store.list_audit(
        "work_order", "wo-conformance-1"
    )] == ["work_order_created", "plan_approved"]
    assert len(store.list_audit_events(["plan_approved"])) == 1


# -- the hosted boot ----------------------------------------------------------


class _FakeTransport:
    """Replays recorded-shape Chat Completions responses, in the
    eval gate's mould: the live adapter's code path runs end to
    end; the network never does."""

    def __init__(self, script):
        self.script = list(script)

    def post(self, url, *, headers, json_body, timeout):
        return self.script.pop(0)


def _chat(content=None, tool_calls=None):
    message: dict = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    body = {
        "choices": [{"message": message, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 120,
            "completion_tokens": 40,
            "total_tokens": 160,
        },
    }
    return HttpResponse(200, json.dumps(body))


def _tool_call(call_id, name, args):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def test_hosted_profile_boots_on_postgres_and_serves_a_gold_task(
    tmp_path, monkeypatch
):
    """The production shape end to end: DEPLOYMENT_PROFILE=hosted
    binds its rules at load (live model, Postgres store, key
    present), the service constructs on the real store, one gold
    task runs through the live profile over a fake transport,
    and a second, fresh service reads the recorded outcome back
    from the database — the bytes on disk, not one process's
    state."""
    task = GoldSuite(REPO_ROOT / "evals" / "gold").get("reserve-overcommit")
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
    # DATABASE_URL is repointed at the scratch database only now,
    # after the fixture's wipe guards have read the ambient env:
    # the boot targets the scratch store deliberately, and the
    # wipe never runs against a DATABASE_URL this test set.
    monkeypatch.setenv("DEPLOYMENT_PROFILE", "hosted")
    monkeypatch.setenv("DATABASE_URL", TEST_DSN)
    monkeypatch.setenv("BASETEN_API_KEY", "conformance-fake-key")
    monkeypatch.delenv("MODEL_PROFILE", raising=False)

    dirs = {
        "targets_dir": tmp_path / "targets",
        "runs_dir": tmp_path / "runs",
        "artifacts_dir": tmp_path / "artifacts",
    }
    settings = load_settings(model_profile="baseten-glm", **dirs)
    assert settings.store_kind == "postgres"
    assert settings.deployment.name == "hosted"
    settings.model_transport = _FakeTransport(script)
    service = CodingService(settings)

    readiness = service.readiness()
    assert readiness["status"] == "ready", readiness["problems"]
    assert readiness["store"] == "postgres"
    assert readiness["migrations"]["current"] is True
    assert readiness["deployment_profile"] == "hosted"

    order = service.create_gold_work_order(task.id, actor="conformance")
    wid = order["work_order_id"]
    service.draft_plan(wid, actor="conformance")
    service.decide_plan(wid, approve=True, actor="Conformance Reviewer")
    run = service.start_run(wid, actor="conformance")
    assert run["status"] == "succeeded", run.get("failure_reason")
    assert run["verification"]["passed"] is True
    assert run["deployment_profile"] == "hosted"

    # A fresh service over the same database reads the outcome
    # back: the order, its run, and the usage the run recorded.
    reread = CodingService(load_settings(model_profile="baseten-glm", **dirs))
    detail = reread.work_order_detail(wid)
    assert detail["work_order"]["status"] == "succeeded"
    assert [r["run_id"] for r in detail["runs"]] == [run["run_id"]]
    assert detail["usage"], "the hosted run recorded no usage on Postgres"
    assert all(row["profile_id"] == "baseten-glm" for row in detail["usage"])
