"""Deployment profiles (round 16): naming binds the rules.

The deployment's shape is a governed record (profiles/deployment/),
loaded fail-closed like the model profiles and the price table.
These tests hold the round's claims:

- the three shipped records load, and the loader refuses records
  it does not fully understand (unknown field, unknown rule kind,
  undeclared rule, unknown requirement value, name/file mismatch);
- each shipped profile loads settings clean under its own valid
  environment, and each rule refuses by name under a violating
  one — the offline model under hosted, SQLite under hosted, a
  Baseten host under hosted-self, a missing DATABASE_URL, a
  missing key;
- the default (no DEPLOYMENT_PROFILE anywhere) is local and
  changes nothing — the rest of the suite is the standing proof;
- readiness reports the profile's name, version, content hash,
  and per-rule states; the digest names it; a run record is
  stamped with the profile it ran under.

No test connects anywhere: the hosted matrix stops at settings
load, which is where the rules bind — the store is never built
against the fake DATABASE_URL these tests set.
"""

from __future__ import annotations

import json

import pytest

from open_model_coding_agent.config import REPO_ROOT, load_profile, load_settings
from open_model_coding_agent.deployments import (
    evaluate_rules,
    list_deployment_profiles,
    load_deployment_profile,
    violated_rules,
)
from open_model_coding_agent.errors import ConfigError
from open_model_coding_agent.service import CodingService

DEPLOYMENT_DIR = REPO_ROOT / "profiles" / "deployment"
FAKE_DATABASE_URL = "postgresql://codingagent:codingagent@db:5432/codingagent"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in (
        "BASETEN_API_KEY",
        "BASETEN_MODEL",
        "BASETEN_BASE_URL",
        "MODEL_PROFILE",
        "DEPLOYMENT_PROFILE",
        "DATABASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)


def _record(**overrides):
    record = {
        "name": "custom",
        "version": 1,
        "label": "Custom",
        "description": "A deployment shape a test wrote.",
        "rules": {
            "model": "any",
            "store": "any",
            "api_key": "profile",
            "endpoint": "any",
        },
    }
    record.update(overrides)
    return record


def _write(directory, record, filename="custom.json"):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    path.write_text(json.dumps(record))
    return path


# -- the shipped records and the loader ------------------------------------


def test_the_shipped_profiles_load_with_their_rules():
    profiles = {p.name: p for p in list_deployment_profiles(DEPLOYMENT_DIR)}
    assert sorted(profiles) == ["hosted", "hosted-self", "local"]
    assert profiles["local"].rules == {
        "model": "any",
        "store": "any",
        "api_key": "profile",
        "endpoint": "any",
    }
    assert profiles["hosted"].rules["model"] == "live"
    assert profiles["hosted"].rules["store"] == "postgres"
    assert profiles["hosted"].rules["api_key"] == "required"
    assert profiles["hosted-self"].rules["endpoint"] == "not_baseten"
    for profile in profiles.values():
        assert profile.version >= 1
        # The content hash is the file's SHA-256: 64 hex chars,
        # stable across loads of the same record.
        assert len(profile.content_hash) == 64
        again = load_deployment_profile(profile.name, DEPLOYMENT_DIR)
        assert again.content_hash == profile.content_hash


def test_an_unknown_deployment_profile_refuses_listing_the_valid(monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_PROFILE", "production")
    with pytest.raises(ConfigError) as excinfo:
        load_settings()
    message = str(excinfo.value)
    assert "unknown deployment profile 'production'" in message
    assert "hosted" in message and "local" in message


@pytest.mark.parametrize(
    "mutate, fragment",
    [
        (lambda r: r.update({"surprise": True}), "unknown field"),
        (lambda r: r.update({"name": "other"}), "must agree"),
        (lambda r: r.update({"version": 0}), "positive integer"),
        (lambda r: r["rules"].update({"network": "none"}), "unknown rule kind"),
        (lambda r: r["rules"].pop("store"), "undeclared"),
        (lambda r: r["rules"].update({"model": "fast"}), "requires one of"),
    ],
)
def test_the_loader_refuses_records_it_does_not_understand(tmp_path, mutate, fragment):
    record = _record()
    mutate(record)
    _write(tmp_path, record)
    with pytest.raises(ConfigError) as excinfo:
        load_deployment_profile("custom", tmp_path)
    assert fragment in str(excinfo.value)


def test_the_loader_refuses_malformed_json(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "custom.json").write_text("{not json")
    with pytest.raises(ConfigError, match="not valid JSON"):
        load_deployment_profile("custom", tmp_path)


# -- the default ------------------------------------------------------------


def test_the_default_deployment_is_local_and_unbound(tmp_path):
    settings = load_settings(sqlite_path=str(tmp_path / "codingagent.db"))
    assert settings.deployment_profile == "local"
    assert settings.deployment.name == "local"
    states = evaluate_rules(
        settings.deployment,
        model_profile=settings.profile,
        store_kind=settings.store_kind,
        environ={},
    )
    assert violated_rules(states) == []


# -- the hosted matrix --------------------------------------------------------


def _hosted_env(monkeypatch, *, database_url=True, key=True):
    monkeypatch.setenv("DEPLOYMENT_PROFILE", "hosted")
    monkeypatch.setenv("MODEL_PROFILE", "baseten-glm")
    if key:
        monkeypatch.setenv("BASETEN_API_KEY", "test-key")
    if database_url:
        monkeypatch.setenv("DATABASE_URL", FAKE_DATABASE_URL)


def test_hosted_refuses_the_offline_model_by_name(monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_PROFILE", "hosted")
    monkeypatch.setenv("DATABASE_URL", FAKE_DATABASE_URL)
    with pytest.raises(ConfigError) as excinfo:
        load_settings()
    message = str(excinfo.value)
    assert "deployment profile 'hosted'" in message
    assert "rule 'model' requires 'live'" in message


def test_hosted_refuses_sqlite_by_name(monkeypatch):
    _hosted_env(monkeypatch, database_url=False)
    with pytest.raises(ConfigError) as excinfo:
        load_settings()
    message = str(excinfo.value)
    assert "deployment profile 'hosted'" in message
    assert "rule 'store' requires 'postgres'" in message


def test_hosted_refuses_a_missing_key(monkeypatch):
    _hosted_env(monkeypatch, key=False)
    with pytest.raises(ConfigError) as excinfo:
        load_settings()
    # The standing live-profile key refusal fires at load, by name.
    assert "BASETEN_API_KEY" in str(excinfo.value)


def test_hosted_loads_clean_under_its_own_valid_env(monkeypatch):
    _hosted_env(monkeypatch)
    settings = load_settings()
    assert settings.deployment.name == "hosted"
    assert settings.store_kind == "postgres"
    states = evaluate_rules(
        settings.deployment,
        model_profile=settings.profile,
        store_kind=settings.store_kind,
        environ={"BASETEN_API_KEY": "test-key"},
    )
    assert violated_rules(states) == []


def test_local_permits_a_live_profile_on_sqlite(monkeypatch, tmp_path):
    # Local binds nothing beyond the model profile's own rules: a
    # developer may serve a live model against the local store.
    monkeypatch.setenv("MODEL_PROFILE", "baseten-glm")
    monkeypatch.setenv("BASETEN_API_KEY", "test-key")
    settings = load_settings(sqlite_path=str(tmp_path / "codingagent.db"))
    assert settings.deployment.name == "local"
    assert settings.store_kind == "sqlite"


# -- the hosted-self matrix ---------------------------------------------------


def _hosted_self_env(monkeypatch, *, base_url=None):
    monkeypatch.setenv("DEPLOYMENT_PROFILE", "hosted-self")
    monkeypatch.setenv("MODEL_PROFILE", "baseten-glm")
    monkeypatch.setenv("BASETEN_API_KEY", "test-key")
    monkeypatch.setenv("DATABASE_URL", FAKE_DATABASE_URL)
    if base_url:
        monkeypatch.setenv("BASETEN_BASE_URL", base_url)


def test_hosted_self_refuses_the_profiles_own_baseten_endpoint(monkeypatch):
    _hosted_self_env(monkeypatch)
    with pytest.raises(ConfigError) as excinfo:
        load_settings()
    message = str(excinfo.value)
    assert "deployment profile 'hosted-self'" in message
    assert "rule 'endpoint' requires 'not_baseten'" in message
    assert "inference.baseten.co" in message


def test_hosted_self_refuses_a_baseten_host_named_outright(monkeypatch):
    _hosted_self_env(monkeypatch, base_url="https://inference.baseten.co/v1")
    with pytest.raises(ConfigError, match="not_baseten"):
        load_settings()


def test_hosted_self_loads_clean_against_a_self_hosted_endpoint(monkeypatch):
    _hosted_self_env(monkeypatch, base_url="https://models.internal.example/v1")
    settings = load_settings()
    assert settings.deployment.name == "hosted-self"
    assert settings.profile.base_url == "https://models.internal.example/v1"
    states = evaluate_rules(
        settings.deployment,
        model_profile=settings.profile,
        store_kind=settings.store_kind,
        environ={"BASETEN_API_KEY": "test-key"},
    )
    assert violated_rules(states) == []


# -- the evaluator, rule by rule ----------------------------------------------


def test_the_evaluator_states_each_rule():
    hosted = load_deployment_profile("hosted", DEPLOYMENT_DIR)
    offline = load_profile("offline", REPO_ROOT / "profiles")
    states = evaluate_rules(
        hosted, model_profile=offline, store_kind="sqlite", environ={}
    )
    by_rule = {state["rule"]: state for state in states}
    assert by_rule["model"]["state"] == "violated"
    assert by_rule["store"]["state"] == "violated"
    # No live profile selected: no key is presented, so the
    # required-key rule is violated too, and says why.
    assert by_rule["api_key"]["state"] == "violated"
    assert "no live model profile" in by_rule["api_key"]["detail"]
    # hosted places no constraint on the endpoint host.
    assert by_rule["endpoint"]["state"] == "satisfied"

    glm = load_profile("baseten-glm", REPO_ROOT / "profiles")
    states = evaluate_rules(
        hosted,
        model_profile=glm,
        store_kind="postgres",
        environ={"BASETEN_API_KEY": "test-key"},
    )
    assert violated_rules(states) == []

    hosted_self = load_deployment_profile("hosted-self", DEPLOYMENT_DIR)
    states = evaluate_rules(
        hosted_self,
        model_profile=glm,
        store_kind="postgres",
        environ={"BASETEN_API_KEY": "test-key"},
    )
    by_rule = {state["rule"]: state for state in states}
    assert by_rule["endpoint"]["state"] == "violated"


# -- the surfaces: readiness, digest, the run record ---------------------------


def test_readiness_reports_the_deployment_profile_and_its_rules(settings):
    readiness = CodingService(settings).readiness()
    local = load_deployment_profile("local", DEPLOYMENT_DIR)
    assert readiness["deployment_profile"] == "local"
    assert readiness["deployment_profile_version"] == local.version
    assert readiness["deployment_profile_hash"] == local.content_hash
    assert readiness["status"] == "ready"
    rules = {state["rule"]: state for state in readiness["deployment_rules"]}
    assert set(rules) == {"model", "store", "api_key", "endpoint"}
    assert all(state["state"] == "satisfied" for state in rules.values())


def test_the_digest_names_the_deployment_profile(service):
    digest = service.operations_digest()
    assert digest["deployment_profile"] == "local"
    assert digest["deployment_profile_version"] == 1


def test_a_run_record_is_stamped_with_its_deployment_profile(service, approved_order):
    run = service.start_run(approved_order["work_order_id"], actor="tester")
    assert run["deployment_profile"] == "local"
    stored = service.store.get_run(run["run_id"])
    assert stored["deployment_profile"] == "local"
