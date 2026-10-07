import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

from PIL import Image as PILImage

from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.data import ImageContent, SendGroup
from core.image_stitch import (
    ImageStitchCacheIndex,
    ImageStitchCommandService,
    parse_parserimg_stitch_command,
)
from core.sender import MessageSender


class DummyEvent:
    def __init__(self, text: str = "/parserimg stitch", group_id: str = "g1"):
        self.message_str = text
        self.group_id = group_id
        self.sent: list[MessageChain] = []

    def get_group_id(self) -> str:
        return self.group_id

    @staticmethod
    def get_self_id() -> str:
        return "10000"

    @staticmethod
    def chain_result(chain) -> MessageChain:
        return MessageChain(list(chain))

    async def send(self, chain: MessageChain) -> None:
        self.sent.append(chain)


class FakeSender:
    def __init__(self) -> None:
        self.results = []

    async def send_parse_result(self, _event, result) -> bool:
        self.results.append(result)
        return True


def make_image(path: Path, size: tuple[int, int], color: tuple[int, int, int]) -> None:
    PILImage.new("RGB", size, color).save(path)


def test_parse_parserimg_stitch_defaults_to_latest_horizontal() -> None:
    cmd = parse_parserimg_stitch_command("/parserimg stitch")

    assert cmd is not None
    assert cmd.source == "latest"
    assert cmd.direction == "horizontal"


def test_parse_parserimg_stitch_accepts_direction_alias() -> None:
    cmd = parse_parserimg_stitch_command("/parserimg stitch --layout vertical")

    assert cmd is not None
    assert cmd.source == "latest"
    assert cmd.direction == "vertical"


def test_sender_records_successful_image_group_for_stitch(tmp_path: Path) -> None:
    cache = ImageStitchCacheIndex()
    sender = MessageSender(
        config=SimpleNamespace(),
        renderer=SimpleNamespace(),
        image_stitch_cache=cache,
    )
    first = tmp_path / "1.jpg"
    second = tmp_path / "2.jpg"
    make_image(first, (10, 10), (255, 0, 0))
    make_image(second, (12, 10), (0, 255, 0))

    sender._build_send_plan = lambda *_args, **_kwargs: {
        "preview_card": False,
        "force_merge": False,
    }

    async def fake_build_segments(_result, _plan):
        return [Plain("sent")]

    sender._build_segments = fake_build_segments
    group = SendGroup(contents=[ImageContent(first), ImageContent(second)])

    ok = asyncio.run(sender._send_group(DummyEvent(), object(), group))

    assert ok is True
    entry = cache.latest("g1")
    assert entry is not None
    assert entry.paths == (first, second)


def test_parserimg_stitch_default_uses_latest_group_horizontally(tmp_path: Path) -> None:
    cache = ImageStitchCacheIndex()
    first = tmp_path / "1.jpg"
    second = tmp_path / "2.jpg"
    make_image(first, (10, 8), (255, 0, 0))
    make_image(second, (12, 8), (0, 255, 0))
    cache.record(group_id="g1", paths=[first, second])
    sender = FakeSender()
    service = ImageStitchCommandService(
        cfg=SimpleNamespace(cache_dir=tmp_path),
        sender=sender,
        cache_index=cache,
    )

    result = asyncio.run(service.handle(DummyEvent()))

    assert result.sent is True
    assert len(sender.results) == 1
    output = sender.results[0].contents[0].path_task
    assert output.name.startswith("parserimg_stitch_")
    with PILImage.open(output) as img:
        assert img.size == (22, 8)


def test_parserimg_stitch_reports_empty_cache(tmp_path: Path) -> None:
    service = ImageStitchCommandService(
        cfg=SimpleNamespace(cache_dir=tmp_path),
        sender=FakeSender(),
        cache_index=ImageStitchCacheIndex(),
    )

    result = asyncio.run(service.handle(DummyEvent()))

    assert result.sent is False
    assert result.message == "parserimg stitch 失败: image_cache_empty"


def test_parserimg_stitch_rejects_reply_source_explicitly(tmp_path: Path) -> None:
    service = ImageStitchCommandService(
        cfg=SimpleNamespace(cache_dir=tmp_path),
        sender=FakeSender(),
        cache_index=ImageStitchCacheIndex(),
    )

    result = asyncio.run(
        service.handle(DummyEvent(text="/parserimg stitch --source reply"))
    )

    assert result.sent is False
    assert result.message == "parserimg stitch 失败: source_unsupported"
