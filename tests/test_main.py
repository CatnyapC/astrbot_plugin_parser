import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_parser.main import ParserPlugin


class DummyEvent:
    def __init__(self, *, timeout_requeue: bool):
        self.timeout_requeue = timeout_requeue

    def get_extra(self, key: str, default=""):
        if key == "_router_timeout_requeue":
            return self.timeout_requeue
        return default


def test_should_skip_router_requeue_true():
    assert ParserPlugin._should_skip_router_requeue(
        DummyEvent(timeout_requeue=True)
    )


def test_should_skip_router_requeue_false():
    assert not ParserPlugin._should_skip_router_requeue(
        DummyEvent(timeout_requeue=False)
    )
