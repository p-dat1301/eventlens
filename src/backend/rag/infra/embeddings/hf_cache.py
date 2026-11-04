"""Resolve the pinned snapshot commit of a locally cached Hugging Face model."""

import os
from pathlib import Path
from typing import ClassVar


class ModelNotCachedError(RuntimeError):
    """The model has no complete local snapshot; nothing is downloaded."""

    code: ClassVar[str] = "model_not_cached"


def hub_cache() -> Path:
    """``HF_HUB_CACHE`` or ``$HF_HOME/hub`` or ``~/.cache/huggingface/hub``."""
    explicit = os.environ.get("HF_HUB_CACHE")
    if explicit:
        return Path(explicit)
    home = os.environ.get("HF_HOME")
    return (Path(home) if home else Path.home() / ".cache/huggingface") / "hub"


def snapshot_revision(model: str) -> str:
    """Commit hash recorded in ``refs/main`` whose snapshot has a config."""
    root = hub_cache() / f"models--{model.replace('/', '--')}"
    ref = root / "refs" / "main"
    if not ref.is_file():
        raise ModelNotCachedError(model)
    revision = ref.read_text(encoding="utf-8").strip()
    if not (root / "snapshots" / revision / "config.json").is_file():
        raise ModelNotCachedError(model)
    return revision
