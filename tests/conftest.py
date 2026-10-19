from __future__ import annotations

from pathlib import Path

import pytest

from open_model_coding_agent.config import load_settings
from open_model_coding_agent.service import CodingService

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def settings(tmp_path):
    return load_settings(
        sqlite_path=str(tmp_path / "codingagent.db"),
        targets_dir=tmp_path / "targets",
        runs_dir=tmp_path / "runs",
        artifacts_dir=tmp_path / "artifacts",
    )


@pytest.fixture()
def service(settings):
    return CodingService(settings)


@pytest.fixture()
def gold_order(service):
    return service.create_gold_work_order("reserve-overcommit", actor="tester")


@pytest.fixture()
def approved_order(service, gold_order):
    wid = gold_order["work_order_id"]
    service.draft_plan(wid, actor="tester")
    service.decide_plan(wid, approve=True, actor="Reviewer")
    return service.get_work_order(wid)
