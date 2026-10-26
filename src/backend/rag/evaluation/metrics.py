"""Pure metric helpers: every rate carries its numerator, denominator and rule.

A rate whose denominator is zero is reported as ``value: null`` with an explicit
``not_measurable`` reason instead of 0 or 1, so an empty sample never reads as a
perfect or failed score.
"""

import hashlib
import json
import math
from collections.abc import Iterable, Sequence

type Json = bool | int | float | str | list[Json] | dict[str, Json] | None

DIGITS = 6


def strs(values: Iterable[object]) -> list[Json]:
    """String list typed as JSON (lists are invariant)."""
    return [str(value) for value in values]


def ratio(numerator: int, denominator: int, definition: str) -> dict[str, Json]:
    """One rate with its sample size; ``None`` when nothing was measurable."""
    report: dict[str, Json] = {
        "value": round(numerator / denominator, DIGITS) if denominator else None,
        "numerator": numerator,
        "denominator": denominator,
        "definition": definition,
    }
    if not denominator:
        report["not_measurable"] = "denominator is 0"
    return report


def precision_recall(tp: int, fp: int, fn: int, unit: str) -> dict[str, Json]:
    """Micro precision/recall over ``unit`` pairs with raw counts."""
    return {
        "unit": unit,
        "true_positive": tp,
        "false_positive": fp,
        "false_negative": fn,
        "precision": ratio(tp, tp + fp, f"correct predicted {unit} / predicted"),
        "recall": ratio(tp, tp + fn, f"correct predicted {unit} / gold"),
    }


def set_counts[T](predicted: Iterable[T], gold: Iterable[T]) -> tuple[int, int, int]:
    """(tp, fp, fn) of one predicted set against one gold set."""
    left, right = set(predicted), set(gold)
    return len(left & right), len(left - right), len(right - left)


def percentile(values: Sequence[float], q: float) -> float | None:
    """Nearest-rank percentile; ``None`` for an empty sample."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return round(ordered[rank - 1], 3)


def latency_summary(values: Sequence[float]) -> dict[str, Json]:
    """Sample size plus p50/p95/max in milliseconds."""
    return {
        "samples": len(values),
        "p50_ms": percentile(values, 50),
        "p95_ms": percentile(values, 95),
        "max_ms": None if not values else round(max(values), 3),
    }


def calibration(pairs: Sequence[tuple[float, bool]], bins: int = 5) -> dict[str, Json]:
    """Brier score and expected calibration error over equal-width bins."""
    if not pairs:
        return {
            "samples": 0,
            "brier": None,
            "ece": None,
            "not_measurable": "no scored confidence values",
        }
    brier = sum((confidence - correct) ** 2 for confidence, correct in pairs)
    table: list[Json] = []
    ece = 0.0
    for index in range(bins):
        low, high = index / bins, (index + 1) / bins
        members = [
            (c, ok)
            for c, ok in pairs
            if low <= c < high or (index == bins - 1 and c == high)
        ]
        if not members:
            continue
        mean_confidence = sum(c for c, _ in members) / len(members)
        accuracy = sum(ok for _, ok in members) / len(members)
        ece += len(members) / len(pairs) * abs(mean_confidence - accuracy)
        table.append(
            {
                "range": [low, high],
                "samples": len(members),
                "mean_confidence": round(mean_confidence, DIGITS),
                "accuracy": round(accuracy, DIGITS),
            }
        )
    return {
        "samples": len(pairs),
        "brier": round(brier / len(pairs), DIGITS),
        "ece": round(ece, DIGITS),
        "bins": table,
    }


def canonical(report: Json) -> str:
    """Stable JSON text used for digests and reproducibility checks."""
    return json.dumps(report, sort_keys=True, ensure_ascii=False, indent=2)


def digest(report: Json) -> str:
    """SHA-256 of the canonical JSON rendering."""
    return "sha256:" + hashlib.sha256(canonical(report).encode()).hexdigest()
