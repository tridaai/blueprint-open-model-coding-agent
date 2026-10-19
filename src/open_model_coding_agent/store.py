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
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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


def make_store(settings: Settings) -> SqlStore:
    return SqlStore(settings)


# Typing alias: the service depends on the port, not the adapter.
Store = SqlStore

__all__ = ["SqlStore", "Store", "make_store", "utcnow", "Any"]
