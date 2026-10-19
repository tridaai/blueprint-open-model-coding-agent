"""The gold task suite: the benchmark, as data.

Each gold task is gold-by-construction. It ships a *broken* overlay of
one workshop file (a planted defect or a stubbed feature), a *fixed*
overlay (the correct file), grading tests that fail on the broken
state and pass on the fixed state, and the scripted edits that turn
one into the other. The overlays and grading tests live in
``evals/gold/`` — the eval area. They are never copied into a run
workspace: the shell stages grading tests only into the verification
copy, and the reference fix reaches a run only as the scripted
model's edits, the way a real model's knowledge would.

:meth:`GoldSuite.check_construction` proves the construction: every
scripted edit's anchor occurs exactly once in the broken file,
applying the edits reproduces the fixed file byte for byte.
"""

from __future__ import annotations

import json
from pathlib import Path

from .errors import ConfigError


class GoldTask:
    def __init__(self, spec: dict, root: Path) -> None:
        self.spec = spec
        self.root = root
        self.id: str = spec["id"]
        self.title: str = spec["title"]
        self.task_text: str = spec["task_text"]
        self.file_scope: list[str] = spec["file_scope"]
        self.grading_file: str = spec["grading_file"]
        self.plan_approach: str = spec["plan_approach"]
        self.scripted_edits: list[dict] = spec["scripted_edits"]

    # -- overlay access -------------------------------------------------
    def _overlay(self, kind: str) -> dict[str, str]:
        base = self.root / kind / self.id
        out: dict[str, str] = {}
        for path in sorted(base.rglob("*")):
            if path.is_file():
                out[path.relative_to(base).as_posix()] = path.read_text()
        return out

    def broken_files(self) -> dict[str, str]:
        return self._overlay("broken")

    def fixed_files(self) -> dict[str, str]:
        return self._overlay("fixed")

    def grading_source(self) -> str:
        return (self.root / "grading" / self.id / self.grading_file).read_text()

    def grading_relpath(self) -> str:
        return f"grading/{self.grading_file}"

    # -- construction proof ---------------------------------------------
    def check_construction(self) -> list[str]:
        """Return a list of construction errors (empty = sound)."""
        problems: list[str] = []
        broken = self.broken_files()
        fixed = self.fixed_files()
        if set(broken) != set(fixed):
            problems.append(
                f"{self.id}: broken/fixed overlay file sets differ: "
                f"{sorted(broken)} vs {sorted(fixed)}"
            )
            return problems
        for edit in self.scripted_edits:
            path = edit["path"]
            if path not in broken:
                problems.append(f"{self.id}: scripted edit names unknown file {path}")
                continue
            if broken[path].count(edit["old"]) != 1:
                problems.append(
                    f"{self.id}: edit anchor for {path} does not occur exactly "
                    f"once in the broken file"
                )
        if not problems:
            repaired = dict(broken)
            for edit in self.scripted_edits:
                repaired[edit["path"]] = repaired[edit["path"]].replace(
                    edit["old"], edit["new"]
                )
            for path in fixed:
                if repaired[path] != fixed[path]:
                    problems.append(
                        f"{self.id}: scripted edits do not reproduce the fixed "
                        f"file for {path}"
                    )
        return problems


class GoldSuite:
    def __init__(self, gold_dir: Path) -> None:
        self.root = Path(gold_dir)
        index = self.root / "tasks.json"
        if not index.is_file():
            raise ConfigError(f"gold task index not found at {index}")
        data = json.loads(index.read_text())
        self.tasks: list[GoldTask] = [GoldTask(spec, self.root) for spec in data["tasks"]]
        self._by_id = {t.id: t for t in self.tasks}

    def get(self, task_id: str) -> GoldTask | None:
        return self._by_id.get(task_id)

    def check_construction(self) -> list[str]:
        problems: list[str] = []
        for task in self.tasks:
            problems.extend(task.check_construction())
        return problems
