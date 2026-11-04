"""Build ``fixtures/rag/search-selection/freshness-updates-v1`` (synthetic).

Usage: ``uv run python scripts/rag/build_freshness_fixture.py [--check]``.
Same deterministic builder as the search-selection fixture; the manifest
carries the fixed query clock so every run scores freshness at one instant.
Mechanism test only: fictional storylines, author labels, no review.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from build_search_fixture import build  # noqa: E402
from freshness_fixture_corpus import ARTICLES, QUERY_CLOCK, QUESTIONS  # noqa: E402

OUT = Path(__file__).parents[2] / "fixtures/rag/search-selection/freshness-updates-v1"
META: dict[str, object] = {
    "dataset_id": "eventlens-freshness-updates",
    "version": "freshness-updates.v1",
    "labels": "author-labeled synthetic update scenarios; mechanism test only",
    "query_clock": QUERY_CLOCK,
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    files = build(ARTICLES, QUESTIONS, META)
    if args.check:
        stale = [
            name
            for name, body in files.items()
            if not (OUT / name).is_file()
            or (OUT / name).read_text(encoding="utf-8") != body
        ]
        if stale:
            print(f"freshness fixture out of date: {stale}", file=sys.stderr)  # noqa: T201
            return 1
        return 0
    OUT.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        _ = (OUT / name).write_text(body, encoding="utf-8")
    print(f"wrote {sorted(files)} to {OUT}")  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
