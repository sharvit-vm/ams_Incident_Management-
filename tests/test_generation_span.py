from contextlib import contextmanager
from unittest.mock import Mock, patch

from observability.agent_trace import generation_span


def test_exception_inside_generation_is_preserved():
    with patch("observability.agent_trace._get_langfuse_client", return_value=None):
        try:
            with generation_span(name="test"):
                raise ValueError("original failure")
        except ValueError as exc:
            assert str(exc) == "original failure"
        else:
            raise AssertionError("model error was swallowed")


def test_exception_inside_open_generation_is_preserved():
    @contextmanager
    def observation():
        yield Mock()

    client = Mock()
    client.start_as_current_observation.return_value = observation()
    with patch("observability.agent_trace._get_langfuse_client", return_value=client), patch("observability.agent_trace.langfuse_flush"):
        try:
            with generation_span(name="test"):
                raise ValueError("original failure")
        except ValueError as exc:
            assert str(exc) == "original failure"
        else:
            raise AssertionError("model error was swallowed")
