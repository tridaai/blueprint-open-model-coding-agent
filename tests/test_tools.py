"""The tool port: scope is enforced in code, commands are an
allowlist of one shape, and the child env carries no credentials."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from open_model_coding_agent.tools import SandboxTools, scrubbed_env

WORKSHOP = Path(__file__).resolve().parents[1] / "workshop"


@pytest.fixture()
def tools(tmp_path):
    root = tmp_path / "repo"
    import shutil

    shutil.copytree(WORKSHOP, root / "workshop")
    return SandboxTools(root, scope=["workshop/inventory.py"], test_timeout=60)


def test_edit_inside_scope_lands(tools):
    result = tools.edit_file(
        "workshop/inventory.py", "def skus(self)", "def skus(self)  # all"
    )
    assert result.ok
    assert "def skus(self)  # all" in (tools.root / "workshop/inventory.py").read_text()


def test_root_reached_through_a_symlink_still_resolves(tmp_path):
    # macOS hands out temp dirs as /var/... while their real path is
    # /private/var/...; a root given through a symlink must not make
    # every workspace file look like an escape.
    import shutil

    real = tmp_path / "real"
    shutil.copytree(WORKSHOP, real / "workshop")
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    tools = SandboxTools(
        link, scope=["workshop/inventory.py"], test_timeout=60
    )
    assert tools.read_file("workshop/inventory.py").ok
    assert tools.edit_file(
        "workshop/inventory.py", "def skus(self)", "def skus(self)  # all"
    ).ok


def test_edit_outside_scope_is_refused_as_data(tools):
    result = tools.edit_file("workshop/pricing.py", "x", "y")
    assert not result.ok and result.refused
    assert "outside the approved file scope" in result.output


def test_edit_with_missing_anchor_is_refused(tools):
    result = tools.edit_file("workshop/inventory.py", "no such text", "y")
    assert not result.ok and result.refused
    assert "anchor not found" in result.output


def test_edit_with_ambiguous_anchor_is_refused(tools):
    result = tools.edit_file("workshop/inventory.py", "qty", "qty")
    assert not result.ok and result.refused
    assert "occurs" in result.output


def test_run_tests_only_accepts_test_files(tools):
    refused = tools.run_tests(["workshop/inventory.py"])
    assert not refused.ok and refused.refused
    refused = tools.run_tests(["../outside/test_x.py"])
    assert not refused.ok and refused.refused
    refused = tools.run_tests([])
    assert not refused.ok and refused.refused


def test_run_tests_runs_the_workshops_own_tests(tools):
    result = tools.run_tests(["workshop/tests/test_inventory.py"])
    assert result.ok, result.output


def test_env_is_scrubbed_of_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("BASETEN_API_KEY", "should-not-leak")
    monkeypatch.setenv("SOME_SECRET_TOKEN", "should-not-leak")
    env = scrubbed_env(tmp_path)
    assert "BASETEN_API_KEY" not in env
    assert "SOME_SECRET_TOKEN" not in env
    assert env["PYTHONPATH"] == str(tmp_path)
    assert "PATH" in env
    assert os.environ["BASETEN_API_KEY"] == "should-not-leak"  # parent untouched
