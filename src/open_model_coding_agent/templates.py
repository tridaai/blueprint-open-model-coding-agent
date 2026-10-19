"""Task templates: the shapes of work, validated as data.

A template is a pattern, not an order: a task text with named
parameters (``{module}``), a scope pattern naming the files an
instantiated order will carry, and a step skeleton that arrives at
plan drafting as labelled context. This module holds the pure
arithmetic of that pattern — which parameters a pattern uses, what
a legal parameter value is, what a legal scope entry is, and the
substitution itself — so the service's governed acts (create,
publish, instantiate) all lean on one definition of the rules.

The rules exist because a template is a standing instruction a
future order will trust:

- The declared parameter set and the set the patterns actually use
  must be identical, in both directions. A parameter declared but
  never used misleads the instantiator; a placeholder used but
  never declared can never be supplied.
- A parameter value that lands in a path (the scope pattern, a
  step's files or tests) is one path-segment token — letters,
  digits, dot, dash, underscore. It can never contain a slash, a
  ``..``, or a glob character, so a value can widen nothing:
  substituted into the scope pattern it stays inside the segment
  the pattern gave it. A value that lands only in prose is freer
  text, but still brace-free, so it cannot masquerade as a
  placeholder.
- A scope entry, pattern or substituted, is a clean repo-relative
  path: never absolute, never a ``.`` or ``..`` segment, never a
  glob, never a backslash. The pattern is checked with its
  placeholders standing as a token; the substituted result is
  checked again — defence in depth, since the pattern was authored
  by one person and the values supplied by another.
"""

from __future__ import annotations

import re

from .errors import GateError

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
PLACEHOLDER_RE = re.compile(r"\{([a-z][a-z0-9_]*)\}")
VALUE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
GLOB_CHARS = frozenset("*?[]")
# What a placeholder stands as when the pattern itself is checked:
# a value-shaped token that cannot smuggle structure of its own.
_PROBE = "x"


def used_parameters(*texts: str) -> set[str]:
    """The parameter names a set of pattern texts uses. A brace
    that is not a well-formed ``{name}`` placeholder is a shape
    error, named as such — a pattern nobody can substitute is not
    a template."""
    used: set[str] = set()
    for text in texts:
        used.update(PLACEHOLDER_RE.findall(text or ""))
        remainder = PLACEHOLDER_RE.sub("", text or "")
        if "{" in remainder or "}" in remainder:
            raise GateError(
                "template_parameters_mismatch",
                f"pattern text {text!r} carries a brace that is not "
                f"a well-formed {{parameter}} placeholder — parameter "
                f"names are lowercase letters, digits, and "
                f"underscores, starting with a letter",
            )
    return used


def scope_entry_problem(entry: str) -> str | None:
    """Why a scope entry is not a clean repo-relative path, or
    None when it is one."""
    if not entry or entry != entry.strip():
        return "it is empty or padded"
    if "\\" in entry:
        return "it contains a backslash"
    if entry.startswith("/"):
        return "it is an absolute path"
    if any(ch in GLOB_CHARS for ch in entry):
        return "it contains a glob character"
    segments = entry.split("/")
    if any(seg in ("", ".", "..") for seg in segments):
        return "it contains an empty, '.', or '..' segment"
    return None


def _step_texts(steps: list[dict]):
    for step in steps:
        yield str(step.get("intent") or "")
        yield from (str(f) for f in step.get("files") or [])
        yield from (str(t) for t in step.get("tests") or [])


def validate_template_shape(
    *,
    name: str,
    description: str,
    task_text: str,
    parameters: list[str],
    scope_pattern: list[str],
    steps: list[dict],
) -> None:
    """The creation gate for a template version, raising GateError
    by the rule that refuses it. A template that passes is one an
    instantiator can trust mechanically: its parameters are exactly
    the ones its patterns use, its scope pattern is clean paths,
    and its skeleton stays inside the scope pattern it declares."""
    if not NAME_RE.match(name or ""):
        raise GateError(
            "template_name_invalid",
            f"template name {name!r} is not a valid template name — "
            f"lowercase letters, digits, and dashes, starting with a "
            f"letter or digit",
        )
    if not str(description or "").strip():
        raise GateError(
            "template_shape_invalid",
            "a template carries a description of when to use it — "
            "an unnamed purpose is how the wrong shape gets picked",
        )
    if not str(task_text or "").strip():
        raise GateError(
            "template_shape_invalid",
            "a template's task text is empty — the pattern is the "
            "task an instantiated order will carry",
        )
    declared = list(parameters or [])
    if len(set(declared)) != len(declared):
        raise GateError(
            "template_parameters_mismatch",
            f"the parameter list {declared} names a parameter twice",
        )
    for param in declared:
        if not re.fullmatch(r"[a-z][a-z0-9_]*", param or ""):
            raise GateError(
                "template_parameters_mismatch",
                f"parameter name {param!r} is not a valid parameter "
                f"name — lowercase letters, digits, and underscores, "
                f"starting with a letter",
            )
    texts = [task_text, *(scope_pattern or []), *_step_texts(steps or [])]
    used = used_parameters(*texts)
    if used != set(declared):
        raise GateError(
            "template_parameters_mismatch",
            f"declared parameters {sorted(declared)} do not match "
            f"the parameters the patterns use {sorted(used)} — a "
            f"declared parameter must be used, and a used one must "
            f"be declared",
        )
    if not scope_pattern:
        raise GateError(
            "template_scope_pattern_invalid",
            "a template's scope pattern is empty — an instantiated "
            "order must know the files it may touch",
        )
    for entry in scope_pattern:
        problem = scope_entry_problem(PLACEHOLDER_RE.sub(_PROBE, entry))
        if problem:
            raise GateError(
                "template_scope_pattern_invalid",
                f"scope pattern entry {entry!r} is not a clean "
                f"repo-relative path pattern: {problem}",
            )
    if not steps:
        raise GateError(
            "template_steps_invalid",
            "a template carries no step skeleton — the skeleton is "
            "the guidance an instantiated order's draft is shown",
        )
    scope_set = set(scope_pattern)
    for index, step in enumerate(steps):
        label = f"step {index + 1}"
        if not str(step.get("intent") or "").strip():
            raise GateError(
                "template_steps_invalid",
                f"the skeleton's {label} has no intent",
            )
        files = [str(f) for f in step.get("files") or []]
        if not files:
            raise GateError(
                "template_steps_invalid",
                f"the skeleton's {label} names no files",
            )
        for entry in files:
            problem = scope_entry_problem(PLACEHOLDER_RE.sub(_PROBE, entry))
            if problem:
                raise GateError(
                    "template_steps_invalid",
                    f"the skeleton's {label} names file {entry!r}, "
                    f"which is not a clean path pattern: {problem}",
                )
            if entry not in scope_set:
                raise GateError(
                    "template_steps_invalid",
                    f"the skeleton's {label} names file {entry!r}, "
                    f"which the scope pattern does not — a skeleton "
                    f"may not teach a plan to reach outside the "
                    f"scope the template declares",
                )
        for entry in (str(t) for t in step.get("tests") or []):
            problem = scope_entry_problem(PLACEHOLDER_RE.sub(_PROBE, entry))
            if problem:
                raise GateError(
                    "template_steps_invalid",
                    f"the skeleton's {label} names test {entry!r}, "
                    f"which is not a clean path pattern: {problem}",
                )


def path_parameters(scope_pattern: list[str], steps: list[dict]) -> set[str]:
    """The parameters whose values land in paths — the scope
    pattern, and the skeleton's files and tests. These are the
    values the strict token rule binds; a parameter used only in
    prose (the task text, a step's intent) never names a file."""
    texts = [*(scope_pattern or [])]
    for step in steps or []:
        texts.extend(str(f) for f in step.get("files") or [])
        texts.extend(str(t) for t in step.get("tests") or [])
    return used_parameters(*texts)


def validate_parameter_values(
    declared: list[str], values: dict, *, path_params: set[str]
) -> dict[str, str]:
    """An instantiation's parameter values, checked against the
    template's declared set. The set must match exactly. A value
    that lands in a path must be a single path-segment token —
    the property that makes widening by value impossible. A value
    that lands only in prose must still be a plain string with no
    brace in it, so it cannot masquerade as a placeholder."""
    supplied = set(values or {})
    missing = [p for p in declared if p not in supplied]
    if missing:
        raise GateError(
            "template_parameter_missing",
            f"instantiation refused: no value supplied for parameter"
            f"{'s' if len(missing) > 1 else ''} {missing} — a "
            f"template is instantiated whole or not at all",
        )
    unknown = sorted(supplied - set(declared))
    if unknown:
        raise GateError(
            "template_parameter_unknown",
            f"instantiation refused: values supplied for {unknown}, "
            f"which the template does not declare — a value with "
            f"nowhere to land is a sign the wrong template (or a "
            f"tampered request) is in hand",
        )
    checked: dict[str, str] = {}
    for param in declared:
        value = values[param]
        if param in path_params:
            if (
                not isinstance(value, str)
                or not VALUE_RE.match(value)
                or value in (".", "..")
                or ".." in value
            ):
                raise GateError(
                    "template_parameter_invalid",
                    f"instantiation refused: the value {value!r} "
                    f"for parameter {param!r} is not a single "
                    f"path-segment token — letters, digits, dot, "
                    f"dash, underscore, never a slash, a '..', or a "
                    f"glob; the value fills one segment of the "
                    f"scope pattern, it does not redraw the pattern",
                )
        else:
            if (
                not isinstance(value, str)
                or not value.strip()
                or "{" in value
                or "}" in value
            ):
                raise GateError(
                    "template_parameter_invalid",
                    f"instantiation refused: the value for "
                    f"parameter {param!r} must be plain prose — "
                    f"non-empty, with no braces in it; a brace "
                    f"would masquerade as a placeholder the "
                    f"template never declared",
                )
        checked[param] = value
    return checked


def substitute(text: str, values: dict[str, str]) -> str:
    """Fill a pattern's placeholders with checked values. Every
    placeholder must have a value — checked upstream — and nothing
    brace-shaped may survive."""
    out = PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], text or "")
    if "{" in out or "}" in out:
        raise GateError(
            "template_parameters_mismatch",
            f"substitution of {text!r} left a brace behind — the "
            f"pattern and the values do not belong together",
        )
    return out


def substitute_steps(steps: list[dict], values: dict[str, str]) -> list[dict]:
    return [
        {
            "intent": substitute(str(step.get("intent") or ""), values),
            "files": [substitute(str(f), values) for f in step.get("files") or []],
            "tests": [substitute(str(t), values) for t in step.get("tests") or []],
        }
        for step in steps
    ]


def check_substituted_scope(scope: list[str]) -> None:
    """The defence-in-depth pass: the scope an instantiated order
    will actually carry, checked entry by entry after substitution."""
    for entry in scope:
        problem = scope_entry_problem(entry)
        if problem:
            raise GateError(
                "template_scope_invalid",
                f"instantiation refused: the substituted scope "
                f"entry {entry!r} is not a clean repo-relative "
                f"path: {problem}",
            )


# -- the shipped library -------------------------------------------------
# The seed templates, written for this blueprint. Each is a shape
# the gold suite exercises: a missing guard, a boundary that is off
# by one, a feature that is a stub. They enter the store through
# the same create + publish acts as any template (see the service),
# authored and published by the blueprint's standing seed actors.

SEED_AUTHOR = "Blueprint Seed"
SEED_PUBLISHER = "Blueprint Seed Review"


def seed_template_specs() -> list[dict]:
    return [
        {
            "name": "guard-clause-fix",
            "description": (
                "Use when an operation that should be refused is "
                "allowed through — a missing guard lets invalid "
                "state be written. The fix refuses the operation "
                "before any state changes and touches nothing else."
            ),
            "task_text": (
                "In workshop/{module}.py, an operation is missing "
                "its guard: {behaviour}. Add the guard so the "
                "operation is refused before any state changes, "
                "leaving the module's other behaviour exactly as "
                "it stands."
            ),
            "parameters": ["module", "behaviour"],
            "scope_pattern": ["workshop/{module}.py"],
            "steps": [
                {
                    "intent": (
                        "Read workshop/{module}.py and find the "
                        "operation that writes state without first "
                        "checking it may"
                    ),
                    "files": ["workshop/{module}.py"],
                    "tests": [],
                },
                {
                    "intent": (
                        "Add the guard so the invalid operation is "
                        "refused before any state changes"
                    ),
                    "files": ["workshop/{module}.py"],
                    "tests": [],
                },
            ],
        },
        {
            "name": "boundary-fix",
            "description": (
                "Use when a threshold is off by one — the case "
                "exactly at the boundary gets the wrong side of "
                "the rule. The fix moves the comparison, nothing "
                "else."
            ),
            "task_text": (
                "In workshop/{module}.py, a boundary is off by "
                "one: {behaviour}. Correct the comparison so the "
                "case exactly at the boundary follows the stated "
                "rule, leaving every other case priced, counted, "
                "or sorted exactly as before."
            ),
            "parameters": ["module", "behaviour"],
            "scope_pattern": ["workshop/{module}.py"],
            "steps": [
                {
                    "intent": (
                        "Read workshop/{module}.py and find the "
                        "comparison that decides the boundary case"
                    ),
                    "files": ["workshop/{module}.py"],
                    "tests": [],
                },
                {
                    "intent": (
                        "Correct the comparison so the boundary "
                        "case follows the stated rule"
                    ),
                    "files": ["workshop/{module}.py"],
                    "tests": [],
                },
            ],
        },
        {
            "name": "missing-feature",
            "description": (
                "Use when a promised capability is a stub or "
                "absent — the entry point exists but does nothing, "
                "or does not exist. The fix implements exactly the "
                "named behaviour on the data the module already "
                "holds."
            ),
            "task_text": (
                "In workshop/{module}.py, a promised capability "
                "is missing: {behaviour}. Implement it on the "
                "data the module already holds, in the module's "
                "own idiom, leaving its other behaviour exactly "
                "as it stands."
            ),
            "parameters": ["module", "behaviour"],
            "scope_pattern": ["workshop/{module}.py"],
            "steps": [
                {
                    "intent": (
                        "Read workshop/{module}.py and find where "
                        "the missing capability belongs in the "
                        "module's existing shape"
                    ),
                    "files": ["workshop/{module}.py"],
                    "tests": [],
                },
                {
                    "intent": (
                        "Implement the missing capability on the "
                        "data the module already holds"
                    ),
                    "files": ["workshop/{module}.py"],
                    "tests": [],
                },
            ],
        },
    ]
