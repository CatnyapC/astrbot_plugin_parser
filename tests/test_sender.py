import sys
from pathlib import Path
from types import SimpleNamespace

from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.message.components import Image, Nodes, Plain, Video

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.sender import MessageSender


class DummyEvent:
    def __init__(self):
        self.sent: list[MessageChain] = []

    @staticmethod
    def get_self_id() -> str:
        return "10000"

    @staticmethod
    def get_group_id() -> str:
        return "20000"

    @staticmethod
    def get_extra(_key: str, default=""):
        return default

    @staticmethod
    def chain_result(chain) -> MessageChain:
        return MessageChain(list(chain))

    async def send(self, chain: MessageChain) -> None:
        self.sent.append(chain)


class DummyArchive:
    def __init__(self):
        self.calls: list[dict] = []

    async def archive_sent_chain(self, **kwargs):
        self.calls.append(kwargs)


def build_sender() -> MessageSender:
    return MessageSender(config=SimpleNamespace(), renderer=SimpleNamespace())


def test_merge_segments_wraps_plain_and_image_into_nodes():
    sender = build_sender()

    segs = [Plain("hello"), Image("file:///tmp/test.png")]

    merged = sender._merge_segments_if_needed(DummyEvent(), segs, force_merge=True)

    assert len(merged) == 1
    assert isinstance(merged[0], Nodes)
    assert len(merged[0].nodes) == 2
    assert all(node.name == "狐米" for node in merged[0].nodes)


def test_merge_segments_wraps_video_into_nodes_too():
    sender = build_sender()

    segs = [Plain("hello"), Video("file:///tmp/test.mp4")]

    merged = sender._merge_segments_if_needed(DummyEvent(), segs, force_merge=True)

    assert len(merged) == 1
    assert isinstance(merged[0], Nodes)
    assert len(merged[0].nodes) == 2


def test_merge_segments_uses_configured_sender_name():
    sender = MessageSender(
        config=SimpleNamespace(merge_sender_name="自定义名字"),
        renderer=SimpleNamespace(),
    )

    merged = sender._merge_segments_if_needed(
        DummyEvent(),
        [Plain("hello")],
        force_merge=True,
    )

    assert len(merged) == 1
    assert isinstance(merged[0], Nodes)
    assert merged[0].nodes[0].name == "自定义名字"


def test_send_group_archives_sent_chain():
    archive = DummyArchive()
    context = SimpleNamespace(
        get_registered_star=lambda name: (
            SimpleNamespace(star_cls=archive)
            if name == "astrbot_plugin_thread_archive"
            else None
        )
    )
    sender = MessageSender(
        config=SimpleNamespace(),
        renderer=SimpleNamespace(),
        context=context,
    )

    async def fake_build_segments(_result, _plan):
        return [Plain("hello")]

    sender._build_send_plan = lambda *_args, **_kwargs: {
        "preview_card": False,
        "force_merge": True,
    }
    sender._build_segments = fake_build_segments
    event = DummyEvent()
    group = SimpleNamespace(contents=[], force_merge=None, render_card=None)

    ok = __import__("asyncio").run(sender._send_group(event, object(), group))

    assert ok is True
    assert len(event.sent) == 1
    assert len(archive.calls) == 1
    assert archive.calls[0]["source"] == "after_message_sent"
    assert archive.calls[0]["raw_json_extra"]["origin_source"] == "parser_send_group"
    assert archive.calls[0]["include_current_event_raw_id"] is True
    assert archive.calls[0]["use_current_event_raw_id_as_message_raw_id"] is False
    assert isinstance(archive.calls[0]["chain"][0], Nodes)


def test_send_preview_card_archives_sent_chain(tmp_path):
    archive = DummyArchive()
    context = SimpleNamespace(
        get_registered_star=lambda name: (
            SimpleNamespace(star_cls=archive)
            if name == "astrbot_plugin_thread_archive"
            else None
        )
    )
    image_path = tmp_path / "card.png"
    image_path.write_bytes(b"fake-card")
    sender = MessageSender(
        config=SimpleNamespace(),
        renderer=SimpleNamespace(render_card=lambda _result: image_path),
        context=context,
    )

    async def fake_render_card(_result):
        return image_path

    sender.renderer.render_card = fake_render_card
    event = DummyEvent()

    __import__("asyncio").run(
        sender._send_preview_card(event, object(), {"preview_card": True})
    )

    assert len(event.sent) == 1
    assert len(archive.calls) == 1
    assert archive.calls[0]["source"] == "after_message_sent"
    assert archive.calls[0]["raw_json_extra"]["origin_source"] == "parser_preview_card"
    assert isinstance(archive.calls[0]["chain"][0], Image)


def test_to_file_uri_survives_astrbot_file_trim(tmp_path):
    sender = build_sender()
    image_path = tmp_path / "card.png"
    image_path.write_bytes(b"fake-card")

    uri = sender._to_file_uri(image_path)

    assert uri == f"file:////{image_path.as_posix().lstrip('/')}"
    assert uri.startswith("file:////")
    assert uri[8:] == image_path.as_posix()
