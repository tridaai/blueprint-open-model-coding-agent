"""Errors the shell raises on purpose.

A GateError is a governance refusal — the request was understood and
refused by a rule, and the refusal is recorded. A ConfigError is a
deployment mistake: the process should not start half-configured.
"""

from __future__ import annotations


class GateError(Exception):
    """A governance refusal, carrying a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ConfigError(Exception):
    """The deployment is misconfigured; refuse loudly at startup."""
