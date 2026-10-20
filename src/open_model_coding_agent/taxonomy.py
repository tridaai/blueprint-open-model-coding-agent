"""The failure taxonomy (round 15): governed data, loaded fail-closed.

Every named failure in this system — a GateError code the shell
raises, a refusal code the tool port returns as data, a failure
class a stored record carries — is classified exactly once, here:
a class, a likely cause, and the operator's next action. The
mapping is *data* (``taxonomy/taxonomy-v<N>.json``), versioned like
the price table: a change publishes a new version alongside the
old, and versions are never edited in place. The loader is
fail-closed in the profiles' mould — unknown fields, an entry
whose class is not declared, a duplicate name, or a version that
disagrees with its file name each refuse by name at startup, so a
deployment never runs with a half-written taxonomy.

Classification itself never refuses: a name the table does not
carry — a record written by a newer build, a name a future round
adds before the table catches up — classifies as ``unclassified``,
and the digest *counts* it under that name. Nothing is silently
dropped; an unclassified count above zero is the taxonomy telling
its maintainers it is behind the code. The conformance test holds
the other direction: every name the code can raise today is in
the shipped table, derived from the source, not hand-listed.

One entry is deliberately not a code-raised name: ``config_error``.
A ConfigError carries no stable code (its message is the record),
so configuration refusals classify under that single name.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ConfigError

_TABLE_FILE = re.compile(r"^taxonomy-v(\d+)\.json$")
_TOP_FIELDS = {"version", "as_of", "source", "classes", "entries"}
_CLASS_FIELDS = {"id", "label", "summary"}
_ENTRY_FIELDS = {"name", "class", "cause", "action"}

UNCLASSIFIED = "unclassified"


def _refuse(problem: str) -> ConfigError:
    return ConfigError(f"failure taxonomy: {problem}")


@dataclass(frozen=True)
class TaxonomyEntry:
    name: str
    class_id: str
    cause: str
    action: str

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "class": self.class_id,
            "cause": self.cause,
            "action": self.action,
        }


@dataclass(frozen=True)
class FailureTaxonomy:
    """One version of the taxonomy, as recorded."""

    version: int
    as_of: str
    source: str
    classes: dict[str, dict] = field(default_factory=dict)
    entries: dict[str, TaxonomyEntry] = field(default_factory=dict)

    def classify(self, name: str | None) -> dict:
        """The recorded classification for a failure name — or the
        unclassified standing for a name the table does not carry.
        An unclassified result is a fact to count, never an error:
        records outlive the build that wrote them."""
        if name is not None and name in self.entries:
            return self.entries[name].as_dict()
        return {
            "name": name,
            "class": UNCLASSIFIED,
            "cause": (
                "no taxonomy entry carries this name — the record "
                "was written by a build or a round this table "
                "predates"
            ),
            "action": (
                "extend the taxonomy with a new published version "
                "classifying this name; until then it counts as "
                "unclassified, never dropped"
            ),
        }

    def class_label(self, class_id: str) -> str:
        if class_id == UNCLASSIFIED:
            return "Unclassified"
        record = self.classes.get(class_id) or {}
        return record.get("label", class_id)


def _parse_taxonomy(raw: object, source: Path, file_version: int) -> FailureTaxonomy:
    if not isinstance(raw, dict):
        raise _refuse(f"{source.name} is not a JSON object")
    unknown = set(raw) - _TOP_FIELDS
    if unknown:
        raise _refuse(f"{source.name}: unknown field(s) {sorted(unknown)}")
    version = raw.get("version")
    if version != file_version:
        raise _refuse(
            f"{source.name}: version {version!r} disagrees with its "
            f"file name (v{file_version})"
        )
    classes: dict[str, dict] = {}
    raw_classes = raw.get("classes")
    if not isinstance(raw_classes, list) or not raw_classes:
        raise _refuse(f"{source.name}: classes must be a non-empty list")
    for record in raw_classes:
        if not isinstance(record, dict) or set(record) - _CLASS_FIELDS:
            raise _refuse(f"{source.name}: a class record is malformed")
        class_id = record.get("id")
        if (
            not isinstance(class_id, str)
            or not class_id
            or not isinstance(record.get("label"), str)
            or not record["label"]
            or not isinstance(record.get("summary"), str)
            or not record["summary"]
        ):
            raise _refuse(f"{source.name}: a class record is incomplete")
        if class_id in classes:
            raise _refuse(f"{source.name}: class {class_id!r} declared twice")
        classes[class_id] = {
            "label": record["label"],
            "summary": record["summary"],
        }
    entries: dict[str, TaxonomyEntry] = {}
    raw_entries = raw.get("entries")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise _refuse(f"{source.name}: entries must be a non-empty list")
    for record in raw_entries:
        if not isinstance(record, dict) or set(record) - _ENTRY_FIELDS:
            raise _refuse(f"{source.name}: an entry record is malformed")
        name = record.get("name")
        if not isinstance(name, str) or not name:
            raise _refuse(f"{source.name}: an entry carries no name")
        if name in entries:
            raise _refuse(f"{source.name}: entry {name!r} is classified twice")
        class_id = record.get("class")
        if class_id not in classes:
            raise _refuse(
                f"{source.name}: entry {name!r} names undeclared "
                f"class {class_id!r}"
            )
        cause, action = record.get("cause"), record.get("action")
        if (
            not isinstance(cause, str)
            or not cause
            or not isinstance(action, str)
            or not action
        ):
            raise _refuse(
                f"{source.name}: entry {name!r} must carry its "
                f"likely cause and the operator's next action"
            )
        entries[name] = TaxonomyEntry(
            name=name, class_id=class_id, cause=cause, action=action
        )
    return FailureTaxonomy(
        version=file_version,
        as_of=str(raw.get("as_of") or ""),
        source=str(raw.get("source") or ""),
        classes=classes,
        entries=entries,
    )


def load_taxonomy(taxonomy_dir: Path) -> FailureTaxonomy:
    """The taxonomy in force: the highest recorded version in the
    directory, loaded fail-closed. Unlike the price table — where
    every version stays loadable because old runs were costed under
    it — classification is a read-time act over records, so only
    the current version is ever applied; publishing a new version
    is the deliberate act that moves it."""
    folder = Path(taxonomy_dir)
    found: dict[int, Path] = {}
    if folder.is_dir():
        for path in sorted(folder.glob("taxonomy-v*.json")):
            match = _TABLE_FILE.match(path.name)
            if match:
                found[int(match.group(1))] = path
    if not found:
        raise _refuse(
            f"no taxonomy (taxonomy-v<N>.json) found in {folder}; "
            f"failures would have nothing governed to classify "
            f"under — refusing to start"
        )
    version = max(found)
    path = found[version]
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise _refuse(f"{path.name} is not valid JSON: {exc}") from None
    return _parse_taxonomy(raw, path, version)


__all__ = [
    "FailureTaxonomy",
    "TaxonomyEntry",
    "UNCLASSIFIED",
    "load_taxonomy",
]
