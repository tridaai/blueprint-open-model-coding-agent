"""The tool port: the only hands the agent has, and they are gloved.

A :class:`SandboxTools` instance is rooted at one run workspace — an
isolated copy of the target repo. The shell wraps the model's steps
in these tools, and the tools enforce, in code:

- **Edits only inside the approved file scope.** An edit naming a file
  outside the plan's scope is refused and the refusal is returned as
  data (and recorded by the caller), never silently widened.
- **Edits are anchored replacements.** The ``old`` text must occur
  exactly once in the file; a missing or ambiguous anchor is data the
  model can react to, not a retry-in-the-dark.
- **Commands are an allowlist of one shape:** ``pytest`` over named,
  existing test files inside the workspace. There is no shell, no
  arbitrary command, and the child environment is scrubbed of every
  credential-looking variable before anything runs.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

SENSITIVE_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "AUTH", "CREDENTIAL")

_EXCLUDED_DIRS = {"__pycache__", ".git", ".pytest_cache"}


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


class ToolResult:
    def __init__(self, ok: bool, output: str, refused: bool = False) -> None:
        self.ok = ok
        self.output = output
        self.refused = refused

    def as_dict(self) -> dict:
        return {"ok": self.ok, "output": self.output[:2000], "refused": self.refused}


class SandboxTools:
    def __init__(self, root: Path, *, scope: list[str], test_timeout: int) -> None:
        self.root = Path(root)
        self.scope = list(scope)
        self.test_timeout = test_timeout

    # -- path discipline ----------------------------------------------
    def _resolve(self, rel: str) -> Path | None:
        """Resolve a workspace-relative path, refusing escapes."""
        candidate = (self.root / rel).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            return None
        return candidate

    def _rel(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    # -- read tools -----------------------------------------------------
    def list_files(self) -> ToolResult:
        files: list[str] = []
        for path in sorted(self.root.rglob("*")):
            if any(part in _EXCLUDED_DIRS for part in path.parts):
                continue
            if path.is_file():
                files.append(self._rel(path))
        return ToolResult(True, "\n".join(files))

    def read_file(self, path: str) -> ToolResult:
        target = self._resolve(path)
        if target is None or not target.is_file():
            return ToolResult(False, f"no such file in the workspace: {path}")
        return ToolResult(True, target.read_text())

    def search(self, pattern: str) -> ToolResult:
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            return ToolResult(False, f"invalid search pattern: {exc}")
        hits: list[str] = []
        for path in sorted(self.root.rglob("*.py")):
            if any(part in _EXCLUDED_DIRS for part in path.parts):
                continue
            for lineno, line in enumerate(path.read_text().splitlines(), 1):
                if regex.search(line):
                    hits.append(f"{self._rel(path)}:{lineno}: {line.strip()}")
                    if len(hits) >= 100:
                        return ToolResult(True, "\n".join(hits))
        return ToolResult(True, "\n".join(hits) or "(no matches)")

    # -- the edit tool ----------------------------------------------------
    def edit_file(self, path: str, old: str, new: str) -> ToolResult:
        """Anchored replacement — the only way the agent changes code.

        Refused, as data, when the file is outside the approved scope,
        when the anchor is absent, or when it is ambiguous.
        """
        if path not in self.scope:
            return ToolResult(
                False,
                f"edit refused: {path} is outside the approved file scope "
                f"{self.scope}",
                refused=True,
            )
        target = self._resolve(path)
        if target is None or not target.is_file():
            return ToolResult(False, f"no such file in the workspace: {path}")
        text = target.read_text()
        occurrences = text.count(old)
        if occurrences == 0:
            return ToolResult(
                False, f"edit refused: anchor not found in {path}", refused=True
            )
        if occurrences > 1:
            return ToolResult(
                False,
                f"edit refused: anchor occurs {occurrences} times in {path}; "
                f"an anchor must be unique",
                refused=True,
            )
        target.write_text(text.replace(old, new))
        return ToolResult(True, f"edited {path}")

    # -- the one allowed command shape ------------------------------------
    def run_tests(self, paths: list[str]) -> ToolResult:
        """Run pytest over named test files — the only command there is.

        Anything that is not an existing test file inside the
        workspace is refused. There is deliberately no way to name a
        different command.
        """
        if not paths:
            return ToolResult(False, "run_tests needs at least one path", refused=True)
        for rel in paths:
            target = self._resolve(rel)
            if target is None or not target.is_file() or not _is_test_path(rel):
                return ToolResult(
                    False,
                    f"run refused: {rel} is not a test file in the workspace",
                    refused=True,
                )
        cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *paths]
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
            return ToolResult(
                False, f"test run exceeded {self.test_timeout}s and was stopped"
            )
        output = (proc.stdout + proc.stderr).strip()
        return ToolResult(proc.returncode == 0, output or "(no output)")


def dispatch(tools: SandboxTools, step) -> ToolResult:
    """Run one model step through the tool port."""
    args = step.args or {}
    if step.tool == "list_files":
        return tools.list_files()
    if step.tool == "read_file":
        return tools.read_file(str(args.get("path", "")))
    if step.tool == "search":
        return tools.search(str(args.get("pattern", "")))
    if step.tool == "edit_file":
        return tools.edit_file(
            str(args.get("path", "")), str(args.get("old", "")), str(args.get("new", ""))
        )
    if step.tool == "run_tests":
        return tools.run_tests(list(args.get("paths", [])))
    return ToolResult(False, f"unknown tool {step.tool!r}", refused=True)
