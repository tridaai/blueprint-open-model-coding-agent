"""Model profiles: versioned JSON records, loaded fail-closed.

A profile is data, not code: one JSON record per servable model
configuration in the profiles directory. The loader refuses, by
name, anything it does not fully understand — an unknown profile
name, an unknown field in a record, a malformed or unlisted model
slug, a live profile missing its endpoint or key env. There is no
silent fallback to another profile, ever: a deployment that names a
profile gets that profile, exactly as recorded, or a startup error.

Profiles are edited per deployment (a team pins the slug builds it
has evaluated). Editing the data never widens what a profile may
serve beyond its own record: the slug a run uses must be the
profile's recorded default, an env override the loader validates
against the profile's own allowlist, or nothing.

Two more facts live on a live record. ``max_tokens`` is the
per-request completion ceiling the adapter sends — governed here,
as data, because a reasoning model spends completion tokens on
reasoning before any visible content, and the ceiling that suits
one model starves another. And ``servable`` records whether the
provider's catalog currently serves the profile's model at all:
a profile whose model the provider does not list is recorded
``servable: false`` with the reason (and the date it was read) on
the record, and every serving path refuses it by that reason —
never a mysterious 404 mid-run, never a pretense that the slug
is live.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from pathlib import Path

from .errors import ConfigError

SLUG_SHAPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")

# The completion ceiling a live record carries when it does not
# name one. Generous on purpose: a reasoning model can spend most
# of a small budget on reasoning_content and return nothing.
DEFAULT_MAX_TOKENS = 8192

_COMMON_FIELDS = {"profile_id", "version", "label", "kind"}
_LIVE_FIELDS = {
    "base_url",
    "api_key_env",
    "model",
    "allowed_models",
    "allowed_model_prefixes",
    "max_tokens",
    "servable",
    "not_servable_reason",
}
_KINDS = ("offline", "baseten")


@dataclass(frozen=True)
class ModelProfile:
    """One servable model configuration, as recorded."""

    profile_id: str
    version: int
    label: str
    kind: str  # "offline" | "baseten"
    base_url: str | None = None
    api_key_env: str | None = None
    model: str | None = None
    allowed_models: tuple[str, ...] = ()
    allowed_model_prefixes: tuple[str, ...] = ()
    max_tokens: int = DEFAULT_MAX_TOKENS
    servable: bool = True
    not_servable_reason: str | None = None

    @property
    def is_live(self) -> bool:
        return self.kind != "offline"

    def servability_refusal(self) -> ConfigError:
        """The named refusal every serving path raises for a
        profile the provider does not currently serve. The record's
        own reason is the message — a benchmark or comparison that
        records this refusal records the reason with it."""
        return ConfigError(
            f"model profile {self.profile_id!r} is recorded not "
            f"servable: {self.not_servable_reason}"
        )

    def slug_allowed(self, slug: str) -> bool:
        """A slug is servable by this profile only in the shape the
        profile itself records: the org/name form, and listed in the
        profile's allowlist or under one of its allowed prefixes."""
        if not SLUG_SHAPE.match(slug or ""):
            return False
        if slug in self.allowed_models:
            return True
        return any(slug.startswith(p) for p in self.allowed_model_prefixes)


def _refuse(profile_id: str, problem: str) -> ConfigError:
    return ConfigError(f"model profile {profile_id!r}: {problem}")


def _parse_record(profile_id: str, raw: object, source: Path) -> ModelProfile:
    if not isinstance(raw, dict):
        raise _refuse(profile_id, f"{source.name} is not a JSON object")
    unknown = sorted(set(raw) - _COMMON_FIELDS - _LIVE_FIELDS)
    if unknown:
        raise _refuse(
            profile_id,
            f"unknown field(s) {unknown} in {source.name}; the loader "
            f"refuses records it does not fully understand",
        )
    if raw.get("profile_id") != profile_id:
        raise _refuse(
            profile_id,
            f"{source.name} records profile_id {raw.get('profile_id')!r}; "
            f"the file name and the record must agree",
        )
    version = raw.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise _refuse(profile_id, f"version must be a positive integer, got {version!r}")
    label = raw.get("label")
    if not isinstance(label, str) or not label:
        raise _refuse(profile_id, "label must be a non-empty string")
    kind = raw.get("kind")
    if kind not in _KINDS:
        raise _refuse(profile_id, f"kind must be one of {_KINDS}, got {kind!r}")
    if kind == "offline":
        carried = sorted(f for f in _LIVE_FIELDS if raw.get(f) is not None)
        if carried:
            raise _refuse(
                profile_id,
                f"an offline profile carries no live fields, found {carried}",
            )
        return ModelProfile(profile_id=profile_id, version=version, label=label, kind=kind)
    # Live profiles: every serving field is required and validated.
    base_url = raw.get("base_url")
    if not isinstance(base_url, str) or not base_url.startswith("https://"):
        raise _refuse(profile_id, f"base_url must be an https URL, got {base_url!r}")
    api_key_env = raw.get("api_key_env")
    if not isinstance(api_key_env, str) or not api_key_env:
        raise _refuse(profile_id, "api_key_env must name the environment variable holding the key")
    allowed_models = raw.get("allowed_models") or []
    allowed_prefixes = raw.get("allowed_model_prefixes") or []
    if not all(isinstance(s, str) for s in allowed_models + allowed_prefixes):
        raise _refuse(profile_id, "allowed_models and allowed_model_prefixes must be strings")
    max_tokens = raw.get("max_tokens", DEFAULT_MAX_TOKENS)
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens < 1:
        raise _refuse(
            profile_id,
            f"max_tokens must be a positive integer (the per-request "
            f"completion ceiling), got {max_tokens!r}",
        )
    servable = raw.get("servable", True)
    if not isinstance(servable, bool):
        raise _refuse(profile_id, f"servable must be a boolean, got {servable!r}")
    not_servable_reason = raw.get("not_servable_reason")
    if not_servable_reason is not None and (
        not isinstance(not_servable_reason, str) or not not_servable_reason.strip()
    ):
        raise _refuse(
            profile_id, "not_servable_reason must be a non-empty string when recorded"
        )
    if not servable and not not_servable_reason:
        raise _refuse(
            profile_id,
            "a profile recorded servable: false must carry its "
            "not_servable_reason — a refusal nobody can read is not "
            "governance, it is a dead end",
        )
    if servable and not_servable_reason:
        raise _refuse(
            profile_id,
            "a servable profile carries no not_servable_reason; the "
            "record contradicts itself",
        )
    profile = ModelProfile(
        profile_id=profile_id,
        version=version,
        label=label,
        kind=kind,
        base_url=base_url.rstrip("/"),
        api_key_env=api_key_env,
        model=raw.get("model"),
        allowed_models=tuple(allowed_models),
        allowed_model_prefixes=tuple(allowed_prefixes),
        max_tokens=max_tokens,
        servable=servable,
        not_servable_reason=not_servable_reason,
    )
    model = raw.get("model")
    if not isinstance(model, str) or not profile.slug_allowed(model):
        raise _refuse(
            profile_id,
            f"model slug {model!r} is not in this profile's own allowlist "
            f"(models {list(profile.allowed_models)}, prefixes "
            f"{list(profile.allowed_model_prefixes)}, org/name shape required)",
        )
    return profile


def load_profile(profile_id: str, profiles_dir: Path) -> ModelProfile:
    """Resolve one profile by id from the profiles directory,
    refusing unknown names and malformed records by name."""
    path = Path(profiles_dir) / f"{profile_id}.json"
    if not path.is_file():
        valid = sorted(p.stem for p in Path(profiles_dir).glob("*.json"))
        raise ConfigError(
            f"unknown model profile {profile_id!r}; valid profiles: "
            f"{', '.join(valid)}"
        )
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise _refuse(profile_id, f"{path.name} is not valid JSON: {exc}") from None
    return _parse_record(profile_id, raw, path)


def list_profiles(profiles_dir: Path) -> list[ModelProfile]:
    """Every recorded profile, loaded fail-closed (a broken record
    refuses the listing rather than being skipped)."""
    return [
        load_profile(path.stem, profiles_dir)
        for path in sorted(Path(profiles_dir).glob("*.json"))
    ]


def apply_overrides(
    profile: ModelProfile, *, model: str | None = None, base_url: str | None = None
) -> ModelProfile:
    """Deployment overrides (from the environment), validated against
    the profile's own record. An override never widens the profile:
    a slug outside its allowlist is refused by name."""
    if profile.kind == "offline":
        if model or base_url:
            raise _refuse(
                profile.profile_id,
                "the offline profile takes no model or endpoint override",
            )
        return profile
    out = profile
    if base_url:
        if not base_url.startswith("https://"):
            raise _refuse(profile.profile_id, f"base_url override must be an https URL, got {base_url!r}")
        out = replace(out, base_url=base_url.rstrip("/"))
    if model:
        if not out.slug_allowed(model):
            raise _refuse(
                profile.profile_id,
                f"model override {model!r} is outside this profile's "
                f"allowlist (models {list(profile.allowed_models)}, prefixes "
                f"{list(profile.allowed_model_prefixes)}); select the "
                f"profile that governs that model instead",
            )
        out = replace(out, model=model)
    return out
