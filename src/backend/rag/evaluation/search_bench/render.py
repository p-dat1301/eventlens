"""Markdown summary of a ``search-selection-report.v1`` (numbers only)."""

from collections.abc import Mapping, Sequence
from typing import cast


def _f(value: object, digits: int = 3) -> str:
    return "—" if not isinstance(value, int | float) else f"{value:.{digits}f}"


def _ms(value: object) -> str:
    return "—" if not isinstance(value, int | float) else f"{value:.0f}"


def summary_table(report: Mapping[str, object], split: str) -> str:
    """One row per system for ``split``."""
    lines = [
        (
            f"| System ({split}) | n | R@5 | R@10 | MRR | nDCG@10 | Span@5 "
            "| Span@10 | NoAns AUROC | p50 ms | p95 ms | errors |"
        ),
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for system in cast("Sequence[Mapping[str, object]]", report["systems"]):
        summaries = cast("Mapping[str, Mapping[str, object]]", system["summary"])
        s = summaries[split]
        recall = cast("Mapping[str, object]", s["recall"])
        span = cast("Mapping[str, Mapping[str, object]]", s["span_hit"])
        latency = cast("Mapping[str, object]", s["latency_ms"])
        errors = cast("Mapping[str, int]", s["excluded_errors"])
        cells = (
            f"`{system['name']}`",
            f"{s['scored']}/{s['answerable']}",
            _f(recall["5"]),
            _f(recall["10"]),
            _f(s["mrr"]),
            _f(s["ndcg10"]),
            f"{span['5']['hits']}/{span['5']['of']}",
            f"{span['10']['hits']}/{span['10']['of']}",
            f"{_f(s['no_answer_top1_auroc'], 2)} ({s['unanswerable']})",
            _ms(latency["p50"]),
            _ms(latency["p95"]),
            str(sum(errors.values())),
        )
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def comparison_table(report: Mapping[str, object]) -> str:
    """Paired bootstrap deltas (left - right) with 95% CI."""
    lines = [
        (
            "| Split | Left - Right | n | ΔnDCG@10 [95% CI] | ΔR@10 [95% CI] "
            "| ΔMRR [95% CI] | W/L/T (nDCG) |"
        ),
        "|---|---|---:|---|---|---|---|",
    ]
    for row in cast("Sequence[Mapping[str, object]]", report["comparisons"]):
        cells: list[str] = []
        for metric in ("ndcg10", "recall10", "mrr"):
            m = cast("Mapping[str, object]", row[metric])
            low, high = cast("Sequence[float]", m["ci95"])
            mark = " **clear**" if m["clear"] else ""
            cells.append(f"{_f(m['mean_delta'])} [{_f(low)}, {_f(high)}]{mark}")
        nd = cast("Mapping[str, object]", row["ndcg10"])
        pair = f"`{row['left']}` - `{row['right']}`"
        tally = f"{nd['wins']}/{nd['losses']}/{nd['ties']}"
        lines.append(
            "| "
            + " | ".join((str(row["split"]), pair, str(row["n"]), *cells, tally))
            + " |"
        )
    return "\n".join(lines)


def index_table(report: Mapping[str, object]) -> str:
    """Index build time and size per engine."""
    lines = [
        "| Index | chunks | build s | size MiB |",
        "|---|---:|---:|---:|",
    ]
    stats = cast("Mapping[str, Mapping[str, object]]", report["index_stats"])
    for name, s in sorted(stats.items()):
        seconds = (
            cast("float", s.get("index_seconds", 0.0))
            + cast("float", s.get("embed_seconds", 0.0))
            + cast("float", s.get("load_seconds", 0.0))
        )
        size = cast("int", s.get("size_bytes", 0)) / 1_048_576
        lines.append(f"| `{name}` | {s['chunks']} | {seconds:.1f} | {size:.2f} |")
    return "\n".join(lines)


def breakdown_table(report: Mapping[str, object], dimension: str) -> str:
    """nDCG@10 per value of ``dimension`` (test split), one column per system."""
    systems = cast("Sequence[Mapping[str, object]]", report["systems"])
    values: set[str] = set()
    for system in systems:
        groups = cast("Mapping[str, Mapping[str, object]]", system["breakdown_test"])
        values.update(groups.get(dimension, {}).keys())
    header = "| System | " + " | ".join(sorted(values)) + " |"
    lines = [header, "|---|" + "---:|" * len(values)]
    for system in systems:
        groups = cast(
            "Mapping[str, Mapping[str, Mapping[str, object]]]", system["breakdown_test"]
        )
        empty: Mapping[str, Mapping[str, object]] = {}
        row = groups.get(dimension, empty)
        cells: list[str] = []
        for value in sorted(values):
            cell = row.get(value)
            cells.append(
                "—" if cell is None else f"{_f(cell['ndcg10'], 2)} (n={cell['scored']})"
            )
        lines.append(f"| `{system['name']}` | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def render(report: Mapping[str, object]) -> str:
    """Full markdown summary."""
    run = cast("Mapping[str, object]", report["run"])
    corpus = cast("Mapping[str, object]", report["corpus"])
    questions = cast("Mapping[str, object]", report["questions"])
    parts = [
        "# Search selection benchmark — generated summary",
        "",
        (
            f"- Corpus `{corpus['export_id']}` digest `{corpus['digest']}`: "
            f"{corpus['documents']} documents, {corpus['chunks']} chunks "
            f"(`{corpus['chunker_version']}`)"
        ),
        (
            f"- Questions: {questions['total']} {questions['label_status']}; "
            f"subsets {questions['subset_sizes']}"
        ),
        (
            f"- Repeats {run['repeats']}, query clock {run['query_clock']}, "
            f"policy `{run['retrieval_policy']}`, hybrid selected on dev: "
            f"`{run['selected_on_dev']}`"
        ),
        "",
        "## Test split",
        summary_table(report, "test"),
        "",
        "## Dev split (used for selection)",
        summary_table(report, "dev"),
        "",
        "## Paired comparisons",
        comparison_table(report),
        "",
        "## Index cost",
        index_table(report),
        "",
    ]
    for dimension in ("category", "source_domain", "parse_version", "length"):
        parts.extend(
            [
                f"## nDCG@10 by {dimension} (test)",
                breakdown_table(report, dimension),
                "",
            ]
        )
    return "\n".join(parts)
