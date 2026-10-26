"""Loopback rag-api smoke for gate-g3: health, briefing, citation inspection.

Usage: ``api_smoke.py health|briefing http://127.0.0.1:PORT``. Stdlib only;
refuses any non-loopback base URL. Exit 0 on success, 1 on any failure.
"""

import json
import sys
import time
import urllib.error
import urllib.request
from typing import cast
from urllib.parse import quote, urlsplit

QUESTION = "When did East Pier close and reopen?"


def _call(method: str, url: str, body: object | None = None) -> tuple[int, object]:
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(  # noqa: S310 - loopback checked in main
        url, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"null")


def health(base: str) -> None:
    deadline = time.monotonic() + 60
    while True:
        try:
            status, _ = _call("GET", f"{base}/health")
            if status == 200:
                print("health 200")
                return
        except OSError:
            pass
        if time.monotonic() > deadline:
            raise SystemExit("rag-api did not become healthy within 60 s")
        time.sleep(0.5)


def briefing(base: str) -> None:
    status, payload = _call(
        "POST",
        f"{base}/briefing",
        {"schema_version": "briefing_request.v1", "query": QUESTION},
    )
    answer = cast("dict[str, object]", payload)
    citations = cast("list[dict[str, object]]", answer.get("citations") or [])
    print(f"briefing {status} outcome={answer.get('outcome')} "
          f"mode={answer.get('retrieval_mode')} citations={len(citations)}")
    if status != 200 or answer.get("outcome") != "answered" or not citations:
        raise SystemExit("briefing did not return a cited answer")
    ref = str(citations[0]["source_ref"])
    status, payload = _call("GET", f"{base}/briefing/sources/{quote(ref, safe='')}")
    source = cast("dict[str, object]", payload)
    span = cast("dict[str, object]", source.get("span") or {})
    content = str(source.get("normalized_content", ""))
    start, end = int(str(span.get("start_offset"))), int(str(span.get("end_offset")))
    exact = content[start:end] == span.get("excerpt")
    print(f"source {status} span=[{start},{end}) excerpt_matches={exact}")
    if status != 200 or not exact:
        raise SystemExit("cited source did not resolve to the exact span")


def main() -> None:
    action, base = sys.argv[1], sys.argv[2].rstrip("/")
    if urlsplit(base).hostname not in {"127.0.0.1", "localhost"}:
        raise SystemExit("api_smoke only talks to loopback")
    {"health": health, "briefing": briefing}[action](base)


if __name__ == "__main__":
    main()
