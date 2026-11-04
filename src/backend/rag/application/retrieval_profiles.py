"""Named retrieval tunings shipped with the package (opt-in, default baseline).

``policies/retrieval-tuning.v1.json`` lists every profile explicitly. A
profile may ``extends`` another one; its sections replace the parent's
section keys one by one. ``baseline`` must equal ``BASELINE_TUNING``
(the frozen v3 behaviour), so selecting it is always the way back.
"""

# ruff: noqa: EM101, EM102, TRY003 - one-line validation messages

import json
from collections.abc import Mapping
from functools import cache
from importlib.resources import files
from typing import Final, cast

from backend.rag.domain.retrieval_tuning import (
    BASELINE_TUNING,
    BASELINE_TUNING_ID,
    TUNING_SCHEMA,
    InvalidTuningError,
    RetrievalTuning,
    parse_tuning,
)

PROFILES_RESOURCE: Final = "retrieval-tuning.v1.json"
SECTIONS: Final = ("weights", "selection", "diversity", "freshness", "boosts")


def _resolve(
    name: str, profiles: Mapping[str, object], seen: tuple[str, ...] = ()
) -> dict[str, object]:
    if name in seen:
        raise InvalidTuningError(f"profile cycle {' -> '.join((*seen, name))}")
    raw = profiles.get(name)
    if not isinstance(raw, Mapping):
        raise InvalidTuningError(f"unknown profile {name!r}; known {sorted(profiles)}")
    body = dict(cast("Mapping[str, object]", raw))
    parent = body.pop("extends", None)
    if parent is None:
        return body
    if not isinstance(parent, str):
        raise InvalidTuningError("extends must name a profile")
    merged = _resolve(parent, profiles, (*seen, name))
    for key, value in body.items():
        base = merged.get(key)
        if key in SECTIONS and isinstance(base, Mapping) and isinstance(value, Mapping):
            merged[key] = {**base, **value}
        else:
            merged[key] = value
    return merged


def load_profiles(text: str) -> dict[str, RetrievalTuning]:
    """Parse and validate every profile of a profiles document."""
    document = cast("object", json.loads(text))
    if not isinstance(document, dict):
        raise InvalidTuningError(f"profiles document must be {TUNING_SCHEMA}")
    body = cast("dict[str, object]", document)
    if body.get("schema_version") != TUNING_SCHEMA:
        raise InvalidTuningError(f"profiles document must be {TUNING_SCHEMA}")
    profiles = body.get("profiles")
    if not isinstance(profiles, dict):
        raise InvalidTuningError("profiles must be an object")
    known = cast("dict[str, object]", profiles)
    parsed = {name: parse_tuning(name, _resolve(name, known)) for name in known}
    if parsed.get(BASELINE_TUNING_ID) != BASELINE_TUNING:
        raise InvalidTuningError("baseline profile must equal the frozen v3 tuning")
    return parsed


@cache
def bundled_profiles() -> dict[str, RetrievalTuning]:
    """Profiles shipped with the package."""
    text = (
        files("backend.rag.application")
        .joinpath("policies")
        .joinpath(PROFILES_RESOURCE)
        .read_text(encoding="utf-8")
    )
    return load_profiles(text)


def bundled_tuning(name: str = BASELINE_TUNING_ID) -> RetrievalTuning:
    """One named profile; unknown names fail (never a silent default)."""
    profiles = bundled_profiles()
    tuning = profiles.get(name)
    if tuning is None:
        raise InvalidTuningError(f"unknown profile {name!r}; known {sorted(profiles)}")
    return tuning
