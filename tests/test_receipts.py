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
async def test_parse_entry_captures_only_current_partial_success(monkeypatch):
    import re
    from collections import defaultdict

    from astrbot.core.message.components import Plain
    from data.plugins.astrbot_plugin_parser import main

    class Event:
        unified_msg_origin = "qq:100"
        message_str = "BVtest"
        bot = object()
        message_obj = SimpleNamespace(
            raw_message=defaultdict(lambda: 100, raw_id="4", time=100, self_id=300)
        )

        def __init__(self):
            self.acks = [SimpleNamespace(raw_ids=("old",))]

        def get_extra(self, key, default=None):
            return self.acks if key == "_sent_message_receipts" else default

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
            return [Plain(self.message_str)]

    monkeypatch.setattr(main, "AiocqhttpMessageEvent", Event)
    plugin = object.__new__(main.ParserPlugin)
    parsed = result()
    parsed.get_resource_id()

    async def parse(*args):
        return parsed

    async def compete(**kwargs):
        return True

    async def send(event, result):
        event.acks.append(SimpleNamespace(raw_ids=("new",)))
        return False

    received = []

    async def enqueue(*args):
        received.append(args)

    plugin.cfg = SimpleNamespace(should_apply_whitelist=lambda _: False)
    plugin.key_pattern_list = [("BV", re.compile("BVtest"))]
    plugin.parser_map = {"BV": SimpleNamespace(parse=parse, platform=parsed.platform)}
    plugin.arbiter = SimpleNamespace(compete=compete)
    plugin.debouncer = SimpleNamespace(
        hit_link=lambda *a: False, check_resource=lambda *a: False
    )
    plugin.sender = SimpleNamespace(send_parse_result=send)
    plugin.receipt_outbox = SimpleNamespace(enabled=True, enqueue=enqueue)
    await plugin.on_message(Event())
    assert received[0][2]["link"] == "BVtest"
    assert received[0][2]["user_id"] == "200"
    assert received[0][3][0].raw_ids == ("new",)
    assert len(received[0][3]) == 1


@pytest.mark.asyncio
async def test_restart_resolves_adapter_before_new_group_messages(
    tmp_path, monkeypatch
):
    import asyncio

    config = SimpleNamespace(
        data_dir=tmp_path,
        receipt_url="http://127.0.0.1:1/api/parser/receipts",
        receipt_token="secret",
    )
    bot = object()
    context = SimpleNamespace(
        get_platform_inst=lambda id: (
            SimpleNamespace(get_client=lambda: bot) if id == "qq" else None
        )
    )
    outbox = receipts.ReceiptOutbox(config, context)
    receipts.atomic_json(
        outbox.root / "pending.json", {"source": source(), "raw_ids": ["77"]}
    )
    done = asyncio.Event()

    async def deliver(path, session, event):
        assert event.bot is bot
        assert event.get_self_id() == "300"
        path.unlink()
        done.set()

    monkeypatch.setattr(outbox, "deliver", deliver)
    outbox.start()
    try:
        await asyncio.wait_for(done.wait(), timeout=2)
    finally:
        await outbox.close()


@pytest.mark.asyncio
async def test_outbox_survives_failure_and_restart_without_media_download(
    tmp_path, monkeypatch
):
    config = SimpleNamespace(
        data_dir=tmp_path,
        receipt_url="http://127.0.0.1:8765/api/parser/receipts",
        receipt_token="secret",
    )
    outbox = receipts.ReceiptOutbox(config, None)
    outbox.start = lambda: None
    event = SimpleNamespace(get_self_id=lambda: "300")
    ack = SimpleNamespace(raw_ids=("500", "501"))
    await outbox.enqueue(event, result(), source(), [ack])
    path = next(outbox.root.glob("*.json"))
    fetched = []

    async def get_msg(event, raw_id):
        fetched.append(raw_id)
        return {"group_id": 100, "user_id": 300, "real_seq": "900", "time": 101}, ""

    monkeypatch.setattr(receipts, "_onebot_reply_message", get_msg)
    with pytest.raises(OSError):
        await outbox.deliver(path, Session(fail=True), event)
    saved = json.loads(path.read_text())
    assert len(saved["deliveries"]) == 1
    assert saved["source"]["user_id"] == "200"
    assert saved["content"]["ai_summary"] == "视频讨论了城市交通"
    assert "raw_ids" not in saved
    restarted = receipts.ReceiptOutbox(config, None)
    session = Session()
    await restarted.deliver(path, session, None)
    assert not path.exists()
    assert fetched == ["500", "501"]
    assert session.calls[0][1]["allow_redirects"] is False


@pytest.mark.asyncio
async def test_outbox_rejects_unmatched_delivery_and_plaintext_remote_url(
    tmp_path, monkeypatch
):
    config = SimpleNamespace(
        data_dir=tmp_path,
        receipt_url="http://example.invalid/api/parser/receipts",
        receipt_token="secret",
    )
    outbox = receipts.ReceiptOutbox(config, None)
    path = tmp_path / "receipt.json"
    receipts.atomic_json(path, {"source": source(), "deliveries": []})
    with pytest.raises(ValueError, match="https_or_loopback"):
        await outbox.deliver(path, Session(), None)
    assert path.exists()


@pytest.mark.asyncio
async def test_wrong_delivery_identity_stays_pending(tmp_path, monkeypatch):
    config = SimpleNamespace(
        data_dir=tmp_path,
        receipt_url="http://127.0.0.1:1/api/parser/receipts",
        receipt_token="secret",
    )
    outbox = receipts.ReceiptOutbox(config, None)
    path = tmp_path / "pending.json"
    receipts.atomic_json(path, {"source": source(), "raw_ids": ["77"]})

    async def get_msg(*args):
        return {"group_id": 999, "user_id": 300, "real_seq": 900, "time": 101}, ""

    monkeypatch.setattr(receipts, "_onebot_reply_message", get_msg)
    session = Session()
    with pytest.raises(ValueError, match="identity_unavailable"):
        await outbox.deliver(path, session, SimpleNamespace(get_self_id=lambda: "300"))
    assert path.exists()
    assert session.calls == []


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
