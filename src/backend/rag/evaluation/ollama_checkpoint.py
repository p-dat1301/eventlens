"""Durable, resumable response store for the offline Ollama bench (private only).

Records hold the exact prompt (publisher text) and the raw model output, so the
store must live under a ``.private/`` directory (gitignored). Layout::

    manifest.json                        plan + run history (text-free)
    responses/<model-key>/<qid>.json     latest record per (model, question)
    rejected/<model-key>.<qid>.<reason>.<n>.json   unreadable, stale or retried

Every file is written atomically (temp file in the same directory, fsync,
``os.replace``): a crash leaves the previous complete file or the new one,
never a torn one. A record is accepted only when it parses under the strict
schema and its ``record_digest`` verifies; the bench additionally reuses it
only when its ``request_id`` equals the one recomputed for the current plan.
"""

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, ClassVar, Final, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from backend.rag.evaluation.metrics import Json, canonical, digest

RECORD_SCHEMA: Final = "ollama-bench-response.v1"
CHECKPOINT_SCHEMA: Final = "ollama-bench-checkpoint.v1"
PRIVATE_DIR: Final = ".private"
_SAFE: Final = re.compile(r"[A-Za-z0-9._-]+")


class CheckpointLocationError(ValueError):
    """The store would hold publisher text outside ``.private/``."""


def sha256_text(text: str) -> str:
    """``sha256:<hex>`` of UTF-8 text."""
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


class EvidenceRef(BaseModel):
    """One span exactly as sent (text-free identity; label-independent)."""

    model_config: ClassVar[ConfigDict] = ConfigDict(
        strict=True, extra="forbid", frozen=True
    )

    label: str
    chunk_id: str
    article_id: str
    content_hash: str
    chunk_version: str
    char_start: int
    char_end: int
    text_sha256: str


class ResponseRecord(BaseModel):
    """One attempted generation (ok or execution error) with its provenance."""

    model_config: ClassVar[ConfigDict] = ConfigDict(
        strict=True, extra="forbid", frozen=True
    )

    record_schema: Literal["ollama-bench-response.v1"]
    request_id: Annotated[str, Field(min_length=1)]
    identity: dict[str, JsonValue]
    question_id: str
    model: str
    model_digest: str
    run_id: str
    started_at: str
    status: Literal["ok", "error"]
    latency_ms: float | None
    content: str | None
    usage: dict[str, int | str | None]
    error: str | None
    prompt: str
    evidence: list[EvidenceRef]
    record_digest: str = ""

    def body(self) -> dict[str, Json]:
        """Everything but ``record_digest`` (what the digest covers)."""
        return cast(
            "dict[str, Json]", self.model_dump(mode="json", exclude={"record_digest"})
        )

    def sealed(self) -> "ResponseRecord":
        """Same record with its integrity digest set."""
        return self.model_copy(update={"record_digest": digest(self.body())})


@dataclass(frozen=True, slots=True)
class Loaded:
    """A verified record, or why the file on disk was not usable."""

    record: ResponseRecord | None
    rejection: str | None = None


def atomic_write(path: Path, text: str) -> None:
    """Write ``text`` so readers see the old file or the complete new one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            _ = stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        _ = Path(temp).replace(path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def _key(value: str) -> str:
    if _SAFE.fullmatch(value):
        return value
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", value)
    return f"{cleaned}-{hashlib.sha256(value.encode()).hexdigest()[:8]}"


@dataclass(frozen=True, slots=True)
class CheckpointStore:
    """Files under ``root``; ``root`` must be inside a ``.private`` directory."""

    root: Path

    def __post_init__(self) -> None:
        """Refuse locations that are not private (publisher text inside)."""
        if PRIVATE_DIR not in self.root.absolute().parts:
            msg = f"checkpoint {self.root} must be inside a {PRIVATE_DIR}/ directory"
            raise CheckpointLocationError(msg)

    @property
    def manifest_path(self) -> Path:
        """Plan and run history."""
        return self.root / "manifest.json"

    def path(self, model: str, question_id: str) -> Path:
        """Record file of one (model, question)."""
        return self.root / "responses" / _key(model) / f"{_key(question_id)}.json"

    def load(self, model: str, question_id: str) -> Loaded:
        """Verified record, ``Loaded(None)`` if absent, else a rejection reason."""
        path = self.path(model, question_id)
        if not path.exists():
            return Loaded(None)
        try:
            record = ResponseRecord.model_validate_json(path.read_bytes())
        except ValidationError as error:
            kind = "unreadable" if _json_error(error) else "schema"
            return Loaded(None, kind)
        if record.record_digest != digest(record.body()):
            return Loaded(None, "digest")
        if record.model != model or record.question_id != question_id:
            return Loaded(None, "misfiled")
        return Loaded(record)

    def save(self, record: ResponseRecord) -> ResponseRecord:
        """Seal and write atomically; returns the sealed record."""
        sealed = record.sealed()
        atomic_write(
            self.path(sealed.model, sealed.question_id),
            canonical(cast("Json", sealed.model_dump(mode="json"))) + "\n",
        )
        return sealed

    def reject(self, model: str, question_id: str, reason: str) -> Path | None:
        """Move the current file aside (kept for audit); never deletes it."""
        source = self.path(model, question_id)
        if not source.exists():
            return None
        folder = self.root / "rejected"
        folder.mkdir(parents=True, exist_ok=True)
        stem = f"{_key(model)}.{_key(question_id)}.{reason}"
        index = sum(1 for _ in folder.glob(f"{stem}.*.json")) + 1
        target = folder / f"{stem}.{index}.json"
        _ = source.replace(target)
        return target

    def records(self, model: str) -> dict[str, ResponseRecord]:
        """All verified records of one model (unverifiable files are skipped)."""
        folder = self.root / "responses" / _key(model)
        found: dict[str, ResponseRecord] = {}
        for path in sorted(folder.glob("*.json")) if folder.exists() else ():
            try:
                record = ResponseRecord.model_validate_json(path.read_bytes())
            except ValidationError:
                continue
            if record.record_digest == digest(record.body()):
                found[record.question_id] = record
        return found

    def load_manifest(self) -> dict[str, Json] | None:
        """The manifest, or ``None`` when absent or unreadable."""
        try:
            value = cast("Json", json.loads(self.manifest_path.read_bytes()))
        except (OSError, ValueError):
            return None
        if not isinstance(value, dict):
            return None
        if value.get("checkpoint_schema") != CHECKPOINT_SCHEMA:
            return None
        return value

    def save_manifest(self, manifest: dict[str, Json]) -> None:
        """Atomic manifest write."""
        atomic_write(self.manifest_path, canonical(manifest) + "\n")


def _json_error(error: ValidationError) -> bool:
    return any(item["type"] == "json_invalid" for item in error.errors())


__all__ = [
    "CHECKPOINT_SCHEMA",
    "RECORD_SCHEMA",
    "CheckpointLocationError",
    "CheckpointStore",
    "EvidenceRef",
    "Loaded",
    "ResponseRecord",
    "atomic_write",
    "sha256_text",
]
