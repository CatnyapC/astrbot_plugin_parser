import json
from types import SimpleNamespace

import pytest

from data.plugins.astrbot_plugin_parser.core import receipts
from data.plugins.astrbot_plugin_parser.core.data import Author, ParseResult, Platform


def result(info="AI总结: 视频讨论了城市交通"):
    return ParseResult(
        platform=Platform("bilibili", "B站"),
        title="标题",
        text="简介: 原作者介绍",
        url="https://bilibili.com/BVtest",
        timestamp=100,
        author=Author("UP"),
        extra={"info": info},
    )


def source():
    return {
        "group_id": "100",
        "user_id": "200",
        "bot_id": "300",
        "raw_id": "4",
        "ts": 100,
        "link": "BVtest",
        "platform_id": "qq",
    }


@pytest.mark.parametrize(
    "info",
    [
        "哔哩哔哩 cookie 未配置或失效, 无法使用 AI 总结",
        "该视频暂不支持AI总结",
        "AI总结:   ",
    ],
)
def test_metadata_omits_summary_errors_and_empty_content(info):
    content = receipts.content_metadata(result(info))
    assert "ai_summary" not in content
    assert content["text"] == "简介: 原作者介绍"
    assert "cookie" not in json.dumps(content, ensure_ascii=False)


def test_metadata_preserves_platform_summary_and_separates_author():
    content = receipts.content_metadata(result())
    assert content["ai_summary"] == "视频讨论了城市交通"
    assert content["author"] == "UP"
    assert content["timestamp"] == 100
    assert "user_id" not in content


class Response:
    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def json(self):
        return {"status": "accepted"}


class Session:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.fail:
            raise OSError("offline")
        return Response()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "card_url",
    [
        None,
        "https://www.xiaohongshu.com/discovery/item/note?xsec_token=a%2Fb%3D&share_channel=qq",
        "https://www.bilibili.com/video/BVtest?p=2&share_source=qq#reply",
        "https://b23.tv/BVtest?share_source=qq",
    ],
)
async def test_parse_entry_scopes_pre_send_provenance(monkeypatch, tmp_path, card_url):
    import re
    from collections import defaultdict

    from astrbot.core.message.components import Json, Plain, Video
    from data.plugins.astrbot_plugin_parser import main

    class Event:
        unified_msg_origin = "qq:100"
        message_str = "BVtest"
        bot = object()
        message_obj = SimpleNamespace(
            raw_message=defaultdict(lambda: 100, raw_id="4", time=100, self_id=300)
        )

        def get_extra(self, key, default=None):
            return default

        def get_group_id(self):
            return "100"

        def get_sender_id(self):
            return "200"

        def get_self_id(self):
            return "300"

        def get_platform_id(self):
            return "qq"

        def is_private_chat(self):
            return False

        def get_messages(self):
            if card_url:
                card = {"meta": {"detail_1": {"qqdocurl": card_url}}}
                return [
                    Json(data=json.dumps(card) if "xiaohongshu" in card_url else card)
                ]
            return [Plain(self.message_str)]

    monkeypatch.setattr(main, "AiocqhttpMessageEvent", Event)
    plugin = object.__new__(main.ParserPlugin)
    parsed = result()
    media = tmp_path / "video.mp4"
    media.write_bytes(b"video")

    async def parse(*args):
        return parsed

    async def compete(**kwargs):
        return True

    async def send(event, result):
        await receipts.push_chain_receipt([Video(str(media))])
        raise TimeoutError("ACK lost")

    plugin.cfg = SimpleNamespace(should_apply_whitelist=lambda _: False)
    keyword = "http" if card_url else "BV"
    pattern = (
        "discovery/item/[^ ]+" if card_url and "xiaohongshu" in card_url else "BVtest"
    )
    plugin.key_pattern_list = [(keyword, re.compile(pattern))]
    plugin.parser_map = {
        keyword: SimpleNamespace(parse=parse, platform=parsed.platform)
    }
    plugin.arbiter = SimpleNamespace(compete=compete)
    plugin.debouncer = SimpleNamespace(
        hit_link=lambda *a: False, check_resource=lambda *a: False
    )
    plugin.sender = SimpleNamespace(send_parse_result=send)
    plugin.receipt_outbox = receipts.ReceiptOutbox(
        SimpleNamespace(
            data_dir=tmp_path,
            receipt_url="http://127.0.0.1:1/api/parser/receipts",
            receipt_token="secret",
        )
    )
    plugin.receipt_outbox.start = lambda: None
    with pytest.raises(TimeoutError):
        await plugin.on_message(Event())
    saved = json.loads(next(plugin.receipt_outbox.root.glob("*.json")).read_text())
    assert saved["source"]["link"] == (card_url or "BVtest")
    assert saved["source"]["user_id"] == "200"
    assert len(saved["fingerprints"]) == 1
    assert "deliveries" not in saved and "raw_ids" not in saved
    assert receipts.active_receipt.get() is None


def test_fingerprints_hash_actual_files_inside_forward_nodes(tmp_path):
    import hashlib

    from astrbot.core.message.components import File, Image, Node, Nodes, Plain, Video

    media = tmp_path / "video.mp4"
    media.write_bytes(b"actual outgoing bytes")
    other = tmp_path / "image.png"
    other.write_bytes(b"other bytes")
    chain = [
        Nodes(
            [
                Node(content=[Video(f"file:////{str(media).lstrip('/')}")]),
                Node(
                    content=[
                        Image(str(other)),
                        File(name="video", file=str(media)),
                        Plain("body"),
                    ]
                ),
            ]
        )
    ]
    assert receipts.media_fingerprints(chain) == sorted(
        [
            hashlib.sha256(media.read_bytes()).hexdigest(),
            hashlib.sha256(other.read_bytes()).hexdigest(),
        ]
    )


@pytest.mark.asyncio
async def test_sender_persists_before_send_even_when_ack_is_lost(tmp_path):
    from astrbot.core.message.components import Video
    from data.plugins.astrbot_plugin_parser.core.sender import MessageSender

    media = tmp_path / "video.mp4"
    media.write_bytes(b"video")
    outbox = receipts.ReceiptOutbox(
        SimpleNamespace(
            data_dir=tmp_path,
            receipt_url="http://127.0.0.1:1/api/parser/receipts",
            receipt_token="secret",
        )
    )
    outbox.start = lambda: None
    sent = []

    async def send(chain):
        assert list(outbox.root.glob("*.json"))
        sent.append(chain)
        raise TimeoutError("ACK lost")

    sender = MessageSender(SimpleNamespace(), SimpleNamespace())
    token = receipts.active_receipt.set((outbox, result(), source()))
    try:
        with pytest.raises(TimeoutError):
            await sender._send_chain(
                SimpleNamespace(send=send, chain_result=lambda chain: chain),
                [Video(str(media))],
            )
    finally:
        receipts.active_receipt.reset(token)
    assert len(sent) == 1
    assert len(list(outbox.root.glob("*.json"))) == 1


@pytest.mark.asyncio
async def test_outbox_survives_failure_and_restart_without_adapter_or_media(tmp_path):
    import asyncio

    config = SimpleNamespace(
        data_dir=tmp_path,
        receipt_url="http://127.0.0.1:8765/api/parser/receipts",
        receipt_token="secret",
    )
    outbox = receipts.ReceiptOutbox(config)
    outbox.start = lambda: None
    original_source = source()
    original_source["link"] = (
        "https://www.bilibili.com/video/BVtest?p=2&share_source=qq"
    )
    await outbox.enqueue(result(), original_source, ["a" * 64])
    path = next(outbox.root.glob("*.json"))
    saved = json.loads(path.read_text())
    with pytest.raises(OSError):
        await outbox.deliver(path, Session(fail=True))
    assert json.loads(path.read_text()) == saved
    assert saved["content"]["ai_summary"] == "视频讨论了城市交通"
    restarted = receipts.ReceiptOutbox(config)
    done = asyncio.Event()
    session = Session()
    deliver = restarted.deliver

    async def observed_deliver(path, _session):
        await deliver(path, session)
        done.set()

    restarted.deliver = observed_deliver
    restarted.start()
    try:
        await asyncio.wait_for(done.wait(), timeout=2)
    finally:
        await restarted.close()
    assert not path.exists()
    assert session.calls[0][1]["json"] == saved
    assert session.calls[0][1]["allow_redirects"] is False


@pytest.mark.asyncio
async def test_outbox_rejects_plaintext_remote_url_and_skips_non_media(tmp_path):
    config = SimpleNamespace(
        data_dir=tmp_path,
        receipt_url="http://example.invalid/api/parser/receipts",
        receipt_token="secret",
    )
    outbox = receipts.ReceiptOutbox(config)
    outbox.start = lambda: None
    await outbox.enqueue(result(), source(), [])
    assert not list(outbox.root.glob("*.json"))
    await outbox.enqueue(result(), source(), ["a" * 64])
    path = next(outbox.root.glob("*.json"))
    with pytest.raises(ValueError, match="https_or_loopback"):
        await outbox.deliver(path, Session())
    assert path.exists()


@pytest.mark.asyncio
async def test_youtube_and_tiktok_preserve_already_fetched_description(monkeypatch):
    import re

    from data.plugins.astrbot_plugin_parser.core.parsers.tiktok import TikTokParser
    from data.plugins.astrbot_plugin_parser.core.parsers.youtube import YouTubeParser

    async def info(*args, **kwargs):
        return SimpleNamespace(
            title="Title",
            channel="Author",
            channel_id="channel",
            description="Existing description",
            thumbnail="cover",
            duration=1,
            timestamp=100,
        )

    downloader = SimpleNamespace(
        ytdlp_extract_info=info,
        ytdlp_download_video=lambda *a, **k: "unused",
        download_img=lambda **k: "unused",
    )
    for cls, pattern, url in [
        (YouTubeParser, r".+", "https://youtu.be/example"),
        (TikTokParser, r"(www).+", "www.tiktok.com/example"),
    ]:
        parser = object.__new__(cls)
        parser.cfg = SimpleNamespace(max_duration=10, proxy=None)
        parser.mycfg = SimpleNamespace(proxy=None)
        parser.downloader = downloader
        parser.headers = {}
        parser.cookiejar = SimpleNamespace(cookie_file=None)
        if cls is YouTubeParser:

            async def author(_):
                return Author("Author")

            parser._fetch_author_info = author
            parser.create_video_content = lambda *a: SimpleNamespace(duration=1)
            parsed = await parser.parse_video(re.search(pattern, url))
        else:
            parsed = await parser._parse(re.search(pattern, url))
        assert parsed.text == "Existing description"
        assert parsed.url == url
