import sys
from pathlib import Path
from types import SimpleNamespace

from astrbot.core.message.components import Image, Nodes, Plain, Video

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.sender import MessageSender


class DummyEvent:
    @staticmethod
    def get_self_id() -> str:
        return "10000"


def build_sender() -> MessageSender:
    return MessageSender(config=SimpleNamespace(), renderer=SimpleNamespace())


def test_merge_segments_wraps_plain_and_image_into_nodes():
    sender = build_sender()

    segs = [Plain("hello"), Image("file:///tmp/test.png")]

    merged = sender._merge_segments_if_needed(DummyEvent(), segs, force_merge=True)

    assert len(merged) == 1
    assert isinstance(merged[0], Nodes)
    assert len(merged[0].nodes) == 2


def test_merge_segments_wraps_video_into_nodes_too():
    sender = build_sender()

    segs = [Plain("hello"), Video("file:///tmp/test.mp4")]

    merged = sender._merge_segments_if_needed(DummyEvent(), segs, force_merge=True)

    assert len(merged) == 1
    assert isinstance(merged[0], Nodes)
    assert len(merged[0].nodes) == 2
