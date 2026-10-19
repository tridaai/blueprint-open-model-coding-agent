"""Settings and policy.

Which model serves the agent is a profile: a named, versioned JSON
record in the profiles directory (see :mod:`.profiles`). Settings
resolve the named profile at load, fail-closed — an unknown profile
name is a ConfigError, never a silent fallback. A live profile whose
key env is unset refuses at startup, by name, for the same reason:
the deployment asked for a live model, and quietly running the
offline stand-in instead would be a lie about what served the work.

Deployment overrides come from the environment and are validated
against the profile's own record: ``BASETEN_MODEL`` may pin another
slug only inside the selected profile's allowlist, and
``BASETEN_BASE_URL`` may repoint the endpoint (another Baseten
deployment of the same APIs). Neither can widen a profile.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ConfigError
from .profiles import ModelProfile, apply_overrides
from .profiles import load_profile as _load_profile_record

REPO_ROOT = Path(__file__).resolve().parents[2]

BASETEN_BASE_URL = "https://inference.baseten.co/v1"
# The model this blueprint is built and smoke-tested with: the
# baseten-deepseek profile's default, verified against the live
# Model APIs catalog on 2026-10-19.
BASETEN_DEFAULT_MODEL = "deepseek-ai/DeepSeek-V4-Flash-0731"


def load_profile(profile_id: str, profiles_dir: Path | None = None) -> ModelProfile:
    """Resolve a profile by id, refusing unknown names loudly."""
    return _load_profile_record(profile_id, profiles_dir or (REPO_ROOT / "profiles"))


def resolve_profile(settings: "Settings", profile_id: str) -> ModelProfile:
    """The effective record for a profile under this deployment:
    the record itself, with the deployment's environment overrides
    applied and validated against the record, and — for a live
    profile — the key check :func:`load_settings` performs at
    startup. The configured profile resolves through
    ``Settings.profile``; any other profile that comes to serve —
    a promoted default, a benchmark candidate — resolves through
    here, so the rules are identical whichever profile serves: an
    override never widens a profile, and a live profile without
    its key refuses by name rather than falling back."""
    profile = _load_profile_record(profile_id, settings.profiles_dir)
    if not profile.is_live:
        return profile
    if not profile.servable:
        raise profile.servability_refusal()
    effective = apply_overrides(
        profile,
        model=os.environ.get("BASETEN_MODEL") or None,
        base_url=os.environ.get("BASETEN_BASE_URL") or None,
    )
    if not os.environ.get(effective.api_key_env or ""):
        raise ConfigError(
            f"model profile {effective.profile_id!r} is live and needs "
            f"{effective.api_key_env} set in the environment; refusing "
            f"to serve it without one (no silent fallback to offline)"
        )
    return effective


@dataclass(frozen=True)
class Policy:
    """Governed ceilings for one run, enforced in the shell.

    The step ceiling has been here since round 1; round 6 adds the
    priced budgets: a ceiling on model calls, on provider-measured
    tokens, and on active wall-clock seconds. A run snapshots all
    of them at start, so a policy change mid-flight never moves a
    running run's ceilings; a resumed run spends against the same
    snapshot and the same accumulated usage.
    """

    max_steps: int = 40
    test_timeout_seconds: int = 120
    max_model_calls: int = 200
    max_tokens: int = 2_000_000
    max_wall_seconds: int = 1800


@dataclass
class Settings:
    sqlite_path: str = "./data/codingagent.db"
    database_url: str | None = None
    model_profile: str = "offline"
    profiles_dir: Path = field(default_factory=lambda: REPO_ROOT / "profiles")
    prices_dir: Path = field(default_factory=lambda: REPO_ROOT / "prices")
    # Test seam: an injected HTTP transport for the live adapter.
    # Never set from the environment; production uses the real one.
    model_transport: object | None = field(default=None, repr=False)
    migrations_dir: Path = field(default_factory=lambda: REPO_ROOT / "migrations")
    gold_dir: Path = field(default_factory=lambda: REPO_ROOT / "evals" / "gold")
    targets_dir: Path = field(default_factory=lambda: REPO_ROOT / "data" / "targets")
    runs_dir: Path = field(default_factory=lambda: REPO_ROOT / "data" / "runs")
    artifacts_dir: Path = field(
        default_factory=lambda: REPO_ROOT / "data" / "artifacts"
    )
    policy: Policy = field(default_factory=Policy)
    _effective_profile: ModelProfile | None = field(
        default=None, repr=False, compare=False
    )

    @property
    def store_kind(self) -> str:
        return "postgres" if self.database_url else "sqlite"

    @property
    def profile(self) -> ModelProfile:
        if self._effective_profile is not None:
            return self._effective_profile
        return load_profile(self.model_profile, self.profiles_dir)


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from None


def load_settings(**overrides) -> Settings:
    """Build settings from the environment, with explicit overrides
    (tests and the demo pass tmp paths this way)."""
    env = {
        "sqlite_path": os.environ.get("SQLITE_PATH", "./data/codingagent.db"),
        "database_url": os.environ.get("DATABASE_URL") or None,
        "model_profile": os.environ.get("MODEL_PROFILE", "offline"),
        "profiles_dir": Path(os.environ.get("PROFILES_DIR", REPO_ROOT / "profiles")),
        "prices_dir": Path(os.environ.get("PRICES_DIR", REPO_ROOT / "prices")),
        "migrations_dir": Path(os.environ.get("MIGRATIONS_DIR", REPO_ROOT / "migrations")),
        "gold_dir": Path(os.environ.get("GOLD_DIR", REPO_ROOT / "evals" / "gold")),
        "targets_dir": Path(os.environ.get("TARGETS_DIR", REPO_ROOT / "data" / "targets")),
        "runs_dir": Path(os.environ.get("RUNS_DIR", REPO_ROOT / "data" / "runs")),
        "artifacts_dir": Path(
            os.environ.get("ARTIFACTS_DIR", REPO_ROOT / "data" / "artifacts")
        ),
        "policy": Policy(
            max_steps=_int_env("CODINGAGENT_MAX_STEPS", 40),
            test_timeout_seconds=_int_env("CODINGAGENT_TEST_TIMEOUT", 120),
            max_model_calls=_int_env("CODINGAGENT_MAX_MODEL_CALLS", 200),
            max_tokens=_int_env("CODINGAGENT_MAX_TOKENS", 2_000_000),
            max_wall_seconds=_int_env("CODINGAGENT_MAX_WALL_SECONDS", 1800),
        ),
    }
    env.update(overrides)
    settings = Settings(**env)
    # Fail closed at load: an unknown profile never reaches a run.
    profile = settings.profile
    if profile.is_live:
        # A profile the provider does not currently serve refuses
        # here, with the reason its record carries — before the key
        # check, because servability is the standing fact about the
        # catalog, not about this deployment's secrets.
        if not profile.servable:
            raise profile.servability_refusal()
        effective = apply_overrides(
            profile,
            model=os.environ.get("BASETEN_MODEL") or None,
            base_url=os.environ.get("BASETEN_BASE_URL") or None,
        )
        settings._effective_profile = effective
        # A live profile without its key refuses here, by name —
        # never a silent fall back to the offline stand-in.
        if not os.environ.get(effective.api_key_env or ""):
            raise ConfigError(
                f"model profile {effective.profile_id!r} is live and needs "
                f"{effective.api_key_env} set in the environment; refusing "
                f"to start without it (no silent fallback to offline)"
            )
    return settings
