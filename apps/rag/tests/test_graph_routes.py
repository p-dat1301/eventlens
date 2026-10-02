from concurrent.futures import ThreadPoolExecutor

from eventlens_rag.graph.runner import BaselineGraphRunner


def test_concurrent_runs_keep_distinct_state() -> None:
    runner = BaselineGraphRunner()
    messages = ("first", "second", "third")
    with ThreadPoolExecutor(max_workers=3) as executor:
        results = tuple(executor.map(runner.invoke, messages))
    assert tuple(result.message for result in results) == messages
    assert len({result.request_id for result in results}) == 3
