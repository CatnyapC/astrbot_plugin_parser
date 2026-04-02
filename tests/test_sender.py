import sys
from pathlib import Path
from types import SimpleNamespace

from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.message.components import Image, Nodes, Plain, Video

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.data import Author, ParseResult, Platform
from core.debounce import Debouncer
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


def test_send_parse_result_returns_false_when_send_fails():
    sender = build_sender()
    event = DummyEvent()

    async def fake_send(_chain):
        raise RuntimeError("send failed")

    event.send = fake_send
    sender._resolve_groups = lambda _result: []
    result = ParseResult(
        platform=Platform(name="twitter", display_name="推特"),
        text="正文",
    )

    ok = __import__("asyncio").run(sender.send_parse_result(event, result))

    assert ok is False
    assert event.sent == []


def test_send_chain_retries_once_after_timeout():
    sender = build_sender()
    event = DummyEvent()
    attempts = 0

    sender._send_timeout_seconds = lambda: 0.01

    async def fake_send(_chain):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            await __import__("asyncio").sleep(0.02)

    event.send = fake_send

    __import__("asyncio").run(sender._send_chain(event, [Plain("hello")]))

    assert attempts == 2


def test_send_parse_result_skips_text_fallback_after_group_failure():
    sender = build_sender()
    event = DummyEvent()
    sender._resolve_groups = lambda _result: [object()]

    async def fake_send_group(_event, _result, _group):
        return False

    sender._send_group = fake_send_group
    result = ParseResult(
        platform=Platform(name="twitter", display_name="推特"),
        text="正文",
    )

    ok = __import__("asyncio").run(sender.send_parse_result(event, result))

    assert ok is False
    assert event.sent == []


def test_send_group_retries_with_normalized_image_after_send_failure():
    sender = build_sender()
    original = Image("file:////tmp/original.jpg")
    retried = Image("file:////tmp/retried.jpg")
    attempts: list[MessageChain] = []

    async def fake_send(chain):
        attempts.append(chain)
        if len(attempts) == 1:
            raise RuntimeError("send failed")

    async def fake_build_segments(_result, _plan):
        return [original]

    async def fake_normalize(_segs):
        return [retried]

    event = DummyEvent()
    event.send = fake_send
    sender._build_send_plan = lambda *_args, **_kwargs: {
        "preview_card": False,
        "force_merge": False,
    }
    sender._build_segments = fake_build_segments
    sender._normalize_image_segments_for_retry = fake_normalize
    group = SimpleNamespace(contents=[], force_merge=None, render_card=None)

    ok = __import__("asyncio").run(sender._send_group(event, object(), group))

    assert ok is True
    assert len(attempts) == 2
    assert attempts[0].chain[0] is original
    assert attempts[1].chain[0] is retried


def test_send_group_retries_with_perturbed_png_after_second_failure():
    sender = build_sender()
    original = Image("file:////tmp/original.jpg")
    retried = Image("file:////tmp/retried.jpg")
    perturbed = Image("file:////tmp/perturbed.png")
    attempts: list[MessageChain] = []

    async def fake_send(chain):
        attempts.append(chain)
        if len(attempts) < 3:
            raise RuntimeError("send failed")

    async def fake_build_segments(_result, _plan):
        return [original]

    async def fake_normalize(_segs):
        return [retried]

    async def fake_perturb(_segs):
        return [perturbed]

    event = DummyEvent()
    event.send = fake_send
    sender._build_send_plan = lambda *_args, **_kwargs: {
        "preview_card": False,
        "force_merge": False,
    }
    sender._build_segments = fake_build_segments
    sender._normalize_image_segments_for_retry = fake_normalize
    sender._perturb_image_segments_for_retry = fake_perturb
    group = SimpleNamespace(contents=[], force_merge=None, render_card=None)

    ok = __import__("asyncio").run(sender._send_group(event, object(), group))

    assert ok is True
    assert len(attempts) == 3
    assert attempts[0].chain[0] is original
    assert attempts[1].chain[0] is retried
    assert attempts[2].chain[0] is perturbed


def test_resource_debounce_marks_only_after_success():
    debouncer = Debouncer(SimpleNamespace(debounce_interval=60))
    session = "group:123"
    resource_id = "abc123"

    assert debouncer.check_resource(session, resource_id) is False
    assert debouncer.check_resource(session, resource_id) is False

    debouncer.mark_resource(session, resource_id)

    assert debouncer.check_resource(session, resource_id) is True


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


def test_build_text_fallback_skips_header_only_result():
    result = ParseResult(
        platform=Platform(name="twitter", display_name="推特"),
        author=Author(name="无用户名"),
    )

    assert MessageSender._build_text_fallback(result) == []


def test_build_text_fallback_keeps_header_when_text_present():
    result = ParseResult(
        platform=Platform(name="twitter", display_name="推特"),
        author=Author(name="alice"),
        text="正文",
    )

    segs = MessageSender._build_text_fallback(result)

    assert len(segs) == 1
    assert isinstance(segs[0], Plain)
    assert segs[0].text == "推特 @alice\n正文"


def test_to_file_uri_survives_astrbot_file_trim(tmp_path):
    sender = build_sender()
    image_path = tmp_path / "card.png"
    image_path.write_bytes(b"fake-card")

    uri = sender._to_file_uri(image_path)

    assert uri == f"file:////{image_path.as_posix().lstrip('/')}"
    assert uri.startswith("file:////")
    assert uri[8:] == image_path.as_posix()


def test_to_file_uri_resolves_absolute_symlink(tmp_path):
    sender = build_sender()
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    linked_dir = tmp_path / "linked"
    linked_dir.symlink_to(real_dir, target_is_directory=True)
    image_path = real_dir / "card.png"
    image_path.write_bytes(b"fake-card")

    uri = sender._to_file_uri(linked_dir / "card.png")

    assert uri == f"file:////{image_path.as_posix().lstrip('/')}"
    assert uri[8:] == image_path.as_posix()


def test_perturb_image_for_retry_produces_distinct_outputs(tmp_path):
    sender = build_sender()
    image_path = tmp_path / "card.png"
    image_path.write_bytes(
        bytes.fromhex(
            "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753"
            "de0000000c49444154789c63f8ffff3f0005fe02fea757a90000000049454e44ae426082"
        )
    )

    first = __import__("asyncio").run(sender._perturb_image_for_retry(image_path))
    second = __import__("asyncio").run(sender._perturb_image_for_retry(image_path))

    assert first is not None
    assert second is not None
    assert first != second
    assert first.read_bytes() != second.read_bytes()
