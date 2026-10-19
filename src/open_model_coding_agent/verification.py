"""Verification depth (round 10): the checks beyond "the tests pass".

Round 9 proved the grading tests genuinely *run*. This module is
the pure arithmetic of the next question — whether a patch that
passes tests which genuinely run is an honest patch:

- **Test-surface detection.** A patch that touches the test or
  grading surface is gaming the verdict, not fixing the code:
  test files by the repo's layout rules (a ``test_*.py`` /
  ``*_test.py`` basename, or any file under a ``tests`` / ``test``
  / ``grading`` / ``hidden_grading`` directory), ``conftest.py``
  and interpreter startup files wherever they sit, and the pytest
  configuration files that can reshape collection. The grading
  staging paths themselves are surface by definition.
- **Scope-creep detection.** The patch is compared against the
  *approved plan's steps*, not only against the plan's derived
  file scope: a changed file no plan step named is an
  ``unplanned file``. Plan validation derives a plan's file scope
  from its steps, so on any plan the validator accepted the two
  sets coincide and the check cannot bite — it is defence in
  depth in the round-7/9 mould, computed and stored on every
  verification record whether or not it can fire, and it fires
  on any plan record whose steps and scope disagree.
- **Patch shape.** The patch's facts — files changed, lines
  added and removed per file, whether any changed file is
  binary, and whether the whole change is whitespace-only — are
  computed in :mod:`.patch` and stored on the record.

Policy lives in the service, not here: this module names what a
patch touched; :meth:`CodingService._verify` decides what that
means (for gold tasks, test-surface and unplanned patches fail by
name; for ad-hoc orders the flags are recorded and surfaced, and
the review gate shows the test-surface flag in red).
"""

from __future__ import annotations

from pathlib import PurePosixPath

# Files that can reshape how pytest collects and runs, wherever
# they sit in the tree: collection hooks, interpreter startup, and
# pytest configuration.
_STARTUP_AND_CONFIG = frozenset(
    {
        "conftest.py",
        "sitecustomize.py",
        "usercustomize.py",
        "pytest.ini",
        ".pytest.ini",
        "pyproject.toml",
        "setup.cfg",
        "tox.ini",
    }
)

# Directory names that are test surface by the repo's layout rules:
# the workshop's and ad-hoc repos' own test trees, and the shell's
# grading staging trees in a verification copy.
_TEST_DIRS = frozenset({"tests", "test", "grading", "hidden_grading"})


def is_test_surface_path(rel: str) -> bool:
    """Is ``rel`` (a repo-relative path a patch changed) part of
    the test/grading surface — a file that is, or shadows, a test
    or grading path?

    Rules, by the repo's layout: the file's basename is a test
    module (``test_*.py`` / ``*_test.py``); or any directory part
    is a test directory (``tests`` / ``test`` / ``grading`` /
    ``hidden_grading`` — so a file planted at a grading path, or
    shadowing one under a test tree, counts); or the basename is
    a collection/startup/config file (``conftest.py``,
    ``sitecustomize.py``, ``usercustomize.py``, pytest config).
    A module merely *named* like the code under test is not
    surface: ``workshop/pricing.py`` is the fix's home, never a
    test path."""
    path = PurePosixPath(rel)
    name = path.name
    if name in _STARTUP_AND_CONFIG:
        return True
    if any(part in _TEST_DIRS for part in path.parts[:-1]):
        return True
    return (name.startswith("test_") and name.endswith(".py")) or name.endswith(
        "_test.py"
    )


def test_surface_files(changed: list[str]) -> list[str]:
    """The changed files that are test/grading surface, in patch
    order."""
    return [rel for rel in changed if is_test_surface_path(rel)]


def planned_files(plan: dict) -> list[str]:
    """The files the approved plan's steps name, in step order —
    the set scope-creep detection compares the landed patch
    against. This is deliberately the *steps*, not the plan's
    derived ``files`` list: the steps are what a human approved,
    step by step."""
    planned: list[str] = []
    for step in plan.get("steps") or []:
        for name in step.get("files") or []:
            if name not in planned:
                planned.append(name)
    return planned


def unplanned_files(plan: dict, changed: list[str]) -> list[str]:
    """Changed files no step of the approved plan named, in patch
    order. A file can be inside the work order's wider scope — or
    even inside the plan's stored file list — and still be
    unplanned: no step said the work would touch it."""
    planned = set(planned_files(plan))
    return [rel for rel in changed if rel not in planned]
