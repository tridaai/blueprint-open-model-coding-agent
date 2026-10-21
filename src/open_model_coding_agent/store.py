"""Case store: the governance shell's system of record.

One port, two adapters. SQLite is the local default (SQLITE_PATH,
``./data/codingagent.db``); PostgreSQL is the production profile
(DATABASE_URL). Entity payloads are JSON in TEXT columns in both
dialects — a payload read is the same shape on either store, by
design. Numbered SQL migrations in ``migrations/`` (Postgres) and
``migrations/sqlite/`` are applied by the runner at startup. The SQL
below is written with ? placeholders and translated for psycopg.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Settings


def utcnow() -> str:
    # Microseconds, not seconds: every listing orders by created_at,
    # and two records stamped in the same second would otherwise
    # fall back to their random ids — a run restarted within a
    # second of its predecessor could sort as the older one.
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _migration_files(kind: str, base: Path) -> list[Path]:
    folder = base / "sqlite" if kind == "sqlite" else base
    return sorted(folder.glob("[0-9]*.sql"))


class SqlStore:
    """SQLite and PostgreSQL behind one implementation; the dialects
    differ only in placeholder style and the connection factory."""

    def __init__(self, settings: Settings) -> None:
        self.migrations_dir = settings.migrations_dir
        self.kind = settings.store_kind
        self.target = (
            settings.database_url if self.kind == "postgres" else settings.sqlite_path
        )
        self._ph = "?" if self.kind == "sqlite" else "%s"
        self._lock = threading.Lock()
        if self.kind == "sqlite":
            Path(self.target).parent.mkdir(parents=True, exist_ok=True)
        self._run_migrations()

    # -- connection ---------------------------------------------------
    def _connect(self):
        if self.kind == "sqlite":
            conn = sqlite3.connect(self.target)
            conn.row_factory = sqlite3.Row
            return conn
        import psycopg
        from psycopg.rows import dict_row

        return psycopg.connect(self.target, row_factory=dict_row)

    def _run_migrations(self) -> None:
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version TEXT PRIMARY KEY, applied_at TEXT)"
            )
            applied = {
                row["version"]
                for row in conn.execute(
                    "SELECT version FROM schema_migrations"
                ).fetchall()
            }
            for path in _migration_files(self.kind, self.migrations_dir):
                version = path.stem
                if version in applied:
                    continue
                sql = path.read_text()
                if self.kind == "sqlite":
                    conn.executescript(sql)
                else:
                    for statement in sql.split(";\n"):
                        statement = statement.strip()
                        if statement:
                            conn.execute(statement)
                conn.execute(
                    f"INSERT INTO schema_migrations (version, applied_at) "
                    f"VALUES ({self._ph}, {self._ph})",
                    (version, "startup"),
                )
            conn.commit()

    # -- migrations ---------------------------------------------------
    def migration_status(self) -> dict:
        """The store's migration standing, read live: the versions
        the database records as applied, the versions on disk for
        this dialect, and the difference. Readiness reports it —
        a store whose database stands behind its migrations is a
        deployment fact, named, not a surprise at first write."""
        rows = self._all(
            "SELECT version FROM schema_migrations ORDER BY version"
        )
        applied = [row["version"] for row in rows]
        on_disk = [
            path.stem for path in _migration_files(self.kind, self.migrations_dir)
        ]
        behind = [version for version in on_disk if version not in applied]
        return {
            "applied": applied,
            "on_disk": on_disk,
            "behind": behind,
            "current": not behind,
        }

    # -- helpers ------------------------------------------------------
    def _one(self, sql: str, params: tuple = ()) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    def _all(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def _write(self, sql: str, params: tuple = ()) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(sql, params)
            conn.commit()

    # -- work orders ----------------------------------------------------
    def put_work_order(self, order: dict) -> None:
        payload = json.dumps(order)
        existing = self._one(
            f"SELECT work_order_id FROM work_orders WHERE work_order_id = {self._ph}",
            (order["work_order_id"],),
        )
        if existing:
            self._write(
                f"UPDATE work_orders SET status = {self._ph}, payload = {self._ph}, "
                f"updated_at = {self._ph} WHERE work_order_id = {self._ph}",
                (order["status"], payload, order["updated_at"], order["work_order_id"]),
            )
        else:
            self._write(
                f"INSERT INTO work_orders (work_order_id, status, gold_task_id, "
                f"payload, created_at, updated_at) VALUES ({self._ph}, {self._ph}, "
                f"{self._ph}, {self._ph}, {self._ph}, {self._ph})",
                (
                    order["work_order_id"],
                    order["status"],
                    order.get("gold_task_id"),
                    payload,
                    order["created_at"],
                    order["updated_at"],
                ),
            )

    def get_work_order(self, work_order_id: str) -> dict | None:
        row = self._one(
            f"SELECT payload FROM work_orders WHERE work_order_id = {self._ph}",
            (work_order_id,),
        )
        return json.loads(row["payload"]) if row else None

    def list_work_orders(self) -> list[dict]:
        rows = self._all(
            "SELECT payload FROM work_orders ORDER BY created_at, work_order_id"
        )
        return [json.loads(r["payload"]) for r in rows]

    # -- plans ----------------------------------------------------------
    def put_plan(self, plan: dict) -> None:
        payload = json.dumps(plan)
        existing = self._one(
            f"SELECT work_order_id FROM plans WHERE work_order_id = {self._ph} "
            f"AND version = {self._ph}",
            (plan["work_order_id"], plan["version"]),
        )
        if existing:
            self._write(
                f"UPDATE plans SET status = {self._ph}, payload = {self._ph} "
                f"WHERE work_order_id = {self._ph} AND version = {self._ph}",
                (plan["status"], payload, plan["work_order_id"], plan["version"]),
            )
        else:
            self._write(
                f"INSERT INTO plans (work_order_id, version, status, payload, "
                f"created_at) VALUES ({self._ph}, {self._ph}, {self._ph}, "
                f"{self._ph}, {self._ph})",
                (
                    plan["work_order_id"],
                    plan["version"],
                    plan["status"],
                    payload,
                    plan["created_at"],
                ),
            )

    def get_plan(self, work_order_id: str, version: int | None = None) -> dict | None:
        if version is None:
            row = self._one(
                f"SELECT payload FROM plans WHERE work_order_id = {self._ph} "
                f"ORDER BY version DESC",
                (work_order_id,),
            )
        else:
            row = self._one(
                f"SELECT payload FROM plans WHERE work_order_id = {self._ph} "
                f"AND version = {self._ph}",
                (work_order_id, version),
            )
        return json.loads(row["payload"]) if row else None

    def list_plans(self, work_order_id: str) -> list[dict]:
        """Every plan version for the order, oldest first. Plans are
        versioned artifacts; the history is part of the record."""
        rows = self._all(
            f"SELECT payload FROM plans WHERE work_order_id = {self._ph} "
            f"ORDER BY version",
            (work_order_id,),
        )
        return [json.loads(r["payload"]) for r in rows]

    # -- runs -------------------------------------------------------------
    def put_run(self, run: dict) -> None:
        payload = json.dumps(run)
        existing = self._one(
            f"SELECT run_id FROM runs WHERE run_id = {self._ph}", (run["run_id"],)
        )
        if existing:
            self._write(
                f"UPDATE runs SET status = {self._ph}, payload = {self._ph} "
                f"WHERE run_id = {self._ph}",
                (run["status"], payload, run["run_id"]),
            )
        else:
            self._write(
                f"INSERT INTO runs (run_id, work_order_id, status, payload, "
                f"created_at) VALUES ({self._ph}, {self._ph}, {self._ph}, "
                f"{self._ph}, {self._ph})",
                (
                    run["run_id"],
                    run["work_order_id"],
                    run["status"],
                    payload,
                    run["created_at"],
                ),
            )

    def get_run(self, run_id: str) -> dict | None:
        row = self._one(
            f"SELECT payload FROM runs WHERE run_id = {self._ph}", (run_id,)
        )
        return json.loads(row["payload"]) if row else None

    def list_runs(self, work_order_id: str) -> list[dict]:
        rows = self._all(
            f"SELECT payload FROM runs WHERE work_order_id = {self._ph} "
            f"ORDER BY created_at, run_id",
            (work_order_id,),
        )
        return [json.loads(r["payload"]) for r in rows]

    def latest_run(self, work_order_id: str) -> dict | None:
        runs = self.list_runs(work_order_id)
        return runs[-1] if runs else None

    def all_runs(self) -> list[dict]:
        """Every run on record, across work orders, oldest first.
        The cost report folds over this set."""
        rows = self._all(
            "SELECT payload FROM runs ORDER BY created_at, run_id"
        )
        return [json.loads(r["payload"]) for r in rows]

    def runs_with_status(self, status: str) -> list[dict]:
        """Every run in a status, across work orders. Startup
        reconciliation reads the 'running' set: a row still standing
        there when a new process boots belonged to a process that
        died mid-run."""
        rows = self._all(
            f"SELECT payload FROM runs WHERE status = {self._ph} "
            f"ORDER BY created_at, run_id",
            (status,),
        )
        return [json.loads(r["payload"]) for r in rows]

    # -- run checkpoints ----------------------------------------------------
    def append_checkpoint(self, record: dict) -> None:
        """Checkpoints are append-only: a run's recovery basis is its
        history, and history is never rewritten."""
        self._write(
            f"INSERT INTO run_checkpoints (checkpoint_id, run_id, "
            f"work_order_id, idx, kind, payload, created_at) VALUES "
            f"({self._ph}, {self._ph}, {self._ph}, {self._ph}, {self._ph}, "
            f"{self._ph}, {self._ph})",
            (
                record["checkpoint_id"],
                record["run_id"],
                record["work_order_id"],
                int(record["index"]),
                record["kind"],
                json.dumps(record),
                record["created_at"],
            ),
        )

    def list_checkpoints(self, run_id: str) -> list[dict]:
        rows = self._all(
            f"SELECT payload FROM run_checkpoints WHERE run_id = {self._ph} "
            f"ORDER BY idx",
            (run_id,),
        )
        return [json.loads(r["payload"]) for r in rows]

    # -- model usage ------------------------------------------------------
    def append_usage(self, record: dict) -> None:
        version = record.get("price_table_version")
        self._write(
            f"INSERT INTO model_usage (run_id, work_order_id, profile_id, "
            f"model, source, unit, prompt_tokens, completion_tokens, "
            f"latency_ms, price_table_version, created_at) VALUES "
            f"({self._ph}, {self._ph}, {self._ph}, {self._ph}, {self._ph}, "
            f"{self._ph}, {self._ph}, {self._ph}, {self._ph}, {self._ph}, "
            f"{self._ph})",
            (
                record.get("run_id"),
                record["work_order_id"],
                record["profile_id"],
                record.get("model"),
                record["source"],
                record["unit"],
                int(record["prompt_tokens"]),
                int(record["completion_tokens"]),
                int(record["latency_ms"]),
                int(version) if version is not None else None,
                utcnow(),
            ),
        )

    def list_usage(
        self, *, run_id: str | None = None, work_order_id: str | None = None
    ) -> list[dict]:
        clauses, params = [], []
        if run_id is not None:
            clauses.append(f"run_id = {self._ph}")
            params.append(run_id)
        if work_order_id is not None:
            clauses.append(f"work_order_id = {self._ph}")
            params.append(work_order_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return self._all(
            f"SELECT seq, run_id, work_order_id, profile_id, model, source, "
            f"unit, prompt_tokens, completion_tokens, latency_ms, "
            f"price_table_version, created_at "
            f"FROM model_usage{where} ORDER BY seq",
            tuple(params),
        )

    # -- benchmarks and the promotion gate ---------------------------------
    def append_benchmark_run(self, record: dict) -> None:
        """Benchmark records are append-only: a run over a set, once
        recorded, is evidence — evaluations fold over it, nothing
        rewrites it."""
        self._write(
            f"INSERT INTO benchmark_runs (benchmark_id, profile_id, "
            f"set_name, task_set_hash, actor, payload, created_at) VALUES "
            f"({self._ph}, {self._ph}, {self._ph}, {self._ph}, {self._ph}, "
            f"{self._ph}, {self._ph})",
            (
                record["benchmark_id"],
                record["profile_id"],
                record["set_name"],
                record["task_set_hash"],
                record["actor"],
                json.dumps(record),
                record["created_at"],
            ),
        )

    def list_benchmark_runs(self, profile_id: str | None = None) -> list[dict]:
        if profile_id is None:
            rows = self._all(
                "SELECT payload FROM benchmark_runs ORDER BY created_at, benchmark_id"
            )
        else:
            rows = self._all(
                f"SELECT payload FROM benchmark_runs WHERE profile_id = {self._ph} "
                f"ORDER BY created_at, benchmark_id",
                (profile_id,),
            )
        return [json.loads(r["payload"]) for r in rows]

    def append_promotion_evaluation(self, record: dict) -> None:
        """Evaluation records are append-only, like the benchmarks
        they fold: a newer evaluation supersedes an older one by
        being later, never by editing it."""
        self._write(
            f"INSERT INTO promotion_evaluations (evaluation_id, "
            f"profile_id, set_name, task_set_hash, actor, payload, "
            f"created_at) VALUES ({self._ph}, {self._ph}, {self._ph}, "
            f"{self._ph}, {self._ph}, {self._ph}, {self._ph})",
            (
                record["evaluation_id"],
                record["profile_id"],
                record["set_name"],
                record["task_set_hash"],
                record["actor"],
                json.dumps(record),
                record["created_at"],
            ),
        )

    def list_promotion_evaluations(
        self, profile_id: str | None = None
    ) -> list[dict]:
        if profile_id is None:
            rows = self._all(
                "SELECT payload FROM promotion_evaluations "
                "ORDER BY created_at, evaluation_id"
            )
        else:
            rows = self._all(
                f"SELECT payload FROM promotion_evaluations "
                f"WHERE profile_id = {self._ph} "
                f"ORDER BY created_at, evaluation_id",
                (profile_id,),
            )
        return [json.loads(r["payload"]) for r in rows]

    def append_promotion_event(self, record: dict) -> None:
        """The promotion history is append-only: promotions and
        rollbacks are events, and the current default is derived
        from the latest one — history is never rewritten to make a
        default change look like it was always so."""
        self._write(
            f"INSERT INTO promotion_events (event_id, kind, "
            f"from_profile, to_profile, actor, payload, created_at) "
            f"VALUES ({self._ph}, {self._ph}, {self._ph}, {self._ph}, "
            f"{self._ph}, {self._ph}, {self._ph})",
            (
                record["event_id"],
                record["kind"],
                record.get("from_profile"),
                record["to_profile"],
                record["actor"],
                json.dumps(record),
                record["created_at"],
            ),
        )

    def list_promotion_events(self) -> list[dict]:
        rows = self._all(
            "SELECT payload FROM promotion_events ORDER BY seq"
        )
        return [json.loads(r["payload"]) for r in rows]

    # -- skills library (round 19) ---------------------------------------
    def append_skill_evaluation(self, record: dict) -> None:
        """Skill evaluations are append-only, like the promotion
        gate's: an evaluation, once recorded, is the evidence a
        publication is judged by — a later content edit marks it
        stale at the gate, it never rewrites it. The content hash
        is a column of its own because the gate judges by it."""
        self._write(
            f"INSERT INTO skill_evaluations (evaluation_id, name, "
            f"version, content_hash, actor, payload, created_at) "
            f"VALUES ({self._ph}, {self._ph}, {self._ph}, {self._ph}, "
            f"{self._ph}, {self._ph}, {self._ph})",
            (
                record["evaluation_id"],
                record["name"],
                record["version"],
                record["content_hash"],
                record["actor"],
                json.dumps(record),
                record["created_at"],
            ),
        )

    def list_skill_evaluations(
        self, name: str | None = None, version: int | None = None
    ) -> list[dict]:
        sql = "SELECT payload FROM skill_evaluations"
        clauses: list[str] = []
        params: list = []
        if name is not None:
            clauses.append(f"name = {self._ph}")
            params.append(name)
        if version is not None:
            clauses.append(f"version = {self._ph}")
            params.append(version)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at"
        return [json.loads(r["payload"]) for r in self._all(sql, tuple(params))]

    def append_skill_event(self, record: dict) -> None:
        """The publication history is append-only: publishes and
        rollbacks are events, and a version's standing is derived
        by folding them — history is never rewritten to make a
        rollback look like the version was never published."""
        self._write(
            f"INSERT INTO skill_events (event_id, kind, name, "
            f"version, actor, payload, created_at) VALUES "
            f"({self._ph}, {self._ph}, {self._ph}, {self._ph}, "
            f"{self._ph}, {self._ph}, {self._ph})",
            (
                record["event_id"],
                record["kind"],
                record["name"],
                record["version"],
                record["actor"],
                json.dumps(record),
                record["created_at"],
            ),
        )

    def list_skill_events(self, name: str | None = None) -> list[dict]:
        sql = "SELECT payload FROM skill_events"
        params: tuple = ()
        if name is not None:
            sql += f" WHERE name = {self._ph}"
            params = (name,)
        sql += " ORDER BY seq"
        return [json.loads(r["payload"]) for r in self._all(sql, params)]

    # -- shortlist comparisons (round 8) --------------------------------
    def append_comparison(self, record: dict) -> None:
        """Comparison records are append-only, like the benchmark
        records they name: a comparison, once recorded, is the
        evidence a report folds over — a later task-set edit marks
        it stale on read, it never rewrites it."""
        self._write(
            f"INSERT INTO comparisons (comparison_id, set_name, "
            f"task_set_hash, actor, payload, created_at) VALUES "
            f"({self._ph}, {self._ph}, {self._ph}, {self._ph}, "
            f"{self._ph}, {self._ph})",
            (
                record["comparison_id"],
                record["set_name"],
                record["task_set_hash"],
                record["actor"],
                json.dumps(record),
                record["created_at"],
            ),
        )

    def get_comparison(self, comparison_id: str) -> dict | None:
        row = self._one(
            f"SELECT payload FROM comparisons WHERE comparison_id = {self._ph}",
            (comparison_id,),
        )
        return json.loads(row["payload"]) if row else None

    def list_comparisons(self) -> list[dict]:
        rows = self._all(
            "SELECT payload FROM comparisons ORDER BY created_at, comparison_id"
        )
        return [json.loads(r["payload"]) for r in rows]

    # -- task templates (round 12) ---------------------------------------
    def put_template(self, record: dict) -> None:
        """A template version is written once at creation and once
        at publishing (its status and publisher). Nothing else
        ever updates it — an edit to a template is a new version,
        so history is inserts plus the one governed status flip."""
        payload = json.dumps(record)
        existing = self._one(
            f"SELECT template_id FROM task_templates WHERE name = {self._ph} "
            f"AND version = {self._ph}",
            (record["name"], record["version"]),
        )
        if existing:
            self._write(
                f"UPDATE task_templates SET status = {self._ph}, "
                f"payload = {self._ph} WHERE name = {self._ph} "
                f"AND version = {self._ph}",
                (
                    record["status"],
                    payload,
                    record["name"],
                    record["version"],
                ),
            )
        else:
            self._write(
                f"INSERT INTO task_templates (template_id, name, version, "
                f"status, payload, created_at) VALUES ({self._ph}, "
                f"{self._ph}, {self._ph}, {self._ph}, {self._ph}, "
                f"{self._ph})",
                (
                    record["template_id"],
                    record["name"],
                    record["version"],
                    record["status"],
                    payload,
                    record["created_at"],
                ),
            )

    def get_template(
        self, name: str, version: int | None = None
    ) -> dict | None:
        """One template version — the named one, or the latest
        version of the name when version is omitted."""
        if version is None:
            row = self._one(
                f"SELECT payload FROM task_templates WHERE name = {self._ph} "
                f"ORDER BY version DESC",
                (name,),
            )
        else:
            row = self._one(
                f"SELECT payload FROM task_templates WHERE name = {self._ph} "
                f"AND version = {self._ph}",
                (name, version),
            )
        return json.loads(row["payload"]) if row else None

    def list_templates(self, name: str | None = None) -> list[dict]:
        if name is None:
            rows = self._all(
                "SELECT payload FROM task_templates "
                "ORDER BY name, version"
            )
        else:
            rows = self._all(
                f"SELECT payload FROM task_templates WHERE name = {self._ph} "
                f"ORDER BY version",
                (name,),
            )
        return [json.loads(r["payload"]) for r in rows]

    # -- run challenges (round 13) --------------------------------------
    def put_challenge(self, record: dict) -> None:
        """A challenge is written once when the pass raises it,
        and afterwards only by a recorded act — an answer or a
        supersession — which rewrites the row's status and
        payload. The store keeps no other history for it: the
        acts themselves are audit records."""
        payload = json.dumps(record)
        existing = self._one(
            f"SELECT challenge_id FROM run_challenges "
            f"WHERE challenge_id = {self._ph}",
            (record["challenge_id"],),
        )
        if existing:
            self._write(
                f"UPDATE run_challenges SET status = {self._ph}, "
                f"payload = {self._ph} WHERE challenge_id = {self._ph}",
                (record["status"], payload, record["challenge_id"]),
            )
        else:
            self._write(
                f"INSERT INTO run_challenges (challenge_id, run_id, "
                f"work_order_id, rule, status, payload, created_at) "
                f"VALUES ({self._ph}, {self._ph}, {self._ph}, "
                f"{self._ph}, {self._ph}, {self._ph}, {self._ph})",
                (
                    record["challenge_id"],
                    record["run_id"],
                    record["work_order_id"],
                    record["rule"],
                    record["status"],
                    payload,
                    record["created_at"],
                ),
            )

    def get_challenge(self, challenge_id: str) -> dict | None:
        row = self._one(
            f"SELECT payload FROM run_challenges "
            f"WHERE challenge_id = {self._ph}",
            (challenge_id,),
        )
        return json.loads(row["payload"]) if row else None

    def list_challenges(
        self,
        work_order_id: str | None = None,
        run_id: str | None = None,
    ) -> list[dict]:
        clauses, params = [], []
        if work_order_id is not None:
            clauses.append(f"work_order_id = {self._ph}")
            params.append(work_order_id)
        if run_id is not None:
            clauses.append(f"run_id = {self._ph}")
            params.append(run_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._all(
            f"SELECT payload FROM run_challenges{where} "
            f"ORDER BY created_at, challenge_id",
            tuple(params),
        )
        return [json.loads(r["payload"]) for r in rows]

    # -- batches (round 14) --------------------------------------------
    def put_batch(self, record: dict) -> None:
        """A batch record is written at submission and afterwards
        only by recorded acts — a drive pass's marks, a budget
        stop, a cancellation, a driver release — each of which
        rewrites the row whole from the record it read. Progress
        is never written here: it is folded on read from the
        orders' records."""
        payload = json.dumps(record)
        existing = self._one(
            f"SELECT batch_id FROM batches WHERE batch_id = {self._ph}",
            (record["batch_id"],),
        )
        if existing:
            self._write(
                f"UPDATE batches SET status = {self._ph}, "
                f"payload = {self._ph}, updated_at = {self._ph} "
                f"WHERE batch_id = {self._ph}",
                (
                    record["status"],
                    payload,
                    record["updated_at"],
                    record["batch_id"],
                ),
            )
        else:
            self._write(
                f"INSERT INTO batches (batch_id, name, status, actor, "
                f"payload, created_at, updated_at) VALUES ({self._ph}, "
                f"{self._ph}, {self._ph}, {self._ph}, {self._ph}, "
                f"{self._ph}, {self._ph})",
                (
                    record["batch_id"],
                    record["name"],
                    record["status"],
                    record["actor"],
                    payload,
                    record["created_at"],
                    record["updated_at"],
                ),
            )

    def get_batch(self, batch_id: str) -> dict | None:
        row = self._one(
            f"SELECT payload FROM batches WHERE batch_id = {self._ph}",
            (batch_id,),
        )
        return json.loads(row["payload"]) if row else None

    def list_batches(self) -> list[dict]:
        rows = self._all(
            "SELECT payload FROM batches ORDER BY created_at, batch_id"
        )
        return [json.loads(r["payload"]) for r in rows]

    # -- audit --------------------------------------------------------------
    def append_audit(
        self, entity: str, entity_id: str, event: str, actor: str, detail: dict
    ) -> None:
        self._write(
            f"INSERT INTO audit (entity, entity_id, event, actor, detail, "
            f"created_at) VALUES ({self._ph}, {self._ph}, {self._ph}, {self._ph}, "
            f"{self._ph}, {self._ph})",
            (entity, entity_id, event, actor, json.dumps(detail), utcnow()),
        )

    def list_audit(self, entity: str, entity_id: str) -> list[dict]:
        rows = self._all(
            f"SELECT seq, event, actor, detail, created_at FROM audit "
            f"WHERE entity = {self._ph} AND entity_id = {self._ph} ORDER BY seq",
            (entity, entity_id),
        )
        for row in rows:
            row["detail"] = json.loads(row["detail"])
        return rows

    def list_audit_events(self, events: list[str]) -> list[dict]:
        """Every audit record carrying one of the named events,
        across entities, in record order. The review workbench
        folds reviewer history from these rows: a decision is an
        audit record (actor, timestamp, detail), so history needs
        no table of its own."""
        if not events:
            return []
        marks = ", ".join(self._ph for _ in events)
        rows = self._all(
            f"SELECT seq, entity, entity_id, event, actor, detail, "
            f"created_at FROM audit WHERE event IN ({marks}) ORDER BY seq",
            tuple(events),
        )
        for row in rows:
            row["detail"] = json.loads(row["detail"])
        return rows


def make_store(settings: Settings) -> SqlStore:
    return SqlStore(settings)


# Typing alias: the service depends on the port, not the adapter.
Store = SqlStore

__all__ = ["SqlStore", "Store", "make_store", "utcnow", "Any"]
