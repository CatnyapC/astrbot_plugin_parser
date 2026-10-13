import ast
import asyncio
import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image as PILImage

from astrbot.core.message.components import Image, Plain, Reply
from astrbot.core.message.message_event_result import MessageChain

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.sender import MessageSender
from core.video_gif import (
    GIF_FPS,
    VideoGifCommandService,
    find_cycle_frames,
    parse_parserclip_gif_command,
)
from core.video_slice import (
    VideoSliceCacheIndex,
    VideoSliceCommandService,
    VideoSliceResult,
    _event_raw_id,
)


class Event:
    def __init__(self, text="/parserclip gif", reply="", sender="controller"):
        self.message_str = text
        self.sender = sender
        self.message_obj = SimpleNamespace(
            raw_message={"raw_id": "command-42", "reply_to_raw_id": reply}
        )
        self.sent = []

    def get_group_id(self):
        return "g1"

    def get_sender_id(self):
        return self.sender

    def get_self_id(self):
        return "parser-bot"

    def is_admin(self):
        return False

    def chain_result(self, chain):
        return MessageChain(chain)

    async def send(self, chain):
        self.sent.append(chain)


def make_service(tmp_path, runner=None, sender=None):
    cfg = SimpleNamespace(
        cache_dir=tmp_path,
        max_size=20 * 1024 * 1024,
        admins_id=[],
        parser_video_slice_controller_ids="controller",
        single_heavy_render_card=False,
        forward_threshold=10,
    )
    return VideoGifCommandService(
        VideoSliceCommandService(
            cfg=cfg,
            sender=sender or SimpleNamespace(),
            cache_index=VideoSliceCacheIndex(),
            run_process=runner,
        )
    )


def cycle_frames():
    brightness = (128 + 100 * np.sin(np.arange(12) * 2 * np.pi / 12)).astype(np.uint8)
    return np.repeat(brightness[:, None], 32 * 32, axis=1)


def test_command_defaults_and_validation():
    assert parse_parserclip_gif_command("/parserclip gif") == {
        "source": "latest",
        "cycle": "off",
    }
    assert parse_parserclip_gif_command(
        "/parserclip gif --source=reply --cycle auto"
    ) == {"source": "reply", "cycle": "auto"}
    assert parse_parserclip_gif_command("/parserclip slice") is None
    for args in (
        "--cycle bad",
        "--source current",
        "--fps 30",
        "--source latest --source reply",
        "--cycle",
        "extra off",
        "--cycle 'auto",
    ):
        with pytest.raises(ValueError, match="invalid_arguments"):
            parse_parserclip_gif_command(f"/parserclip gif {args}")


def test_cycle_detects_shortest_complete_repeat():
    assert find_cycle_frames(np.tile(cycle_frames(), (3, 1))) == 12


@pytest.mark.parametrize(
    "frames",
    [
        np.zeros((36, 1024)),
        np.repeat(np.arange(36)[:, None] * 7, 1024, axis=1),
        np.random.default_rng(0).integers(0, 255, (36, 1024)),
    ],
)
def test_cycle_rejects_static_and_nonperiodic(frames):
    with pytest.raises(ValueError, match="cycle_not_found"):
        find_cycle_frames(frames)


def test_reply_does_not_fallback_to_cached_latest(tmp_path):
    service = make_service(tmp_path)
    source = tmp_path / "latest.mp4"
    source.write_bytes(b"video")
    service.video_service.cache_index.record(
        group_id="g1", path=source, source_raw_id="latest", parser_sent_output=True
    )
    other = tmp_path / "other.mp4"
    other.write_bytes(b"another video")
    service.video_service.cache_index.record(
        group_id="g1", path=other, source_raw_id="other"
    )
    for reply in ("", "missing"):
        result = asyncio.run(
            service.handle(Event("/parserclip gif --source reply", reply))
        )
        assert result.status == "failed"
        assert not result.sent
    cache = service.video_service.cache_index
    assert (
        cache.resolve(
            group_id="g1", source="reply", reply_raw_id="latest", strict_reply=True
        )[0].path
        == source
    )
    assert cache.resolve(
        group_id="g1", source="reply", reply_raw_id="missing", strict_reply=True
    ) == (None, "reply_source_not_found")


def test_permission_rejects_before_processing(tmp_path):
    result = asyncio.run(make_service(tmp_path).handle(Event(sender="member")))
    assert result.message == "parserclip gif 失败: permission_denied"


def test_telegram_command_raw_id():
    event = Event()
    event.message_obj.raw_message = SimpleNamespace(message_id=42)
    assert _event_raw_id(event) == "42"


@pytest.mark.parametrize(
    "failure", ["ffmpeg_timeout", "send_failed", "output_too_large"]
)
def test_failed_outputs_are_removed(tmp_path, failure):
    async def runner(cmd, timeout):
        if cmd[0] == "ffprobe":
            return (
                0,
                json.dumps(
                    {"streams": [{"codec_type": "video"}], "format": {"duration": "1"}}
                ),
                "",
            )
        output = Path(cmd[-1])
        if failure == "ffmpeg_timeout":
            output.write_bytes(b"partial")
            return 124, "", "timeout"
        PILImage.new("RGB", (10, 10)).save(output, loop=0)
        if failure == "output_too_large":
            with output.open("ab") as stream:
                stream.truncate(11 * 1024 * 1024)
        return 0, "", ""

    async def send(*_args):
        return False

    service = make_service(tmp_path, runner, SimpleNamespace(send_parse_result=send))
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    service.video_service.cache_index.record(group_id="g1", path=source)
    result = asyncio.run(service.handle(Event()))
    assert result.message == f"parserclip gif 失败: {failure}"
    assert not list(tmp_path.glob("parser_gif_*"))


def test_duration_limit_does_not_truncate(tmp_path):
    commands = []

    async def runner(cmd, timeout):
        commands.append(cmd)
        return (
            0,
            json.dumps(
                {"streams": [{"codec_type": "video"}], "format": {"duration": "61"}}
            ),
            "",
        )

    service = make_service(tmp_path, runner)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    service.video_service.cache_index.record(group_id="g1", path=source)
    result = asyncio.run(service.handle(Event()))
    assert result.message == "parserclip gif 失败: duration_too_long"
    assert len(commands) == 1


def test_static_auto_does_not_fallback_to_full_video(tmp_path):
    commands = []

    async def runner(cmd, timeout):
        commands.append(cmd)
        if cmd[0] == "ffprobe":
            return (
                0,
                json.dumps(
                    {"streams": [{"codec_type": "video"}], "format": {"duration": "3"}}
                ),
                "",
            )
        assert cmd[-1].endswith(".gray")
        Path(cmd[-1]).write_bytes(bytes(36 * 32 * 32))
        return 0, "", ""

    service = make_service(tmp_path, runner)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    service.video_service.cache_index.record(group_id="g1", path=source)
    result = asyncio.run(service.handle(Event("/parserclip gif --cycle auto")))
    assert result.message == "parserclip gif 失败: cycle_not_found"
    assert len(commands) == 2
    assert not list(tmp_path.glob("parser_gif_*"))


def test_failure_receipt_replies_to_command():
    source = Path(__file__).resolve().parents[1] / "main.py"
    tree = ast.parse(source.read_text())
    cls = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ParserPlugin"
    )
    handler = next(
        node
        for node in cls.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "parserclip_command"
    )
    handler.decorator_list = []
    namespace = {
        "AstrMessageEvent": object,
        "Reply": Reply,
        "Plain": Plain,
        "_event_raw_id": lambda event: "command-42",
    }
    exec(
        compile(ast.Module(body=[handler], type_ignores=[]), str(source), "exec"),
        namespace,
    )

    async def failed(_event):
        return VideoSliceResult("failed", "parserclip gif 失败: cycle_not_found")

    async def collect():
        return [
            result
            async for result in namespace["parserclip_command"](
                SimpleNamespace(video_gif_service=SimpleNamespace(handle=failed)),
                Event(),
            )
        ]

    results = asyncio.run(collect())
    assert isinstance(results[0].chain[0], Reply)
    assert results[0].chain[0].id == "command-42"
    assert results[0].chain[1].text == "parserclip gif 失败: cycle_not_found"


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="ffmpeg required"
)
@pytest.mark.parametrize("cycle", ["off", "auto"])
def test_real_gif_and_native_reply(tmp_path, cycle):
    from core.video_slice import _run_process

    for index, frame in enumerate(np.tile(cycle_frames(), (3, 1))):
        PILImage.fromarray(frame.reshape(32, 32)).save(
            tmp_path / f"frame{index:02d}.png"
        )
    source = tmp_path / "source.mkv"
    code, _, error = asyncio.run(
        _run_process(
            [
                "ffmpeg",
                "-y",
                "-v",
                "error",
                "-framerate",
                str(GIF_FPS),
                "-i",
                str(tmp_path / "frame%02d.png"),
                "-c:v",
                "ffv1",
                str(source),
            ],
            20,
        )
    )
    assert code == 0, error
    service = make_service(tmp_path)
    sender = MessageSender(config=service.video_service.cfg, renderer=SimpleNamespace())
    service.video_service.sender = sender
    service.video_service.cache_index.record(
        group_id="g1", path=source, source_raw_id="source-video"
    )
    event = Event(
        f"/parserclip gif --source reply --cycle {cycle}", reply="source-video"
    )

    async def concurrent_requests():
        return await asyncio.gather(service.handle(event), service.handle(event))

    result, duplicate = asyncio.run(concurrent_requests())
    assert result.status == "ok", result.message
    with PILImage.open(tmp_path / result.output_name) as image:
        assert image.format == "GIF"
        assert image.info["loop"] == 0
        assert image.n_frames == (36 if cycle == "off" else 12)
    assert isinstance(event.sent[0].chain[0], Reply)
    assert event.sent[0].chain[0].id == "command-42"
    assert isinstance(event.sent[0].chain[1], Image)
    assert duplicate.status == "duplicate"
    assert len(event.sent) == 1
