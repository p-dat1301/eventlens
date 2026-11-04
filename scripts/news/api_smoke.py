"""Reproducible backend smoke over real HTTP (crud-api + rag-api + worker).

Steps: list -> filter by topic/location (IDs taken from the facets/taxonomy
responses, never hardcoded) -> detail + summary -> session -> conversation
-> submit question -> poll run -> open citation. Writes a text-free JSON
report (status codes, counts, IDs, states) to ``--report``.

    .venv/bin/python scripts/news/api_smoke.py \\
        --crud http://127.0.0.1:8000 --rag http://127.0.0.1:8001 \\
        --report .omo/evidence/news-backend/final-gates/api-smoke.json

Runs need a worker: ``eventlens-rag-worker serve`` (or ``--run-worker`` to
call ``eventlens-rag-worker runs --max-jobs 1`` between polls, which needs
EVENTLENS_RAG_DATABASE_URL). Exit 0 = every step passed, 1 = a step failed.
"""

import argparse
import json
import subprocess  # noqa: S404 - optional local worker invocation
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

Json = dict[str, object]


@dataclass(slots=True)
class Client:
    """Minimal JSON client; keeps the session token for rag-api calls."""

    report: list[Json] = field(default_factory=list[Json])
    token: str | None = None

    def call(
        self, step: str, method: str, url: str, body: Json | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, Json]:
        """One request; records step, method, path and status only."""
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(url, data=data, method=method)  # noqa: S310
        request.add_header("Content-Type", "application/json")
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as error:
            status, raw = error.code, error.read()
        payload = cast("Json", json.loads(raw)) if raw else {}
        self.report.append(
            {"step": step, "method": method, "path": urllib.parse.urlsplit(url).path,
             "status": status}
        )
        return status, payload


def _require(condition: object, message: str) -> None:
    if not condition:
        raise SystemExit(f"smoke failed: {message}")


def main() -> int:  # noqa: C901, PLR0915 - linear script
    """Run the seven steps."""
    parser = argparse.ArgumentParser()
    _ = parser.add_argument("--crud", default="http://127.0.0.1:8000")
    _ = parser.add_argument("--rag", default="http://127.0.0.1:8001")
    _ = parser.add_argument("--report", type=Path)
    _ = parser.add_argument("--run-worker", action="store_true")
    _ = parser.add_argument("--question", default="Có những thay đổi nào đáng chú ý?")
    args = parser.parse_args()
    crud, rag = cast("str", args.crud), cast("str", args.rag)
    client = Client()
    summary: Json = {}
    try:
        status, page = client.call("1.list", "GET", f"{crud}/api/v1/articles?limit=5")
        _require(status == 200, f"list returned {status}")
        items = cast("list[Json]", page["items"])
        _require(items, "catalog is empty; import articles first")
        summary["listed"] = len(items)

        _, facets = client.call("2.facets", "GET", f"{crud}/api/v1/article-facets")
        topics = cast("list[Json]", facets.get("topics", []))
        locations = cast("list[Json]", facets.get("locations", []))
        if not topics:  # no labels yet: still exercise the filter with real IDs
            _, listed_topics = client.call("2.topics", "GET", f"{crud}/api/v1/topics")
            topics = cast("list[Json]", listed_topics["items"])
        if not locations:
            _, listed = client.call("2.locations", "GET", f"{crud}/api/v1/locations?q=ho%20chi%20minh")
            locations = cast("list[Json]", listed["items"])
        _require(topics and locations, "taxonomy endpoints returned nothing")
        summary["facet_topics"] = len(cast("list[Json]", facets.get("topics", [])))
        summary["facet_locations"] = len(cast("list[Json]", facets.get("locations", [])))
        summary["topics_unclassified"] = facets.get("topics_unclassified")
        query: list[tuple[str, str]] = [
            ("limit", "5"),
            ("topic_ids", str(topics[0]["id"])),
            ("location_ids", str(locations[0]["id"])),
        ]
        status, filtered = client.call(
            "2.filter", "GET",
            f"{crud}/api/v1/articles?{urllib.parse.urlencode(query)}",
        )
        _require(status == 200, f"filtered list returned {status}")
        summary["filters_used"] = [k for k, _ in query if k != "limit"]
        summary["filtered"] = len(cast("list[Json]", filtered["items"]))

        article_id = str(items[0]["article_id"])
        status, detail = client.call("3.detail", "GET", f"{crud}/api/v1/articles/{article_id}")
        _require(status == 200, f"detail returned {status}")
        status, summ = client.call(
            "3.summary", "GET", f"{crud}/api/v1/articles/{article_id}/summary"
        )
        _require(status == 200, f"summary returned {status}")
        summary["summary_status"] = summ["status"]
        summary["content_policy"] = detail["content_policy"]

        status, session = client.call("4.session", "POST", f"{rag}/api/v1/sessions")
        _require(status == 201, f"session returned {status}")
        client.token = str(session["session_token"])
        scope: Json = {"type": "articles", "articles": [{"article_id": article_id}]}
        status, conversation = client.call(
            "4.conversation", "POST", f"{rag}/api/v1/conversations", {"scope": scope}
        )
        _require(status == 201, f"conversation returned {status}")

        conversation_id = str(conversation["conversation_id"])
        key = f"smoke-{int(time.time())}"
        status, accepted = client.call(
            "5.submit", "POST", f"{rag}/api/v1/conversations/{conversation_id}/runs",
            {"question": args.question}, {"Idempotency-Key": key},
        )
        _require(status == 202, f"submit returned {status}: {accepted.get('error')}")
        run_url = f"{rag}{accepted['status_url']}"

        run: Json = {}
        for _ in range(60):
            if args.run_worker:
                _ = subprocess.run(  # noqa: S603
                    ["eventlens-rag-worker", "runs", "--max-jobs", "1"],  # noqa: S607
                    check=False, capture_output=True,
                )
            _, run = client.call("6.poll", "GET", run_url)
            if run.get("status") in {"completed", "failed", "cancelled"}:
                break
            time.sleep(1)
        summary["run_status"] = run.get("status")
        summary["run_outcome"] = run.get("outcome")
        summary["run_error"] = run.get("error")
        _require(run.get("status") == "completed", f"run ended {run.get('status')}")

        citations = cast("list[Json]", run.get("citations", []))
        summary["citations"] = len(citations)
        if citations:
            status, ref = client.call(
                "7.source-ref", "POST", f"{rag}{citations[0]['source_ref_url']}"
            )
            _require(status == 201, f"source-ref returned {status}")
            status, viewer = client.call("7.viewer", "GET", f"{rag}{ref['viewer_url']}")
            _require(status == 200, f"viewer returned {status}")
            span = cast("Json", viewer["span"])
            summary["viewer_span_matches"] = span["excerpt"] == cast(
                "Json", citations[0]["span"]
            )["excerpt"]
        ok = 0
    except SystemExit as failure:
        summary["failure"] = str(failure)
        ok = 1
    report = {"ok": ok == 0, "summary": summary, "requests": client.report}
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        _ = args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(report["summary"], ensure_ascii=False))  # noqa: T201
    return ok


if __name__ == "__main__":
    sys.exit(main())
