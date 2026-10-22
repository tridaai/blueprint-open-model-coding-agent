"""The tool port: the only hands the agent has, and they are gloved.

A :class:`SandboxTools` instance is rooted at one run workspace — an
isolated copy of the target repo. The shell wraps the model's steps
in these tools, and the tools enforce, in code:

- **Edits only inside the approved file scope.** An edit naming a file
  outside the plan's scope is refused and the refusal is returned as
  data (and recorded by the caller), never silently widened.
- **Edits are anchored replacements — the only way code changes.**
  There is no whole-file write and no append tool. The anchor text
  must occur exactly once in the file; a missing anchor refuses as
  ``anchor_not_found`` and an ambiguous one as ``anchor_ambiguous``,
  both returned as data the model can act on (re-read, narrow the
  anchor), never a silent best-effort edit. A successful edit records
  the anchor and the file's before/after digests.
- **Reads are bounded.** ``read_file`` takes an optional line range;
  a file (or range) larger than the read ceiling comes back as its
  head plus a truncation marker naming the file's true size, and a
  binary or undecodable file refuses by name instead of returning
  garbage.
- **Search is precise and honest.** Matches carry file, line, and a
  bounded context window; case sensitivity and glob/path scoping are
  explicit parameters that cannot reach outside the workspace; and
  when matches exceed the result limit the output *says so* — a
  truncated result is labelled, never silently cut.
- **Affected tests are computed by the shell, in code.** Given the
  plan's steps and the files a run has actually edited, the affected
  set is a deterministic rule (below), not a model choice. A run may
  ask to run that set, or a named subset of it. Verification still
  runs the full grading set on a pristine copy — the affected set is
  the run's quick feedback, never the verdict.
- **Commands are an allowlist of one shape:** ``pytest`` over named,
  existing test files inside the workspace. There is no shell, no
  arbitrary command, and the child environment is scrubbed of every
  credential-looking variable before anything runs.
- **Symlinks are never followed.** A path named directly resolves
  first, and a symlink pointing outside the workspace refuses by
  name; listings and searches skip symlink entries entirely, so a
  link planted in the target repo cannot smuggle outside content
  into a result the model reads.
- **Grading runs are isolated runs.** When the shell grades (its own
  verification, never the model's in-run checks), pytest runs with
  ``--noconftest`` and the repo's own ``addopts`` overridden away:
  files the target repo carries cannot weaken collection, skip the
  grading tests, or patch the code under test before the grading
  tests import it. The run's evidence is the JUnit report pytest
  writes — the grading counts (tests, failures, errors, skipped)
  are read from it, and a grading run counts as passed only when
  every grading test actually ran and passed: an exit code of zero
  over tests that were skipped or deselected proves nothing.

Every result is structured: ``ok``, the human-readable ``output``, a
``refused`` flag, a stable ``code`` naming a refusal, and a
``detail`` dict (digests, counts, truncation state). The service
records each result verbatim on the run's audit trail — a refusal or
a truncation is a stored record, not a log line.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

SENSITIVE_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "AUTH", "CREDENTIAL")

_EXCLUDED_DIRS = {"__pycache__", ".git", ".pytest_cache"}

# Read discipline: a single read returns at most this many bytes of
# file content; anything beyond comes back with a truncation marker.
MAX_READ_BYTES = 32768
# Search discipline: at most this many matches per search (the
# result says how many there were in total), with a small context
# window around each match.
SEARCH_DEFAULT_LIMIT = 50
SEARCH_MAX_LIMIT = 200
SEARCH_DEFAULT_CONTEXT = 1
SEARCH_MAX_CONTEXT = 5


def scrubbed_env(root: Path) -> dict[str, str]:
    """The environment a test run is allowed to see: the basics, the
    workspace on PYTHONPATH, and nothing credential-shaped."""
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        if any(marker in key.upper() for marker in SENSITIVE_MARKERS):
            continue
        env[key] = value
    env["PYTHONPATH"] = str(root)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _is_test_path(rel: str) -> bool:
    name = Path(rel).name
    return name.startswith("test_") and name.endswith(".py") or name.endswith(
        "_test.py"
    )


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ToolResult:
    """One tool call's outcome, structured so the shell can record
    it verbatim and the model can act on it: a refusal or truncation
    is data with a stable name, never a bare string in a log."""

    def __init__(
        self,
        ok: bool,
        output: str,
        refused: bool = False,
        code: str | None = None,
        detail: dict | None = None,
    ) -> None:
        self.ok = ok
        self.output = output
        self.refused = refused
        self.code = code
        self.detail = detail or {}

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "output": self.output[:2000],
            "refused": self.refused,
            "code": self.code,
            "detail": self.detail,
        }


class SandboxTools:
    def __init__(
        self,
        root: Path,
        *,
        scope: list[str],
        test_timeout: int,
        plan_steps: list[dict] | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.scope = list(scope)
        self.test_timeout = test_timeout
        # The approved plan's steps (intent/files/tests per step), the
        # input to affected-test selection alongside the edits below.
        self.plan_steps = list(plan_steps or [])
        # Files this instance has actually edited, in edit order —
        # the other input to affected-test selection.
        self.edited_files: list[str] = []

    # -- path discipline ----------------------------------------------
    def _resolve(self, rel: str) -> Path | None:
        """Resolve a workspace-relative path, refusing escapes and
        case-mismatched spellings."""
        candidate = (self.root / rel).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            return None
        # On a case-insensitive filesystem (macOS, Windows) "PKG/CALC.PY"
        # opens pkg/calc.py; the workspace is case-exact, so an existing
        # component spelled differently on disk is refused like an escape.
        current = self.root
        for part in candidate.relative_to(self.root).parts:
            try:
                entries = os.listdir(current)
            except OSError:
                break
            if part not in entries:
                if part.casefold() in {e.casefold() for e in entries}:
                    return None
                break
            current = current / part
        return candidate

    def _rel(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def _workspace_files(self) -> list[Path]:
        # Symlink entries are excluded wholesale: a link planted in
        # the target repo pointing outside the workspace must not
        # feed its target's content into a listing, a search, or the
        # affected-test scan. Named access to a symlink still goes
        # through _resolve, which refuses the escape by name.
        return [
            path
            for path in sorted(self.root.rglob("*"))
            if not any(part in _EXCLUDED_DIRS for part in path.parts)
            and path.is_file()
            and not path.is_symlink()
        ]

    def _read_text(self, target: Path) -> str | None:
        """Decode a workspace file as UTF-8 text, or None when it is
        not text (binary or undecodable) — callers refuse by name
        rather than hand the model garbage."""
        try:
            text = target.read_bytes().decode("utf-8")
        except (UnicodeDecodeError, OSError):
            return None
        if "\x00" in text:
            return None
        return text

    # -- read tools -----------------------------------------------------
    def list_files(self) -> ToolResult:
        files = [self._rel(path) for path in self._workspace_files()]
        return ToolResult(True, "\n".join(files))

    def read_file(
        self,
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> ToolResult:
        """Read one file, optionally a 1-based inclusive line range.

        The returned content is the file's exact text (or the exact
        lines of the range) unless it exceeds the read ceiling, in
        which case the head comes back with a truncation marker
        naming the file's total size. Invalid ranges and binary
        files refuse by name.
        """
        target = self._resolve(path)
        if target is None or not target.is_file():
            return ToolResult(
                False,
                f"no such file in the workspace: {path}",
                refused=True,
                code="file_not_found",
                detail={"path": path},
            )
        text = self._read_text(target)
        if text is None:
            return ToolResult(
                False,
                f"read refused: {path} is not a UTF-8 text file",
                refused=True,
                code="binary_file",
                detail={"path": path},
            )
        lines = text.splitlines()
        total_lines = len(lines)
        total_bytes = len(text.encode("utf-8"))
        detail = {
            "path": path,
            "total_lines": total_lines,
            "total_bytes": total_bytes,
        }
        if total_lines == 0:
            return ToolResult(True, "", detail={**detail, "truncated": False})
        start = 1 if start_line is None else start_line
        end = total_lines if end_line is None else end_line
        if start < 1:
            return ToolResult(
                False,
                f"read refused: line range {start}-{end} is not a valid "
                f"1-based range",
                refused=True,
                code="invalid_line_range",
                detail=detail,
            )
        if start > total_lines:
            return ToolResult(
                False,
                f"read refused: {path} has {total_lines} lines; "
                f"start_line {start} is past the end",
                refused=True,
                code="line_range_out_of_bounds",
                detail=detail,
            )
        if end < start:
            return ToolResult(
                False,
                f"read refused: line range {start}-{end} runs backwards",
                refused=True,
                code="invalid_line_range",
                detail=detail,
            )
        end = min(end, total_lines)
        if start_line is None and end_line is None:
            body = text
        else:
            body = "\n".join(lines[start - 1 : end])
            if text.endswith("\n"):
                body += "\n"
        truncated = False
        if len(body.encode("utf-8")) > MAX_READ_BYTES:
            cut = body.encode("utf-8")[:MAX_READ_BYTES].decode(
                "utf-8", errors="ignore"
            )
            if "\n" in cut:
                cut = cut[: cut.rfind("\n") + 1]
            marker = (
                f"[read truncated: {path} is {total_bytes} bytes across "
                f"{total_lines} lines; showing the first "
                f"{len(cut.encode('utf-8'))} bytes — read a narrower line "
                f"range for the rest]"
            )
            body = cut + ("" if cut.endswith("\n") else "\n") + marker
            truncated = True
        return ToolResult(
            True,
            body,
            detail={
                **detail,
                "start_line": start,
                "end_line": end,
                "truncated": truncated,
            },
        )

    def search(
        self,
        pattern: str,
        *,
        case_sensitive: bool = True,
        glob: str = "*.py",
        path: str | None = None,
        limit: int = SEARCH_DEFAULT_LIMIT,
        context: int = SEARCH_DEFAULT_CONTEXT,
    ) -> ToolResult:
        """Search workspace files for a regex pattern.

        Matches come back with file, line, and a bounded context
        window. ``glob`` is matched against workspace-relative paths
        and ``path`` narrows the search to one file or directory;
        neither can reach outside the workspace — an attempt refuses
        by name. When the matches exceed ``limit``, the output ends
        with a truncation marker naming the true total: the cut is
        always labelled, never silent.
        """
        base_detail = {"pattern": pattern, "glob": glob, "path": path}
        if ".." in Path(glob).parts or glob.startswith("/"):
            return ToolResult(
                False,
                f"search refused: glob {glob!r} reaches outside the workspace",
                refused=True,
                code="search_glob_invalid",
                detail=base_detail,
            )
        candidates = self._workspace_files()
        if path is not None:
            target = self._resolve(path)
            if target is None:
                return ToolResult(
                    False,
                    f"search refused: {path} is outside the workspace",
                    refused=True,
                    code="search_outside_workspace",
                    detail=base_detail,
                )
            if target.is_file():
                candidates = [target]
            elif target.is_dir():
                prefix = target
                candidates = [p for p in candidates if prefix in p.parents]
            else:
                return ToolResult(
                    False,
                    f"search refused: no such file or directory: {path}",
                    refused=True,
                    code="file_not_found",
                    detail=base_detail,
                )
        candidates = [
            p for p in candidates if fnmatch.fnmatch(self._rel(p), glob)
        ]
        try:
            regex = re.compile(pattern, 0 if case_sensitive else re.IGNORECASE)
        except re.error as exc:
            return ToolResult(
                False,
                f"invalid search pattern: {exc}",
                code="invalid_pattern",
                detail=base_detail,
            )
        limit = max(1, min(int(limit), SEARCH_MAX_LIMIT))
        context = max(0, min(int(context), SEARCH_MAX_CONTEXT))
        matches: list[tuple[str, list[str], int]] = []  # (rel, lines, lineno)
        for candidate in candidates:
            text = self._read_text(candidate)
            if text is None:
                continue
            lines = text.splitlines()
            for lineno, line in enumerate(lines, 1):
                if regex.search(line):
                    matches.append((self._rel(candidate), lines, lineno))
        total = len(matches)
        shown = matches[:limit]
        out: list[str] = []
        emitted: set[tuple[str, int]] = set()
        for rel, lines, lineno in shown:
            for n in range(
                max(1, lineno - context), min(len(lines), lineno + context) + 1
            ):
                if (rel, n) in emitted:
                    continue
                emitted.add((rel, n))
                mark = ":" if n == lineno else "-"
                out.append(f"{rel}:{n}{mark} {lines[n - 1]}")
        truncated = total > len(shown)
        if truncated:
            out.append(
                f"[search truncated: showing {len(shown)} of {total} "
                f"matches — narrow the pattern, the glob, or the path "
                f"to see the rest]"
            )
        return ToolResult(
            True,
            "\n".join(out) or "(no matches)",
            detail={
                **base_detail,
                "total_matches": total,
                "shown_matches": len(shown),
                "truncated": truncated,
            },
        )

    # -- the edit tool ----------------------------------------------------
    def edit_file(self, path: str, old: str, new: str) -> ToolResult:
        """Anchored replacement — the only way the agent changes code.

        Refused, as data with a stable code, when the file is outside
        the approved scope (``edit_outside_scope``), when the anchor
        is absent (``anchor_not_found``), or when it occurs more than
        once (``anchor_ambiguous``). A successful edit records the
        anchor and the file's before/after digests, so the run's
        record shows exactly what the edit hung on and what it did
        to the file.
        """
        if path not in self.scope:
            return ToolResult(
                False,
                f"edit refused: {path} is outside the approved file scope "
                f"{self.scope}",
                refused=True,
                code="edit_outside_scope",
                detail={"path": path, "scope": list(self.scope)},
            )
        target = self._resolve(path)
        if target is None or not target.is_file():
            return ToolResult(
                False,
                f"no such file in the workspace: {path}",
                refused=True,
                code="file_not_found",
                detail={"path": path},
            )
        text = self._read_text(target)
        if text is None:
            return ToolResult(
                False,
                f"edit refused: {path} is not a UTF-8 text file",
                refused=True,
                code="binary_file",
                detail={"path": path},
            )
        if not old:
            # An empty anchor "occurs" between every character; name
            # what is wrong instead of reporting thousands of matches.
            return ToolResult(
                False,
                f"edit refused: the anchor `old` is empty — read {path} "
                f"and pass the exact text to replace, unique in the file",
                refused=True,
                code="anchor_not_found",
                detail={"path": path, "anchor_sha256": _sha256(old)},
            )
        occurrences = text.count(old)
        if occurrences == 0:
            return ToolResult(
                False,
                f"edit refused: anchor not found in {path}",
                refused=True,
                code="anchor_not_found",
                detail={"path": path, "anchor_sha256": _sha256(old)},
            )
        if occurrences > 1:
            return ToolResult(
                False,
                f"edit refused: anchor occurs {occurrences} times in {path}; "
                f"an anchor must be unique",
                refused=True,
                code="anchor_ambiguous",
                detail={
                    "path": path,
                    "anchor_sha256": _sha256(old),
                    "occurrences": occurrences,
                },
            )
        before = _sha256(text)
        replaced = text.replace(old, new)
        target.write_text(replaced)
        if path not in self.edited_files:
            self.edited_files.append(path)
        return ToolResult(
            True,
            f"edited {path}",
            detail={
                "path": path,
                "anchor": old[:500],
                "anchor_sha256": _sha256(old),
                "before_sha256": before,
                "after_sha256": _sha256(replaced),
            },
        )

    # -- affected-test selection -------------------------------------------
    def affected_tests(self, edited: list[str] | None = None) -> list[str]:
        """The tests a run's edits affect, computed in code.

        Two rules, both deterministic, no model involvement:

        **Plan rule** — a plan step that names an edited file
        contributes the tests that step names.
        **Import rule** — a test file in the workspace whose imports
        reach an edited module is affected. An edited file
        ``workshop/pricing.py`` is the module ``workshop.pricing``
        (stem ``pricing``); a test is reached when it imports that
        dotted name or stem, or imports the stem from the file's
        package (``from workshop import pricing``).

        Returns the sorted union. The set may name tests that are
        not in the workspace — a gold task's grading tests, staged
        only into the verification copy. Those are verification's
        business; :meth:`run_tests` notes them as not-in-workspace
        rather than pretending they ran.
        """
        edited = list(self.edited_files if edited is None else edited)
        if not edited:
            return []
        edited_set = set(edited)
        affected: set[str] = set()
        for step in self.plan_steps:
            if edited_set & set(step.get("files") or []):
                affected.update(step.get("tests") or [])
        modules: list[tuple[str, str, str]] = []  # (dotted, stem, package)
        for rel in edited:
            if rel.endswith(".py"):
                dotted = rel[:-3].replace("/", ".")
                stem = Path(rel).stem
                package = dotted.rpartition(".")[0]
                modules.append((dotted, stem, package))
        if modules:
            for candidate in self._workspace_files():
                rel = self._rel(candidate)
                if not _is_test_path(rel):
                    continue
                text = self._read_text(candidate)
                if text is None:
                    continue
                if _imports_any(text, modules):
                    affected.add(rel)
        return sorted(affected)

    # -- the one allowed command shape ------------------------------------
    def run_tests(
        self, paths: list[str], affected_only: bool = False, *, grading: bool = False
    ) -> ToolResult:
        """Run pytest over named test files — the only command there is.

        Anything that is not an existing test file inside the
        workspace is refused. There is deliberately no way to name a
        different command.

        With ``affected_only``, the paths name a *subset* of the
        affected set this run's edits produced (empty paths = the
        whole set): the shell intersects in code, so the model can
        ask for the affected run but cannot redefine what affected
        means. Tests the set names that are not in the workspace
        (the grading set) are reported as not-in-workspace in the
        result's detail — verification runs those itself, on the
        pristine copy.

        With ``grading`` (the shell's own verification only), the run
        is isolated from the repo it grades — no conftest, no repo
        ``addopts`` — and its result carries the grading evidence
        read from pytest's JUnit report; see :meth:`_run_pytest`.
        """
        if affected_only:
            affected = self.affected_tests()
            if paths:
                wanted = set(paths) | {Path(p).name for p in paths}
                selected = [
                    p for p in affected if p in wanted or Path(p).name in wanted
                ]
            else:
                selected = list(affected)
            runnable = [
                p
                for p in selected
                if (t := self._resolve(p)) is not None
                and t.is_file()
                and _is_test_path(p)
            ]
            skipped = [p for p in selected if p not in runnable]
            detail = {
                "affected": affected,
                "selected": runnable,
                "not_in_workspace": skipped,
            }
            if not runnable:
                return ToolResult(
                    False,
                    "run refused: no affected tests are runnable in the "
                    f"workspace (affected set: {affected or '[]'}; edited "
                    f"files: {self.edited_files or '[]'})",
                    refused=True,
                    code="no_affected_tests",
                    detail=detail,
                )
            result = self._run_pytest(runnable, grading=grading)
            result.detail = {**result.detail, **detail}
            return result
        if not paths:
            return ToolResult(
                False,
                "run_tests needs at least one path",
                refused=True,
                code="no_test_paths",
            )
        for rel in paths:
            target = self._resolve(rel)
            if target is None or not target.is_file() or not _is_test_path(rel):
                return ToolResult(
                    False,
                    f"run refused: {rel} is not a test file in the workspace",
                    refused=True,
                    code="not_a_test_file",
                    detail={"path": rel},
                )
        return self._run_pytest(paths, grading=grading)

    def _run_pytest(self, paths: list[str], *, grading: bool = False) -> ToolResult:
        cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
        report_path: Path | None = None
        if grading:
            # Isolation from the tree being graded: --noconftest keeps
            # repo-carried conftest hooks (skip-marking, collection
            # edits, monkeypatching the code under test) out of the
            # verdict, and -o addopts= overrides whatever the repo's
            # own pytest configuration tried to add to the command.
            # The JUnit report is the evidence the verdict reads.
            fd, raw = tempfile.mkstemp(prefix="grading-", suffix=".xml")
            os.close(fd)
            report_path = Path(raw)
            cmd += [
                "--noconftest",
                "-o",
                "addopts=",
                "--junitxml",
                str(report_path),
            ]
        cmd += list(paths)
        try:
            proc = subprocess.run(
                cmd,
                cwd=self.root,
                env=scrubbed_env(self.root),
                capture_output=True,
                text=True,
                timeout=self.test_timeout,
            )
        except subprocess.TimeoutExpired:
            if report_path is not None:
                report_path.unlink(missing_ok=True)
            return ToolResult(
                False,
                f"test run exceeded {self.test_timeout}s and was stopped",
                code="test_timeout",
                detail={"paths": list(paths)},
            )
        output = (proc.stdout + proc.stderr).strip()
        if not grading:
            return ToolResult(
                proc.returncode == 0,
                output or "(no output)",
                code=None if proc.returncode == 0 else "tests_failed",
                detail={"paths": list(paths)},
            )
        evidence = _grading_evidence(report_path) if report_path else None
        if report_path is not None:
            report_path.unlink(missing_ok=True)
        if proc.returncode != 0:
            return ToolResult(
                False,
                output or "(no output)",
                code="tests_failed",
                detail={"paths": list(paths), "grading": evidence},
            )
        if evidence is None:
            return ToolResult(
                False,
                (output or "(no output)")
                + "\ngrading evidence missing: pytest wrote no readable "
                "JUnit report, so the run proves nothing",
                code="grading_evidence_missing",
                detail={"paths": list(paths), "grading": None},
            )
        fully_run = (
            evidence["tests"] > 0
            and evidence["failures"] == 0
            and evidence["errors"] == 0
            and evidence["skipped"] == 0
        )
        if not fully_run:
            return ToolResult(
                False,
                (output or "(no output)")
                + f"\ngrading incomplete: of {evidence['tests']} grading "
                f"tests, {evidence['skipped']} were skipped and "
                f"{evidence['failures'] + evidence['errors']} did not pass "
                f"— a verdict requires every grading test to run and pass",
                code="grading_not_fully_run",
                detail={"paths": list(paths), "grading": evidence},
            )
        return ToolResult(
            True,
            output or "(no output)",
            detail={"paths": list(paths), "grading": evidence},
        )


def _grading_evidence(report_path: Path) -> dict | None:
    """The grading counts from a pytest JUnit report — how many
    tests were collected and run, and how many failed, errored, or
    were skipped. None when the report is missing or unreadable: a
    grading run without evidence is not a pass."""
    try:
        root = ET.parse(report_path).getroot()
    except (ET.ParseError, OSError):
        return None
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    if not suites:
        return None
    evidence = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    for suite in suites:
        for name in evidence:
            try:
                evidence[name] += int(suite.get(name) or 0)
            except ValueError:
                return None
    return evidence


_IMPORT_RE = re.compile(r"^\s*import\s+([A-Za-z_][\w.]*)")
_FROM_RE = re.compile(r"^\s*from\s+([A-Za-z_][\w.]*)\s+import\s+(.+)$")


def _imports_any(text: str, modules: list[tuple[str, str, str]]) -> bool:
    """Does this source import any of the (dotted, stem, package)
    modules — directly, or the stem from its package?"""
    dotted_names = {d for d, _s, _p in modules}
    stems = {s for _d, s, _p in modules}
    packages = {(p, s) for _d, s, p in modules if p}
    for line in text.splitlines():
        plain = _IMPORT_RE.match(line)
        if plain:
            name = plain.group(1)
            if name in dotted_names or name.split(".")[0] in stems:
                return True
            continue
        from_match = _FROM_RE.match(line)
        if from_match:
            base, names = from_match.group(1), from_match.group(2)
            if base in dotted_names or base in stems:
                return True
            imported = {
                part.strip().split(" as ")[0].strip("()")
                for part in names.split(",")
            }
            if any((base, name) in packages for name in imported):
                return True
    return False


def _opt_int(args: dict, name: str) -> int | None:
    value = args.get(name)
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _opt_bool(args: dict, name: str, default: bool) -> bool:
    value = args.get(name)
    return value if isinstance(value, bool) else default


def _paths_arg(args: dict) -> list[str]:
    """``paths`` as a list of strings. A bare string is one path —
    never split into its characters."""
    value = args.get("paths")
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return [str(p) for p in value]
    return []


def _missing_args(
    result: ToolResult, args: dict, required: tuple[str, ...]
) -> ToolResult:
    """Name the expected arguments on a refusal when the call left a
    required one out, so a model that used other names can correct
    itself instead of repeating the call."""
    missing = [name for name in required if name not in args]
    if result.refused and missing:
        result.output += (
            f" (missing argument(s) {missing}; this tool takes "
            f"{list(required)}, received {sorted(args)})"
        )
    return result


def dispatch(tools: SandboxTools, step) -> ToolResult:
    """Run one model step through the tool port."""
    args = step.args or {}
    if step.tool == "list_files":
        return tools.list_files()
    if step.tool == "read_file":
        return tools.read_file(
            str(args.get("path", "")),
            start_line=_opt_int(args, "start_line"),
            end_line=_opt_int(args, "end_line"),
        )
    if step.tool == "search":
        limit = _opt_int(args, "limit")
        context = _opt_int(args, "context")
        raw_path = args.get("path")
        return tools.search(
            str(args.get("pattern", "")),
            case_sensitive=_opt_bool(args, "case_sensitive", True),
            glob=str(args.get("glob") or "*.py"),
            path=str(raw_path) if raw_path else None,
            limit=SEARCH_DEFAULT_LIMIT if limit is None else limit,
            context=SEARCH_DEFAULT_CONTEXT if context is None else context,
        )
    if step.tool == "edit_file":
        result = tools.edit_file(
            str(args.get("path", "")), str(args.get("old", "")), str(args.get("new", ""))
        )
        return _missing_args(result, args, ("path", "old", "new"))
    if step.tool == "run_tests":
        result = tools.run_tests(
            _paths_arg(args),
            affected_only=_opt_bool(args, "only_affected", False),
        )
        return _missing_args(result, args, ("paths",))
    return ToolResult(
        False, f"unknown tool {step.tool!r}", refused=True, code="unknown_tool"
    )
