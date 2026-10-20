"""The skills library (round 19): governed, versioned method
bundles for the agent, kept as files and loaded fail-closed.

A skill is a named instruction pack — a method the agent is asked
to work by ("test-first repair", "minimal-diff discipline") —
that a work order may mount into the planning and execution
context. The content lives in the repo as files: one Markdown
file per version under ``skills/<name>/v<N>.md``, plus a small
index (``skills/index.json``) that names every skill, every
version it carries, the version's author, and the SHA-256 content
hash of the file. The loader validates the index against the
files exactly the way the price table and the taxonomy loaders
validate their records: an unknown field, a version the index
does not carry, a file the layout does not name, or a content
hash that no longer matches the file refuses at startup by name
— a deployment never serves a skill whose text is not the text
its index vouches for.

The library answers only what the files say: the records, the
content, the hashes. Whether a version may be *mounted* — its
publication standing, earned through the eval gate — is the
service's question, judged from the store's records; the loader
knows nothing of it, the same separation the price table keeps
from the cost ledger.

``write_skill`` is the authoring act's file side: it writes a
new version's content file and the index entry that vouches for
it, assigning the next version number, so a hand-authored index
and a helper-authored one meet the same loader. Authoring is a
file act, like publishing a new price-table version; it creates
a *recorded* version — usable only once the eval gate publishes
it, by a second actor, in the service.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ConfigError

INDEX_VERSION = 1
INDEX_NAME = "index.json"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_INDEX_FIELDS = {"version", "skills"}
_SKILL_FIELDS = {"name", "description", "versions"}
_VERSION_FIELDS = {"version", "file", "author", "content_hash"}


def _refuse(message: str) -> ConfigError:
    return ConfigError(f"skill library: {message}")


def content_hash(content: str) -> str:
    """The hash a version is cited by: SHA-256 over the exact
    text of its content file, as written."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SkillVersion:
    name: str
    version: int
    author: str
    content_hash: str
    content: str
    file: str


@dataclass(frozen=True)
class SkillRecord:
    name: str
    description: str
    versions: dict[int, SkillVersion] = field(default_factory=dict)

    def latest_version(self) -> int:
        return max(self.versions)

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "versions": [
                {
                    "version": version.version,
                    "author": version.author,
                    "content_hash": version.content_hash,
                    "file": version.file,
                }
                for version in (
                    self.versions[key] for key in sorted(self.versions)
                )
            ],
        }


@dataclass(frozen=True)
class SkillLibrary:
    """The loaded library: every skill the index carries, with
    each version's content and hash verified at load."""

    records: dict[str, SkillRecord]
    index_version: int = INDEX_VERSION

    def get(self, name: str) -> SkillRecord | None:
        return self.records.get(name)

    def get_version(self, name: str, version: int) -> SkillVersion | None:
        record = self.records.get(name)
        if record is None:
            return None
        return record.versions.get(version)

    def names(self) -> list[str]:
        return sorted(self.records)


def _check_fields(record: dict, allowed: set[str], what: str) -> None:
    unknown = sorted(set(record) - allowed)
    if unknown:
        raise _refuse(f"{what} carries unknown field(s): {', '.join(unknown)}")
    missing = sorted(allowed - set(record))
    if missing:
        raise _refuse(f"{what} is missing field(s): {', '.join(missing)}")


def load_skill_library(skills_dir: Path) -> SkillLibrary:
    """Load and vouch for the library the directory carries.

    Fail-closed, in the price table's mould: the index must
    exist, carry only known fields, and agree with the files —
    every version entry names the file the layout prescribes for
    it, the file exists, and its content hashes to the recorded
    hash. Any disagreement refuses at load with the skill and
    version named, so a drifted or half-written library never
    serves."""
    skills_dir = Path(skills_dir)
    index_path = skills_dir / INDEX_NAME
    if not index_path.is_file():
        raise _refuse(
            f"no skill index at {index_path} — the library is governed "
            "data; a deployment without one does not start"
        )
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise _refuse(f"the index at {index_path} is not valid JSON: {exc}") from exc
    if not isinstance(index, dict):
        raise _refuse("the index is not a JSON object")
    _check_fields(index, _INDEX_FIELDS, "the index")
    if index["version"] != INDEX_VERSION:
        raise _refuse(
            f"index version {index['version']!r} is not the version this "
            f"build reads ({INDEX_VERSION})"
        )
    if not isinstance(index["skills"], list):
        raise _refuse("the index's skills must be a list")
    records: dict[str, SkillRecord] = {}
    for entry in index["skills"]:
        if not isinstance(entry, dict):
            raise _refuse("a skill entry in the index is not an object")
        _check_fields(entry, _SKILL_FIELDS, "a skill entry")
        name = entry["name"]
        if not isinstance(name, str) or not NAME_RE.match(name):
            raise _refuse(
                f"skill name {name!r} is not a clean skill name "
                "(lowercase letters, digits and hyphens)"
            )
        if name in records:
            raise _refuse(f"skill {name!r} is carried by the index twice")
        description = entry["description"]
        if not isinstance(description, str) or not description.strip():
            raise _refuse(f"skill {name!r} carries no description")
        if not isinstance(entry["versions"], list) or not entry["versions"]:
            raise _refuse(f"skill {name!r} carries no versions")
        versions: dict[int, SkillVersion] = {}
        for raw in entry["versions"]:
            if not isinstance(raw, dict):
                raise _refuse(f"a version entry of skill {name!r} is not an object")
            _check_fields(raw, _VERSION_FIELDS, f"a version entry of skill {name!r}")
            number = raw["version"]
            if not isinstance(number, int) or isinstance(number, bool) or number < 1:
                raise _refuse(
                    f"skill {name!r} carries a version that is not a "
                    f"positive integer: {number!r}"
                )
            if number in versions:
                raise _refuse(f"skill {name!r} carries version {number} twice")
            expected_file = f"{name}/v{number}.md"
            if raw["file"] != expected_file:
                raise _refuse(
                    f"skill {name!r} version {number} names file "
                    f"{raw['file']!r}; the layout carries it at "
                    f"{expected_file!r} and nowhere else"
                )
            author = raw["author"]
            if not isinstance(author, str) or not author.strip():
                raise _refuse(
                    f"skill {name!r} version {number} names no author"
                )
            path = skills_dir / expected_file
            if not path.is_file():
                raise _refuse(
                    f"skill {name!r} version {number} names {expected_file} "
                    "but the file is not in the library"
                )
            content = path.read_text(encoding="utf-8")
            if not content.strip():
                raise _refuse(
                    f"skill {name!r} version {number} carries no content"
                )
            digest = content_hash(content)
            if raw["content_hash"] != digest:
                raise _refuse(
                    f"skill {name!r} version {number}: the content hash "
                    f"does not match the file — the index records "
                    f"{raw['content_hash']!r} and the file hashes to "
                    f"{digest!r}; the text on disk is not the text the "
                    "index vouches for"
                )
            versions[number] = SkillVersion(
                name=name,
                version=number,
                author=author,
                content_hash=digest,
                content=content,
                file=expected_file,
            )
        records[name] = SkillRecord(
            name=name, description=description, versions=versions
        )
    return SkillLibrary(records=records, index_version=index["version"])


def write_skill(
    skills_dir: Path,
    *,
    name: str,
    description: str,
    author: str,
    content: str,
) -> dict:
    """Author a new version of a skill: write its content file
    and the index entry that vouches for it, and return the
    version's index entry.

    The version assigned is the next integer for the name — an
    existing skill keeps its description unless the author
    restates one; a new skill takes the description given. The
    write is the file side of authoring only: the version stands
    *recorded*, and nothing here publishes it. A directory with
    no index yet starts an empty library; a directory whose
    index fails the loader refuses here too, so authoring never
    builds on a library the loader would not serve."""
    skills_dir = Path(skills_dir)
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise _refuse(
            f"skill name {name!r} is not a clean skill name "
            "(lowercase letters, digits and hyphens)"
        )
    if not author or not author.strip():
        raise _refuse("a skill version names its author")
    if not content or not content.strip():
        raise _refuse(f"skill {name!r}: the version carries no content")
    index_path = skills_dir / INDEX_NAME
    if index_path.is_file():
        # The standing library must load before anything is added
        # to it — authoring onto drift is how drift spreads.
        load_skill_library(skills_dir)
        index = json.loads(index_path.read_text(encoding="utf-8"))
    else:
        index = {"version": INDEX_VERSION, "skills": []}
    entry = next((s for s in index["skills"] if s["name"] == name), None)
    if entry is None:
        if not description or not description.strip():
            raise _refuse(f"skill {name!r} carries no description")
        entry = {"name": name, "description": description, "versions": []}
        index["skills"].append(entry)
    elif description and description.strip():
        entry["description"] = description
    number = max((v["version"] for v in entry["versions"]), default=0) + 1
    text = content if content.endswith("\n") else content + "\n"
    version_entry = {
        "version": number,
        "file": f"{name}/v{number}.md",
        "author": author,
        "content_hash": content_hash(text),
    }
    entry["versions"].append(version_entry)
    target_dir = skills_dir / name
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / f"v{number}.md").write_text(text, encoding="utf-8")
    index_path.write_text(
        json.dumps(index, indent=2) + "\n", encoding="utf-8"
    )
    # The library as written must be the library the loader
    # serves — prove it before the act reports success.
    load_skill_library(skills_dir)
    return version_entry
