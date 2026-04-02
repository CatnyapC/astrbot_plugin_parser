import sys
from pathlib import Path
from types import SimpleNamespace

from astrbot.core.message.components import Image, Plain, Video

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.sender import MessageSender


class DummyEvent:
    @staticmethod
    def get_self_id() -> str:
        return "10000"


def build_sender() -> MessageSender:
    return MessageSender(config=SimpleNamespace(), renderer=SimpleNamespace())


def test_split_segments_for_send_splits_multiple_media():
    sender = build_sender()

    segs = [
        Plain("hello"),
        Image("file:///tmp/test-1.png"),
        Image("file:///tmp/test-2.png"),
    ]

    batches = sender._split_segments_for_send(segs)

    assert batches == [
        [Plain("hello")],
        [Image("file:///tmp/test-1.png")],
        [Image("file:///tmp/test-2.png")],
    ]


def test_split_segments_for_send_keeps_single_media_together():
    sender = build_sender()

    segs = [Plain("hello"), Video("file:///tmp/test.mp4")]

    batches = sender._split_segments_for_send(segs)

    assert batches == [segs]
