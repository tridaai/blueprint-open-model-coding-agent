"""The challenge pass (round 13): a second, independent look at a
verified patch, computed from the run's stored records alone.

Verification (round 10) answers "did the patch pass tests that
genuinely run, without touching the surface it is graded by or a
file no plan step named?" The challenge pass asks the questions
verification does not: within the files the plan *did* name, did
the patch stay inside what the plan and the task actually asked
for? It runs after a run lands SUCCEEDED and before the review
gate can accept, and it reads only stored records — the run's
patch, the approved plan record, the work order's task text, and
the run's retained pristine snapshot (the same ground the patch
was diffed against and verification graded on). It never reads
the model's transcript, its self-report, or any drafting
internals: the same records a human reviewer sees are the whole
of its evidence.

Three rules, each deterministic:

- ``intent_creep`` — a hunk modifies an existing function or
  class that neither the task text nor any plan step names. A
  symbol counts as named when its identifier appears (word
  match, case-insensitive — prose mentions count) in the task
  text or the plan's approach and step intents, or when any
  enclosing symbol in its chain is named: editing ``__init__``
  inside a named class is work inside named work. A public
  symbol nobody named is a ``must_answer`` challenge; a private
  one (leading underscore) inside an unnamed chain is
  ``advisory`` — worth a look, not a block. Attribution is by
  the lines a hunk removes and by where its insertions land;
  context lines are not touches, an insertion that introduces
  a definition is the surface rule's to judge, and a change at
  module level (no enclosing symbol) is not judged here at
  all. False negatives of those kinds are accepted; false
  positives are what the rule is built to avoid.

- ``single_source_figures`` — a numeric figure in an added line
  that appears nowhere in the task text, the plan, or the
  changed file's prior content: a number whose only source is
  the patch itself. Figures are compared by value (``5``,
  ``5.0`` and ``05`` are the same figure). The structural
  constants 0 and 1 are exempt — a default, an index, an
  increment is not a figure in the sense this rule judges; a
  rate, a threshold, an amount is.

- ``unrequested_surface`` — an added line introduces a new
  public function or class (a ``def``/``class`` whose name the
  file did not already define) that neither the task text nor
  the plan names. New public surface is a commitment other code
  will grow against; nobody asking for it is exactly the kind
  of thing a reviewer should have to answer for. Private
  introductions are not surface.

A fourth candidate rule from the round's design —
``weakened_tests_signal``, for hunks adjacent to test
configuration or assertion-shaped lines in non-test files — is
deliberately NOT built: round 10's test-surface check already
names every test-configuration file (``conftest.py``,
``sitecustomize.py``, ``usercustomize.py``, ``pytest.ini``,
``pyproject.toml``, ``setup.cfg``, ``tox.ini``) and refuses a
gold patch that touches one at verification, so such a patch
never reaches SUCCEEDED and a challenge rule for it could never
fire on a record this pass sees; on ad-hoc orders the touch is
already recorded on the verification record and shown at the
review gate. A duplicate would add ceremony, not signal.

Each fired rule yields a candidate challenge: the rule, its
severity, and its evidence — the file, the hunk header, and the
exact values compared (the symbol chain, or the figure as
written). The evidence key is a hash over the rule and the
canonical evidence, so re-running the pass over the same run
reproduces the same keys and the service can refuse to store a
duplicate: the pass is idempotent by construction.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re

MUST_ANSWER = "must_answer"
ADVISORY = "advisory"

RULE_INTENT_CREEP = "intent_creep"
RULE_SINGLE_SOURCE_FIGURES = "single_source_figures"
RULE_UNREQUESTED_SURFACE = "unrequested_surface"

# The structural constants: defaults, indexes, increments. A
# figure in the sense the figures rule judges carries domain
# meaning — a rate, a threshold, an amount — and 0 and 1 do not.
_STRUCTURAL_FIGURES = {"0", "1"}

_NUMBER_RE = re.compile(r"(?<![\w.])\d+(?:\.\d+)?(?![\w.])")
_DEF_LINE_RE = re.compile(
    r"^(?P<indent>\s*)(?:async\s+def|def|class)\s+(?P<name>[A-Za-z_]\w*)"
)
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


# -- the patch, parsed -------------------------------------------------


def _parse_patch(patch_text: str) -> dict[str, list[dict]]:
    """Per-file hunks: the header, the old/new spans, and the body
    lines with their diff tags, in patch order."""
    sections: dict[str, list[dict]] = {}
    current_file: str | None = None
    current: dict | None = None
    for line in patch_text.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[len("+++ b/") :].strip()
            sections.setdefault(current_file, [])
            current = None
            continue
        match = _HUNK_RE.match(line)
        if match and current_file is not None:
            current = {
                "header": line.strip(),
                "old_start": int(match.group(1)),
                "old_count": int(match.group(2) or 1),
                "new_start": int(match.group(3)),
                "new_count": int(match.group(4) or 1),
                "lines": [],
            }
            sections[current_file].append(current)
            continue
        if current is not None and line[:1] in (" ", "-", "+"):
            current["lines"].append((line[0], line[1:]))
    return sections


# -- symbols in a file's prior content -----------------------------------


class _Symbol:
    __slots__ = ("name", "kind", "indent", "start", "end", "chain")

    def __init__(
        self, name: str, kind: str, indent: int, start: int, end: int,
        chain: tuple,
    ):
        self.name = name
        self.kind = kind  # "def" | "class"
        self.indent = indent
        self.start = start
        self.end = end
        self.chain = chain


def _ast_symbols(tree: ast.AST) -> list[_Symbol]:
    out: list[_Symbol] = []

    def visit(node: ast.AST, chain: tuple) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                sub = chain + (child.name,)
                out.append(
                    _Symbol(
                        child.name,
                        "class" if isinstance(child, ast.ClassDef) else "def",
                        child.col_offset,
                        child.lineno,
                        child.end_lineno or child.lineno,
                        sub,
                    )
                )
                visit(child, sub)
            else:
                visit(child, chain)

    visit(tree, ())
    return out


def _scanned_symbols(text: str) -> list[_Symbol]:
    """The fallback for content the AST cannot read: an
    indentation scan. A symbol runs from its def/class line to
    the line before the next def/class at its indent or
    shallower; its chain is the indent stack in force at its
    line. Approximate where the AST is exact — it exists so an
    unparseable file degrades to a coarser pass, never to none."""
    lines = text.splitlines()
    found: list[tuple[str, str, int, int]] = []  # name, kind, start, indent
    for index, line in enumerate(lines, start=1):
        match = _DEF_LINE_RE.match(line)
        if match:
            kind = (
                "class"
                if line.lstrip().startswith("class ")
                else "def"
            )
            found.append(
                (match.group("name"), kind, index, len(match.group("indent")))
            )
    symbols: list[_Symbol] = []
    for pos, (name, kind, start, indent) in enumerate(found):
        end = len(lines)
        for _n, _k, other_start, other_indent in found[pos + 1 :]:
            if other_indent <= indent:
                end = other_start - 1
                break
        stack: list[tuple[str, int]] = []
        for n, _k, s, i in found[:pos]:
            if s < start and i < indent:
                while stack and stack[-1][1] >= i:
                    stack.pop()
                stack.append((n, i))
        chain = tuple(n for n, _i in stack) + (name,)
        symbols.append(_Symbol(name, kind, indent, start, end, chain))
    return symbols


def file_symbols(text: str | None) -> list[_Symbol]:
    """The def/class symbols of a file's prior content, with
    their line spans and enclosing chains. Empty for missing
    content (a created file has no prior symbols to creep
    inside)."""
    if not text:
        return []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return _scanned_symbols(text)
    return _ast_symbols(tree)


def _chain_at(symbols: list[_Symbol], line: int) -> tuple:
    """The enclosing chain at a 1-based line: the deepest symbol
    whose span contains the line, named outermost-first."""
    best: tuple = ()
    best_key = (-1, -1)
    for sym in symbols:
        if sym.start <= line <= sym.end:
            key = (len(sym.chain), sym.start)
            if key > best_key:
                best_key = key
                best = sym.chain
    return best


# -- the naming corpus ----------------------------------------------------


def _named(name: str, corpus: str) -> bool:
    """Is an identifier named in the corpus — the task text and
    the plan's own words? A word match, case-insensitive: prose
    that talks about `the ledger's reserve` names ``reserve``."""
    return (
        re.search(r"\b" + re.escape(name) + r"\b", corpus, re.IGNORECASE)
        is not None
    )


def _figures_in(text: str) -> set[str]:
    """The numeric figures a text carries, normalised by value
    so ``5``, ``5.0`` and ``05`` are the one figure."""
    out: set[str] = set()
    for token in _NUMBER_RE.findall(text or ""):
        out.add(_normalise_figure(token))
    return out


def _normalise_figure(token: str) -> str:
    from decimal import Decimal

    return format(Decimal(token).normalize(), "f")


# -- the pass ---------------------------------------------------------------


def compute_challenges(
    *,
    task_text: str,
    plan: dict,
    patch_text: str,
    changed_files: list[str],
    prior_texts: dict[str, str | None],
) -> list[dict]:
    """The candidate challenges over one succeeded run's
    records. Pure: the same records in, the same candidates
    out, in a stable order (file, rule, evidence). Each
    candidate carries its evidence key — the service dedupes
    stored challenges on (run, key)."""
    corpus_parts = [task_text or "", str(plan.get("approach") or "")]
    for step in plan.get("steps") or []:
        corpus_parts.append(str(step.get("intent") or ""))
        corpus_parts.extend(str(f) for f in step.get("files") or [])
        corpus_parts.extend(str(t) for t in step.get("tests") or [])
    corpus = "\n".join(corpus_parts)
    text_figures = _figures_in(corpus)

    sections = _parse_patch(patch_text)
    candidates: list[dict] = []
    for rel in changed_files:
        hunks = sections.get(rel, [])
        if not hunks:
            continue
        prior = prior_texts.get(rel)
        symbols = file_symbols(prior)
        prior_names = {sym.name for sym in symbols}
        candidates.extend(
            _intent_creep(rel, hunks, symbols, corpus)
        )
        candidates.extend(
            _unrequested_surface(rel, hunks, prior_names, corpus)
        )
        candidates.extend(
            _single_source_figures(
                rel, hunks, prior, text_figures
            )
        )
    candidates.sort(
        key=lambda c: (c["evidence"].get("file", ""), c["rule"], c["evidence_key"])
    )
    return candidates


def _candidate(rule: str, severity: str, evidence: dict) -> dict:
    key = hashlib.sha256(
        json.dumps(
            {"rule": rule, "evidence": evidence}, sort_keys=True
        ).encode()
    ).hexdigest()[:20]
    return {
        "rule": rule,
        "severity": severity,
        "evidence": evidence,
        "evidence_key": key,
    }


def _anchors(hunk: dict) -> list[tuple[str, int, int | None]]:
    """The touches a hunk makes, as (kind, old line, added
    indent): every removed line, and for each run of added lines
    that does not itself open a definition, the old line the run
    lands after and the indent of its first non-blank line.
    Context lines are not touches — a hunk's context reaching
    into the next function does not make the patch a change to
    that function — and an added run opening with a def/class
    line is an introduction, which the surface rule judges, so
    it anchors nothing here."""
    anchors: list[list] = []
    cursor = hunk["old_start"]
    in_run: str | None = None
    pending: list | None = None
    for tag, text in hunk["lines"]:
        if tag == "-":
            anchors.append(["removed", cursor, None])
            cursor += 1
            in_run = None
            pending = None
        elif tag == " ":
            cursor += 1
            in_run = None
            pending = None
        elif tag == "+":
            if in_run is None:
                if _DEF_LINE_RE.match(text):
                    in_run = "intro"
                else:
                    in_run = "plain"
                    pending = ["insert", max(cursor - 1, 1), None]
                    anchors.append(pending)
            if in_run == "plain" and pending is not None and text.strip():
                if pending[2] is None:
                    pending[2] = len(text) - len(text.lstrip())
    return [(kind, line, indent) for kind, line, indent in anchors]


def _insertion_chain(
    symbols: list[_Symbol], line: int, added_indent: int | None
) -> tuple:
    """Where an insertion lands. An insertion after a symbol's
    last line is outside that symbol — appending a function
    after a function is module-level work, not an edit to the
    function above it — unless the inserted lines are indented
    as that symbol's body, which makes them its content. An
    insertion landing at class or module level (after dropping
    the symbols it falls outside) is not creep: introductions
    there are the surface rule's, and edits there have no
    enclosing intent to stray from."""
    chain = _chain_at(symbols, line)
    if not chain:
        return ()
    inner = next((s for s in symbols if s.chain == chain), None)
    if inner is None or inner.end > line:
        return chain
    if added_indent is not None and added_indent > inner.indent:
        return chain
    parent = chain[:-1]
    if not parent:
        return ()
    parent_sym = next((s for s in symbols if s.chain == parent), None)
    if parent_sym is not None and parent_sym.kind == "def":
        return parent
    return ()


def _intent_creep(
    rel: str, hunks: list[dict], symbols: list[_Symbol], corpus: str
) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for hunk in hunks:
        chains = set()
        for kind, line_no, added_indent in _anchors(hunk):
            if kind == "removed":
                chain = _chain_at(symbols, line_no)
            else:
                chain = _insertion_chain(symbols, line_no, added_indent)
            if chain:
                chains.add(chain)
        for chain in sorted(chains):
            if any(_named(name, corpus) for name in chain):
                continue
            innermost = chain[-1]
            if innermost in seen:
                continue
            seen.add(innermost)
            severity = (
                ADVISORY if innermost.startswith("_") else MUST_ANSWER
            )
            out.append(
                _candidate(
                    RULE_INTENT_CREEP,
                    severity,
                    {
                        "file": rel,
                        "symbol": innermost,
                        "chain": ".".join(chain),
                        "hunk": hunk["header"],
                    },
                )
            )
    return out


def _unrequested_surface(
    rel: str, hunks: list[dict], prior_names: set[str], corpus: str
) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for hunk in hunks:
        for tag, text in hunk["lines"]:
            if tag != "+":
                continue
            match = _DEF_LINE_RE.match(text)
            if not match:
                continue
            name = match.group("name")
            if name.startswith("_") or name in seen:
                continue
            if name in prior_names:
                # A signature change to a symbol the file already
                # defined is an edit, not an introduction.
                continue
            if _named(name, corpus):
                continue
            seen.add(name)
            out.append(
                _candidate(
                    RULE_UNREQUESTED_SURFACE,
                    MUST_ANSWER,
                    {
                        "file": rel,
                        "symbol": name,
                        "hunk": hunk["header"],
                        "line": text.strip()[:200],
                    },
                )
            )
    return out


def _single_source_figures(
    rel: str,
    hunks: list[dict],
    prior: str | None,
    text_figures: set[str],
) -> list[dict]:
    carried = set(text_figures)
    if prior:
        carried |= _figures_in(prior)
    else:
        # No pristine content to read (a created file, or the
        # snapshot gone): the patch's own old side is the prior
        # content the records carry.
        old_side = "\n".join(
            text
            for hunk in hunks
            for tag, text in hunk["lines"]
            if tag in (" ", "-")
        )
        carried |= _figures_in(old_side)
    out: list[dict] = []
    seen: set[str] = set()
    for hunk in hunks:
        for tag, text in hunk["lines"]:
            if tag != "+":
                continue
            for token in _NUMBER_RE.findall(text):
                value = _normalise_figure(token)
                if value in _STRUCTURAL_FIGURES or value in carried:
                    continue
                if value in seen:
                    continue
                seen.add(value)
                out.append(
                    _candidate(
                        RULE_SINGLE_SOURCE_FIGURES,
                        MUST_ANSWER,
                        {
                            "file": rel,
                            "value": token,
                            "normalised": value,
                            "hunk": hunk["header"],
                            "line": text.strip()[:200],
                        },
                    )
                )
    return out
