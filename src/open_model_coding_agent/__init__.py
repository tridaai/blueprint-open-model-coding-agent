"""Trida AI Blueprint — Open-Model Coding Agent on Baseten.

A governed coding agent: scoped work orders against a codebase, a
human plan gate, sandboxed execution by an open model served through
Baseten's Model APIs, verification of the produced patch in code on a
pristine copy, and a human review gate. The agent returns a patch; it
never commits, pushes, or merges.
"""

__version__ = "0.1.0"
