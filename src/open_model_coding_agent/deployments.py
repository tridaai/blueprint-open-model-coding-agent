"""Deployment profiles: versioned JSON records, loaded fail-closed.

Where the model profiles (see :mod:`.profiles`) govern *which
model* serves, a deployment profile governs the *shape of the
deployment* — whether the offline scripted model may serve at all,
which store backs the shell, whether a provider key must be
present, and whose hosts the endpoint may live on. A deployment
profile is data, not code: one JSON record per named shape in the
deployment profiles directory (``profiles/deployment/``), loaded
fail-closed in the model profiles' mould — an unknown profile name,
an unknown field, a rule kind the loader does not recognise, or a
requirement value it does not understand each refuse by name at
startup. Naming a profile binds its rules: the selection is one
environment variable (``DEPLOYMENT_PROFILE``), and from that name
the rules follow — a deployment that calls itself ``hosted`` does
not get to run the offline model against SQLite because nobody
checked.

The rules themselves are declared on the record, as values from a
small governed vocabulary —

- ``model``: ``any`` (offline or live may serve) or ``live`` (the
  serving model profile must be a live one);
- ``store``: ``any`` or ``postgres`` (``DATABASE_URL`` must be set);
- ``api_key``: ``profile`` (whatever the model profile itself
  demands — the standing rule, which already refuses a live
  profile without its key) or ``required`` (a live profile's key
  must be present);
- ``endpoint``: ``any`` or ``not_baseten`` (the effective endpoint's
  host must not be baseten.co or one of its subdomains).

Each rule kind has exactly one checker in the table below, driven
by the record's declared value: a new deployment profile is new
data, and only a genuinely new rule kind costs a new checker.
Enforcement happens where settings load (:func:`.config.load_settings`)
— a violated rule is a ConfigError naming the profile and the rule,
raised before the app serves anything — and the same evaluation is
what readiness reports, per rule, so the states an operator reads
are the states the loader enforced, computed the same way.

Profile records are versioned and auditable as files, the way the
price table and the taxonomy are: a rule change is a new version
of the record, never a quiet edit of the version in force, and the
loader reports the loaded record's content hash (SHA-256 of the
file) so a readiness read names exactly which record governed the
deployment. There is deliberately no hash pinning: no governed
data in this repo pins one, and the version discipline is the
control — the hash exists so a drifted record is *visible*, in
readiness, next to the version it claims.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import urlparse

from .errors import ConfigError
from .profiles import ModelProfile

_FIELDS = {"name", "version", "label", "description", "rules"}

# The governed rule vocabulary: each kind, and the requirement
# values a record may declare for it. A record naming a kind or a
# value outside this table is a record the loader does not fully
# understand, and it refuses.
_RULE_VALUES: dict[str, tuple[str, ...]] = {
    "model": ("any", "live"),
    "store": ("any", "postgres"),
    "api_key": ("profile", "required"),
    "endpoint": ("any", "not_baseten"),
}


@dataclass(frozen=True)
class DeploymentProfile:
    """One deployment shape, as recorded."""

    name: str
    version: int
    label: str
    description: str
    rules: dict[str, str]
    content_hash: str


def _refuse(name: str, problem: str) -> ConfigError:
    return ConfigError(f"deployment profile {name!r}: {problem}")


def _parse_record(name: str, raw: object, source: Path, content_hash: str) -> DeploymentProfile:
    if not isinstance(raw, dict):
        raise _refuse(name, f"{source.name} is not a JSON object")
    unknown = sorted(set(raw) - _FIELDS)
    if unknown:
        raise _refuse(
            name,
            f"unknown field(s) {unknown} in {source.name}; the loader "
            f"refuses records it does not fully understand",
        )
    if raw.get("name") != name:
        raise _refuse(
            name,
            f"{source.name} records name {raw.get('name')!r}; the file "
            f"name and the record must agree",
        )
    version = raw.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise _refuse(name, f"version must be a positive integer, got {version!r}")
    label = raw.get("label")
    if not isinstance(label, str) or not label:
        raise _refuse(name, "label must be a non-empty string")
    description = raw.get("description")
    if not isinstance(description, str) or not description:
        raise _refuse(name, "description must be a non-empty string")
    rules = raw.get("rules")
    if not isinstance(rules, dict):
        raise _refuse(name, "rules must be an object naming one requirement per rule kind")
    unknown_kinds = sorted(set(rules) - set(_RULE_VALUES))
    if unknown_kinds:
        raise _refuse(
            name,
            f"unknown rule kind(s) {unknown_kinds} in {source.name}; the "
            f"loader recognises {sorted(_RULE_VALUES)}",
        )
    missing_kinds = sorted(set(_RULE_VALUES) - set(rules))
    if missing_kinds:
        raise _refuse(
            name,
            f"rule kind(s) {missing_kinds} undeclared in {source.name}; "
            f"a deployment profile declares every rule — an undeclared "
            f"rule would bind nothing and read as permission",
        )
    for kind, value in rules.items():
        if value not in _RULE_VALUES[kind]:
            raise _refuse(
                name,
                f"rule {kind!r} requires one of {_RULE_VALUES[kind]}, "
                f"got {value!r}",
            )
    return DeploymentProfile(
        name=name,
        version=version,
        label=label,
        description=description,
        rules=dict(rules),
        content_hash=content_hash,
    )


def load_deployment_profile(name: str, profiles_dir: Path) -> DeploymentProfile:
    """Resolve one deployment profile by name from the deployment
    profiles directory, refusing unknown names and malformed
    records by name."""
    path = Path(profiles_dir) / f"{name}.json"
    if not path.is_file():
        valid = sorted(p.stem for p in Path(profiles_dir).glob("*.json"))
        raise ConfigError(
            f"unknown deployment profile {name!r}; valid profiles: "
            f"{', '.join(valid)}"
        )
    blob = path.read_bytes()
    try:
        raw = json.loads(blob)
    except json.JSONDecodeError as exc:
        raise _refuse(name, f"{path.name} is not valid JSON: {exc}") from None
    return _parse_record(name, raw, path, hashlib.sha256(blob).hexdigest())


def list_deployment_profiles(profiles_dir: Path) -> list[DeploymentProfile]:
    """Every recorded deployment profile, loaded fail-closed (a
    broken record refuses the listing rather than being skipped)."""
    return [
        load_deployment_profile(path.stem, profiles_dir)
        for path in sorted(Path(profiles_dir).glob("*.json"))
    ]


# -- the rule checkers ------------------------------------------------------
#
# One checker per rule kind, driven by the record's declared value.
# A checker answers for one rule: the requirement as recorded, the
# state it finds (``satisfied`` or ``violated``), and the detail an
# operator reads — what was found, in the deployment's own terms.


def _check_model(requires: str, model_profile: ModelProfile, store_kind: str, environ: Mapping[str, str]) -> tuple[str, str]:
    if requires == "any":
        return "satisfied", "no constraint: the offline scripted model or a live profile may serve"
    if model_profile.is_live:
        return "satisfied", f"model profile {model_profile.profile_id!r} is live (kind {model_profile.kind!r})"
    return "violated", f"model profile {model_profile.profile_id!r} is the offline scripted model; a live model profile is required"


def _check_store(requires: str, model_profile: ModelProfile, store_kind: str, environ: Mapping[str, str]) -> tuple[str, str]:
    if requires == "any":
        return "satisfied", "no constraint: the SQLite local store or Postgres may back the shell"
    if store_kind == "postgres":
        return "satisfied", "the store is Postgres (DATABASE_URL is set)"
    return "violated", "the store is SQLite (DATABASE_URL is not set); Postgres is required"


def _check_api_key(requires: str, model_profile: ModelProfile, store_kind: str, environ: Mapping[str, str]) -> tuple[str, str]:
    if requires == "profile":
        return "satisfied", "the model profile's own key rule applies: a live profile refuses to load without its key, an offline profile needs none"
    if not model_profile.is_live:
        return "violated", "no live model profile is selected, so no provider key is presented; a key is required"
    if environ.get(model_profile.api_key_env or ""):
        return "satisfied", f"{model_profile.api_key_env} is set in the environment"
    return "violated", f"{model_profile.api_key_env} is not set in the environment; a key is required"


def _check_endpoint(requires: str, model_profile: ModelProfile, store_kind: str, environ: Mapping[str, str]) -> tuple[str, str]:
    if requires == "any":
        return "satisfied", "no constraint on the endpoint host"
    if not model_profile.is_live or not model_profile.base_url:
        return "violated", "no live endpoint is configured (the offline profile serves no endpoint); a self-hosted endpoint is required"
    host = urlparse(model_profile.base_url).hostname or ""
    if host == "baseten.co" or host.endswith(".baseten.co"):
        return "violated", f"the endpoint host {host!r} is a Baseten host; a self-hosted endpoint is required"
    return "satisfied", f"the endpoint host {host!r} is not a Baseten host"


_CHECKERS: dict[str, Callable[..., tuple[str, str]]] = {
    "model": _check_model,
    "store": _check_store,
    "api_key": _check_api_key,
    "endpoint": _check_endpoint,
}


def evaluate_rules(
    profile: DeploymentProfile,
    *,
    model_profile: ModelProfile,
    store_kind: str,
    environ: Mapping[str, str],
) -> list[dict]:
    """Evaluate the profile's declared rules against one concrete
    configuration — the effective model profile, the store kind,
    and the environment. Returns one state per rule, in the
    vocabulary's order: the requirement as recorded, the state
    found, and the detail. Settings load raises over the violated
    states; readiness reports all of them. The evaluation is the
    same in both places so the two can never disagree."""
    states: list[dict] = []
    for kind in _RULE_VALUES:
        requires = profile.rules[kind]
        state, detail = _CHECKERS[kind](requires, model_profile, store_kind, environ)
        states.append(
            {"rule": kind, "requires": requires, "state": state, "detail": detail}
        )
    return states


def violated_rules(states: list[dict]) -> list[dict]:
    """The states a load must refuse over."""
    return [state for state in states if state["state"] == "violated"]
