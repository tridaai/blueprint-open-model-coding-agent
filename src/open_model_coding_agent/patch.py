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
import re
from pathlib import Path

_EXCLUDED_DIRS = {"__pycache__", ".git", ".pytest_cache"}


def _tree_files(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if any(part in _EXCLUDED_DIRS for part in path.parts):
            continue
        # A symlink is not patch content: following it would read
        # whatever it points at — possibly outside the tree — into
        # the diff. Links are skipped, never dereferenced.
        if path.is_file() and not path.is_symlink():
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


def _patch_sections(patch_text: str) -> dict[str, list[str]]:
    """Split a unified diff into per-file sections, keyed by the
    ``+++ b/`` target path — the same split :func:`apply_patch`
    makes."""
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
            continue
        if current is not None:
            sections[current].append(line)
    return sections


def _is_binary_bytes(data: bytes) -> bool:
    return b"\x00" in data or _try_decode(data) is None


def _try_decode(data: bytes) -> str | None:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def patch_shape(
    pristine: Path, patch_text: str, changed: list[str]
) -> dict:
    """The patch's shape facts (round 10): what verification and
    the review gate need to know about the patch as a change, apart
    from whether its tests pass.

    Per changed file: lines added and removed (counted from the
    diff itself), and whether the file is binary — judged from the
    pristine bytes and, where the patch applies cleanly in memory,
    the patched bytes. Overall: whether the patch is ``empty``
    (no changed files, no diff text) and whether it is
    ``whitespace_only`` — every changed file's patched text equals
    its pristine text once all whitespace is removed, so the
    change cannot alter any behaviour. A file the shape cannot
    reconstruct (a patch that does not apply) is never counted as
    whitespace-only: an unreadable patch proves nothing.
    """
    sections = _patch_sections(patch_text)
    per_file: list[dict] = []
    total_added = 0
    total_removed = 0
    binary_files: list[str] = []
    whitespace_only = bool(changed)
    for rel in changed:
        section = sections.get(rel, [])
        added = sum(
            1
            for line in section
            if line.startswith("+") and not line.startswith("+++")
        )
        removed = sum(
            1
            for line in section
            if line.startswith("-") and not line.startswith("---")
        )
        total_added += added
        total_removed += removed
        pristine_path = pristine / rel
        old_bytes = pristine_path.read_bytes() if pristine_path.is_file() else b""
        binary = _is_binary_bytes(old_bytes)
        old_text = _try_decode(old_bytes)
        new_text: str | None = None
        if old_text is not None and section:
            try:
                new_text = _apply_to_text(old_text, section)
            except PatchError:
                new_text = None
        elif old_text is not None:
            new_text = old_text
        if new_text is not None and _is_binary_bytes(new_text.encode("utf-8")):
            binary = True
        if binary:
            binary_files.append(rel)
        if (
            new_text is None
            or old_text is None
            or re.sub(r"\s+", "", old_text) != re.sub(r"\s+", "", new_text)
        ):
            whitespace_only = False
        per_file.append(
            {
                "file": rel,
                "lines_added": added,
                "lines_removed": removed,
                "binary": binary,
            }
        )
    return {
        "files_changed": len(changed),
        "per_file": per_file,
        "lines_added": total_added,
        "lines_removed": total_removed,
        "empty": not changed and not patch_text.strip(),
        "whitespace_only": whitespace_only if changed else False,
        "binary_files": binary_files,
        "any_binary": bool(binary_files),
    }


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
