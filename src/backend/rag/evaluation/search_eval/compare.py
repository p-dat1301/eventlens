"""Paired comparison of two configurations over the same questions (pure).

Per-question differences ``candidate - baseline`` on one split; a seeded
percentile bootstrap over questions gives the 95 % interval, and win/loss/tie
counts show how many questions actually moved. With candidate labels and a
few dozen questions an interval that contains 0 means *no evidence of a
difference*, not evidence of equality.
"""

import random
from collections.abc import Mapping, Sequence
from typing import Final

from backend.rag.evaluation.metrics import Json

BOOTSTRAP_SAMPLES: Final = 10_000
BOOTSTRAP_SEED: Final = 20261028
METRICS: Final = ("ndcg@10", "recall@10", "rr", "recall@1")
# Selection (b_) / payload (c_) metrics; present only for pipeline configs.
EVIDENCE_METRICS: Final = (
    "b_required_cov",
    "b_gold_frac",
    "c_required_cov",
    "c_all_required",
    "c_gold_frac",
)
DIGITS: Final = 4


def _values(rows: Sequence[Mapping[str, Json]], metric: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for row in rows:
        value = row.get(metric)
        if isinstance(value, int | float) and not isinstance(value, bool):
            out[str(row["question_id"])] = float(value)
    return out


def paired(  # noqa: PLR0913 - two keyword-only bootstrap knobs
    baseline: Sequence[Mapping[str, Json]],
    candidate: Sequence[Mapping[str, Json]],
    questions: set[str],
    metric: str,
    *,
    samples: int = BOOTSTRAP_SAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> dict[str, Json]:
    """Mean difference, bootstrap CI and W/L/T for one metric."""
    left, right = _values(baseline, metric), _values(candidate, metric)
    ids = sorted(questions & set(left) & set(right))
    diffs = [right[q] - left[q] for q in ids]
    if not diffs:
        return {"metric": metric, "n": 0}
    rng = random.Random(seed)  # noqa: S311 - reproducible statistics, not crypto
    means = sorted(
        sum(rng.choice(diffs) for _ in diffs) / len(diffs) for _ in range(samples)
    )
    low = means[int(0.025 * samples)]
    high = means[min(int(0.975 * samples), samples - 1)]
    return {
        "metric": metric,
        "n": len(diffs),
        "mean_diff": round(sum(diffs) / len(diffs), DIGITS),
        "ci95": [round(low, DIGITS), round(high, DIGITS)],
        "wins": sum(d > 0 for d in diffs),
        "losses": sum(d < 0 for d in diffs),
        "ties": sum(d == 0 for d in diffs),
        "excludes_zero": low > 0 or high < 0,
        "bootstrap": {"samples": samples, "seed": seed},
    }


def compare(
    report: Mapping[str, Json],
    pairs: Sequence[tuple[str, str]],
    split_of: Mapping[str, str],
    split: str,
) -> dict[str, Json]:
    """Every requested ``(baseline, candidate)`` pair on one split."""
    configs = report["configurations"]
    assert isinstance(configs, dict)  # noqa: S101
    questions = {q for q, s in split_of.items() if s == split}
    out: dict[str, Json] = {}
    for base, cand in pairs:
        left, right = configs.get(base), configs.get(cand)
        if not isinstance(left, dict) or not isinstance(right, dict):
            out[f"{cand} - {base}"] = {"error": "configuration missing"}
            continue
        rows_left, rows_right = left["per_question"], right["per_question"]
        assert isinstance(rows_left, list)  # noqa: S101
        assert isinstance(rows_right, list)  # noqa: S101
        left_rows = [r for r in rows_left if isinstance(r, dict)]
        right_rows = [r for r in rows_right if isinstance(r, dict)]
        wanted = METRICS + tuple(
            m
            for m in EVIDENCE_METRICS
            if any(m in r for r in left_rows) and any(m in r for r in right_rows)
        )
        out[f"{cand} - {base}"] = {
            metric: paired(left_rows, right_rows, questions, metric)
            for metric in wanted
        }
    return {"split": split, "pairs": out}
