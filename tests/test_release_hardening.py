"""Release hardening (round 20): the shipped artifacts agree
with the code that runs from them.

Two static contracts, checked on every run of the suite — no
Docker daemon and no Postgres needed, because both defects they
guard are visible in the files themselves:

- The Dockerfile must carry every runtime data directory the
  app loads, and point its ``*_DIR`` setting at the copy. The
  package installs into site-packages in the image, so the
  repo-root defaults in ``config.py`` do not resolve there:
  a governed data directory that is neither copied nor named
  by its env var is a deployment that refuses to start — the
  loaders are fail-closed, so the failure is loud, total, and
  only visible if someone boots the image. (Before this round
  the image copied neither ``profiles/``, ``prices/``, nor
  ``taxonomy/``, and named no directory but gold and
  migrations: the shipped image could not have started.)

- The two migration dialects must carry the same versions and
  the same tables. The store runs one chain or the other by
  configuration; a migration that lands in only one dialect is
  a schema the other profile discovers at first write.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Each governed/runtime data directory the settings resolve, and
# the env var that must point the image at its copy. The workshop
# has no env var: the service derives it from the gold directory,
# so a COPY is its whole contract.
DATA_DIRS = {
    "profiles": "PROFILES_DIR",
    "prices": "PRICES_DIR",
    "taxonomy": "TAXONOMY_DIR",
    "skills": "SKILLS_DIR",
    "migrations": "MIGRATIONS_DIR",
    "evals/gold": "GOLD_DIR",
    "workshop": None,
}


def _dockerfile() -> str:
    return (REPO_ROOT / "Dockerfile").read_text()


def _copied_paths(text: str) -> set[str]:
    """COPY destinations, normalised against the runtime stage's
    WORKDIR (/app): the Dockerfile writes them as ./<dir>."""
    copied = set()
    for line in text.splitlines():
        match = re.match(r"\s*COPY\s+(?:--from=\S+\s+)?(\S+)\s+(\S+)", line)
        if match:
            dest = match.group(2).rstrip("/")
            if dest.startswith("./"):
                dest = "/app/" + dest[2:]
            copied.add(dest)
    return copied


def _env_assignments(text: str) -> dict[str, str]:
    env = {}
    for match in re.finditer(r"([A-Z_]+)=([^\s\\]+)", text):
        env[match.group(1)] = match.group(2)
    return env


def test_the_image_carries_every_runtime_data_directory():
    text = _dockerfile()
    copied = _copied_paths(text)
    env = _env_assignments(text)
    for directory, env_var in DATA_DIRS.items():
        dest = f"/app/{directory}"
        assert dest in copied, (
            f"the Dockerfile never copies {directory}/ — the app "
            f"loads it at startup and the loaders refuse closed"
        )
        if env_var is not None:
            assert env.get(env_var) == dest, (
                f"{env_var} must point at the copied {directory}/ "
                f"in the image; the repo-root default does not "
                f"resolve under site-packages"
            )
    # The deployment profiles live one level down, under the
    # model profiles directory; their env var must name the
    # subdirectory itself, not its parent.
    assert env.get("DEPLOYMENT_PROFILES_DIR") == "/app/profiles/deployment"


def _migration_tables(folder: Path) -> dict[str, set[str]]:
    tables: dict[str, set[str]] = {}
    for path in sorted(folder.glob("[0-9]*.sql")):
        tables[path.stem] = set(
            re.findall(
                r"CREATE TABLE IF NOT EXISTS\s+(\w+)",
                path.read_text(),
            )
        )
    return tables


def test_the_migration_dialects_carry_the_same_schema():
    postgres = _migration_tables(REPO_ROOT / "migrations")
    sqlite = _migration_tables(REPO_ROOT / "migrations" / "sqlite")
    assert set(postgres) == set(sqlite), (
        "migration versions differ between dialects: "
        f"postgres-only {sorted(set(postgres) - set(sqlite))}, "
        f"sqlite-only {sorted(set(sqlite) - set(postgres))}"
    )
    for version in postgres:
        assert postgres[version] == sqlite[version], (
            f"migration {version} creates different tables per "
            f"dialect: postgres {sorted(postgres[version])}, "
            f"sqlite {sorted(sqlite[version])}"
        )
