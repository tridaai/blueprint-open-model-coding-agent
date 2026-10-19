"""Settings, policy, and model profiles.

The model layer is a profile: a named, versioned description of which
model serves the agent and through which endpoint. The loader is
fail-closed — an unknown profile name is a ConfigError, never a
silent fallback. Round 1 ships two profiles: ``offline`` (the
labelled scripted stand-in the tests, evals, and demo run) and
``baseten`` (the live profile's shape — endpoint, key env, default
slug; its adapter lands in round 2).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ConfigError

REPO_ROOT = Path(__file__).resolve().parents[2]

BASETEN_BASE_URL = "https://inference.baseten.co/v1"
BASETEN_DEFAULT_MODEL = "zai-org/GLM-5"


@dataclass(frozen=True)
class ModelProfile:
    """One servable model configuration."""

    profile_id: str
    label: str
    kind: str  # "offline" | "baseten"
    base_url: str | None = None
    api_key_env: str | None = None
    model: str | None = None


PROFILES: dict[str, ModelProfile] = {
    "offline": ModelProfile(
        profile_id="offline",
        label="Offline scripted model (labelled stand-in — no model is called)",
        kind="offline",
    ),
    "baseten": ModelProfile(
        profile_id="baseten",
        label="Baseten Model API (live adapter lands in round 2)",
        kind="baseten",
        base_url=BASETEN_BASE_URL,
        api_key_env="BASETEN_API_KEY",
        model=BASETEN_DEFAULT_MODEL,
    ),
}


def load_profile(profile_id: str) -> ModelProfile:
    """Resolve a profile by id, refusing unknown names loudly."""
    try:
        return PROFILES[profile_id]
    except KeyError:
        valid = ", ".join(sorted(PROFILES))
        raise ConfigError(
            f"unknown model profile {profile_id!r}; valid profiles: {valid}"
        ) from None


@dataclass(frozen=True)
class Policy:
    """Governed ceilings for one run. Deeper budgets land in round 6."""

    max_steps: int = 40
    test_timeout_seconds: int = 120


@dataclass
class Settings:
    sqlite_path: str = "./data/codingagent.db"
    database_url: str | None = None
    model_profile: str = "offline"
    migrations_dir: Path = field(default_factory=lambda: REPO_ROOT / "migrations")
    gold_dir: Path = field(default_factory=lambda: REPO_ROOT / "evals" / "gold")
    targets_dir: Path = field(default_factory=lambda: REPO_ROOT / "data" / "targets")
    runs_dir: Path = field(default_factory=lambda: REPO_ROOT / "data" / "runs")
    artifacts_dir: Path = field(
        default_factory=lambda: REPO_ROOT / "data" / "artifacts"
    )
    policy: Policy = field(default_factory=Policy)

    @property
    def store_kind(self) -> str:
        return "postgres" if self.database_url else "sqlite"

    @property
    def profile(self) -> ModelProfile:
        return load_profile(self.model_profile)


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
        ),
    }
    env.update(overrides)
    settings = Settings(**env)
    # Fail closed at load: an unknown profile never reaches a run.
    settings.profile
    return settings
