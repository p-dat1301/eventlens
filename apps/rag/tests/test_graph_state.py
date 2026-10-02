import pytest

from eventlens_rag.graph.runner import BaselineGraphRunner


def test_compiled_state_graph_validates_input_and_isolates_invocations() -> None:
    # Given
    runner = BaselineGraphRunner()

    # When
    first = runner.invoke("first request")
    second = runner.invoke("second request")

    # Then
    assert first.request_id != second.request_id
    assert first.message == "first request"
    assert second.message == "second request"


def test_compiled_state_graph_rejects_blank_input() -> None:
    # Given
    runner = BaselineGraphRunner()

    # When / Then
    with pytest.raises(ValueError, match="message"):
        _ = runner.invoke(" ")
