"""Patches: how a run's work leaves the sandbox.

A run's patch is a unified diff of its workspace against the pristine
snapshot taken when the run started. Verification does not trust the
workspace: it applies the patch to a *fresh* copy of the target with
:func:`apply_patch`, which checks every hunk's context as it goes —
a patch that does not apply cleanly to the pristine tree is not a
patch, whatever the agent says happened in its sandbox.
"""

from __future__ import annotations

import difflib
from pathlib import Path

_EXCLUDED_DIRS = {"__pycache__", ".git", ".pytest_cache"}


def _tree_files(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if any(part in _EXCLUDED_DIRS for part in path.parts):
            continue
        if path.is_file():
            out[path.relative_to(root).as_posix()] = path.read_text()
    return out


def build_patch(pristine: Path, workspace: Path) -> tuple[str, list[str]]:
    """Diff two trees. Returns (unified diff text, changed relpaths)."""
    before = _tree_files(pristine)
    after = _tree_files(workspace)
    changed: list[str] = []
    chunks: list[str] = []
    for rel in sorted(set(before) | set(after)):
        old = before.get(rel)
        new = after.get(rel)
        if old == new:
            continue
        changed.append(rel)
        diff = difflib.unified_diff(
            (old or "").splitlines(keepends=True),
            (new or "").splitlines(keepends=True),
            fromfile=f"a/{rel}" if old is not None else "/dev/null",
            tofile=f"b/{rel}" if new is not None else "/dev/null",
        )
        chunks.append("".join(diff))
    return "".join(chunks), changed


class PatchError(Exception):
    """The patch does not apply cleanly to the pristine tree."""


def _parse_hunks(lines: list[str]):
    """Yield (old_start, [hunk lines]) per @@ header."""
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("@@ "):
            header = line.split("@@")[1].strip().split()
            old_start = int(header[0].split(",")[0].lstrip("-"))
            body: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].startswith("@@ "):
                body.append(lines[i])
                i += 1
            yield old_start, body
        else:
            i += 1


def _apply_to_text(original: str, section: list[str]) -> str:
    lines = original.splitlines(keepends=True)
    offset = 0
    for old_start, body in _parse_hunks(section):
        # A creation hunk starts at old line 0: insertions begin at 0.
        idx = (old_start - 1 if old_start > 0 else 0) + offset
        if idx < 0:
            raise PatchError("hunk starts before the file")
        for entry in body:
            if entry.startswith("\\"):
                continue  # "\ No newline at end of file"
            tag, content = entry[0], entry[1:]
            if tag == " ":
                if idx >= len(lines) or lines[idx] != content:
                    raise PatchError("context does not match the pristine file")
                idx += 1
            elif tag == "-":
                if idx >= len(lines) or lines[idx] != content:
                    raise PatchError("removed line does not match the pristine file")
                del lines[idx]
                offset -= 1
            elif tag == "+":
                lines.insert(idx, content)
                idx += 1
                offset += 1
            else:
                raise PatchError(f"unsupported diff line: {entry!r}")
    return "".join(lines)


def apply_patch(root: Path, patch_text: str) -> list[str]:
    """Apply a unified diff to the tree at ``root``, verifying context.

    Supports modifying existing files and creating new ones. Returns
    the list of files the patch touched. Raises PatchError on any
    mismatch — nothing is half-applied silently: application happens
    file by file, and the caller treats a raise as a failed run on a
    throwaway copy.
    """
    if not patch_text.strip():
        return []
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in patch_text.splitlines(keepends=True):
        if line.startswith("--- "):
            continue
        if line.startswith("+++ b/"):
            current = line[len("+++ b/") :].strip()
            sections[current] = []
            continue
        if line.startswith("+++ /dev/null"):
            raise PatchError("file deletions are not supported in round 1")
        if current is not None:
            sections[current].append(line)
    touched: list[str] = []
    for rel, section in sections.items():
        target = root / rel
        original = target.read_text() if target.is_file() else ""
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_apply_to_text(original, section))
        touched.append(rel)
    return touched
