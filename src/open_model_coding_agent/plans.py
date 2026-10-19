"""Plan arithmetic: scope fingerprints, drift, and plan diffs.

Three computations the plan gate leans on, kept pure so they can be
reasoned about (and tested) without the service:

- **Digests and the fingerprint.** At approval, the shell records a
  SHA-256 digest of every file in the plan's approved scope, plus
  one fingerprint hash over the task text, the scope list, and those
  digests. The fingerprint is what approval *means*: this plan, for
  this task, against the repo exactly as it stood.
- **Drift.** At execution, the digests are recomputed. A file whose
  digest moved — edited, or deleted (digest ``None``) — is drift.
  Files outside the approved scope are never digested, so they can
  never stale a plan: the scope is what was approved.
- **The amendment diff.** Two plan versions, compared step by step:
  steps added, steps removed, steps changed, and how the derived
  file scope moved. Steps are matched as whole records, so a step
  that changed in any field reads as changed, never as a coincidence.
"""

from __future__ import annotations

import hashlib
import json
from difflib import SequenceMatcher
from pathlib import Path


def file_digests(target: Path, files: list[str]) -> dict[str, str | None]:
    """SHA-256 of each named file in the target repo. A file that is
    not there digests to None — a deletion is a fact the fingerprint
    must see, not an error."""
    digests: dict[str, str | None] = {}
    for rel in files:
        path = target / rel
        digests[rel] = (
            hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        )
    return digests


def scope_fingerprint(
    *, task_text: str, files: list[str], digests: dict[str, str | None]
) -> str:
    """One hash over the approved scope: the task text, the scope
    list, and the content digest of every file in it."""
    canonical = json.dumps(
        {
            "task_text": task_text,
            "scope": sorted(files),
            "digests": {rel: digests.get(rel) for rel in sorted(files)},
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def drifted_files(
    stored: dict[str, str | None], current: dict[str, str | None]
) -> list[str]:
    """The files whose digest differs between two digest maps."""
    return sorted(
        rel
        for rel in set(stored) | set(current)
        if stored.get(rel) != current.get(rel)
    )


def _step_key(step: dict) -> str:
    return json.dumps(step, sort_keys=True, separators=(",", ":"))


def diff_plans(old: dict, new: dict) -> dict:
    """The diff between two plan versions: steps added, removed, and
    changed (paired inside a replaced block; any surplus on either
    side reads as added or removed), plus the scope movement between
    the versions' derived file lists."""
    old_steps = list(old.get("steps") or [])
    new_steps = list(new.get("steps") or [])
    matcher = SequenceMatcher(
        a=[_step_key(step) for step in old_steps],
        b=[_step_key(step) for step in new_steps],
        autojunk=False,
    )
    added: list[dict] = []
    removed: list[dict] = []
    changed: list[dict] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag == "replace":
            pairs = list(zip(old_steps[i1:i2], new_steps[j1:j2]))
            changed.extend(
                {"before": before, "after": after} for before, after in pairs
            )
            removed.extend(old_steps[i1 + len(pairs) : i2])
            added.extend(new_steps[j1 + len(pairs) : j2])
        elif tag == "delete":
            removed.extend(old_steps[i1:i2])
        elif tag == "insert":
            added.extend(new_steps[j1:j2])
    old_files = set(old.get("files") or [])
    new_files = set(new.get("files") or [])
    return {
        "from_version": old.get("version"),
        "to_version": new.get("version"),
        "steps_added": added,
        "steps_removed": removed,
        "steps_changed": changed,
        "files_added": sorted(new_files - old_files),
        "files_removed": sorted(old_files - new_files),
    }
