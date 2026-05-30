# main.py

import asyncio
import re

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star
from astrbot.core import AstrBotConfig
from astrbot.core.message.components import At, Image, Json
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from .core.arbiter import ArbiterContext, EmojiLikeArbiter
from .core.clean import CacheCleaner
from .core.config import PluginConfig
from .core.debounce import Debouncer
from .core.download import Downloader
from .core.parsers import BaseParser, BilibiliParser
from .core.render import Renderer
from .core.sender import MessageSender
from .core.utils import extract_json_url
from .core.video_slice import VideoSliceCacheIndex, VideoSliceCommandService


class ParserPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.cfg = PluginConfig(config, context=context)
        # 渲染器
        self.renderer = Renderer(self.cfg)
        # 下载器
        self.downloader = Downloader(self.cfg)
        # 防抖器
        self.debouncer = Debouncer(self.cfg)
        # 仲裁器
        self.arbiter = EmojiLikeArbiter()
        self.video_slice_cache = VideoSliceCacheIndex(
            persist_path=self.cfg.data_dir / "video_slice_index.json"
        )
        # 消息发送器
        self.sender = MessageSender(
            self.cfg,
            self.renderer,
            context=context,
            video_slice_cache=self.video_slice_cache,
        )
        self.video_slice_service = VideoSliceCommandService(
            cfg=self.cfg,
            sender=self.sender,
            cache_index=self.video_slice_cache,
        )
        # 缓存清理器
        self.cleaner = CacheCleaner(self.cfg)
        # 关键词 -> Parser 映射
        self.parser_map: dict[str, BaseParser] = {}
        # 关键词 -> 正则 列表
        self.key_pattern_list: list[tuple[str, re.Pattern[str]]] = []

    async def initialize(self):
        """加载、重载插件时触发"""
        # 加载渲染器资源
        await asyncio.to_thread(Renderer.load_resources)
        # 注册解析器
        self._register_parser()

    async def terminate(self):
        """插件卸载时触发"""
        # 关下载器里的会话
        await self.downloader.close()
        # 关所有解析器里的会话 (去重后的实例)
        unique_parsers = set(self.parser_map.values())
        for parser in unique_parsers:
            await parser.close_session()
        # 关缓存清理器
        await self.cleaner.stop()

    def _register_parser(self):
        """注册解析器（以 parser.enable 为唯一启用来源）"""
        # 所有 Parser 子类
        all_subclass = BaseParser.get_all_subclass()
        enabled_platforms = set(self.cfg.parser.enabled_platforms())

        enabled_classes: list[type[BaseParser]] = []
        enabled_names: list[str] = []
        for cls in all_subclass:
            platform_name = cls.platform.name

            if platform_name not in enabled_platforms:
                logger.debug(f"[parser] 平台未启用或未配置: {platform_name}")
                continue

            enabled_classes.append(cls)
            enabled_names.append(platform_name)

            # 一个平台一个 parser 实例
            parser = cls(self.cfg, self.downloader)

            # 关键词 → parser
            for keyword, _ in cls._key_patterns:
                self.parser_map[keyword] = parser

        logger.debug(f"启用平台: {'、'.join(enabled_names) if enabled_names else '无'}")

        # -------- 关键词-正则表（统一生成） --------
        patterns: list[tuple[str, re.Pattern[str]]] = []

        for cls in enabled_classes:
            for kw, pat in cls._key_patterns:
                patterns.append((kw, re.compile(pat) if isinstance(pat, str) else pat))

        # 长关键词优先，避免短词抢匹配
        patterns.sort(key=lambda x: -len(x[0]))

        self.key_pattern_list = patterns

        logger.debug(f"[parser] 关键词-正则对已生成: {[kw for kw, _ in patterns]}")

    def _get_parser_by_type(self, parser_type):
        for parser in self.parser_map.values():
            if isinstance(parser, parser_type):
                return parser
        raise ValueError(f"未找到类型为 {parser_type} 的 parser 实例")

    @staticmethod
    def _get_filter_user_id(event: AstrMessageEvent) -> str:
        return str(event.get_sender_id())

    @staticmethod
    def _should_skip_router_requeue(event: AstrMessageEvent) -> bool:
        return bool(event.get_extra("_router_timeout_requeue", False))

    @staticmethod
    def _normalize_command_user_id(user_id: str) -> str:
        return str(user_id or "").strip()

    def _is_parse_whitelist_allowed(
        self,
        event: AstrMessageEvent,
        group_id: str | None,
        user_id: str,
    ) -> bool:
        if event.is_admin():
            return True
        if self.cfg.is_admin_user(user_id):
            return True
        return self.cfg.is_whitelist_allowed(group_id, user_id)

    async def _can_manage_whitelist(self, event: AstrMessageEvent) -> tuple[bool, str]:
        if event.is_admin():
            return True, ""

        group_id = str(event.get_group_id() or "").strip()
        if not group_id:
            return False, "请在群聊中使用 parserwl。"

        get_group = getattr(event, "get_group", None)
        if get_group is None:
            return False, "当前平台不支持读取群权限。"

        try:
            group = await get_group(group_id)
        except Exception as e:
            logger.warning(f"[parser] 获取群权限失败: {e}")
            return False, "读取群权限失败，无法修改 parser 白名单。"
        if group is None:
            return False, "读取群权限失败，无法修改 parser 白名单。"

        sender_id = str(event.get_sender_id())
        owner_id = str(getattr(group, "group_owner", "") or "")
        admin_ids = {
            str(user_id)
            for user_id in (getattr(group, "group_admins", []) or [])
            if str(user_id)
        }
        if sender_id == owner_id or sender_id in admin_ids:
            return True, ""

        return False, "只有群主/管理员可以修改 parser 白名单。"

    @filter.command_group("parserwl")
    def parserwl(self) -> None:
        """parser 白名单管理"""

    @parserwl.command("list")
    async def parserwl_list(self, event: AstrMessageEvent):
        """查看当前群 parser 白名单"""
        allowed, reason = await self._can_manage_whitelist(event)
        if not allowed:
            yield event.plain_result(reason)
            return

        group_id = str(event.get_group_id() or "").strip()
        if not group_id:
            yield event.plain_result("请在群聊中使用 parserwl。")
            return

        users = sorted(self.cfg.group_user_whitelist.get(group_id, set()))
        if users:
            yield event.plain_result(f"当前群 parser 白名单：{', '.join(users)}")
        else:
            yield event.plain_result("当前群 parser 白名单为空。")

    @parserwl.command("add")
    async def parserwl_add(self, event: AstrMessageEvent, user_id: str = ""):
        """添加当前群 parser 白名单用户"""
        allowed, reason = await self._can_manage_whitelist(event)
        if not allowed:
            yield event.plain_result(reason)
            return

        group_id = str(event.get_group_id() or "").strip()
        user_id = self._normalize_command_user_id(user_id)
        if not group_id:
            yield event.plain_result("请在群聊中使用 parserwl。")
            return
        if not user_id:
            yield event.plain_result("用法：/parserwl add <user_id>")
            return

        if self.cfg.add_whitelist_user(group_id, user_id):
            yield event.plain_result(f"已添加 parser 白名单用户：{user_id}")
        else:
            yield event.plain_result(f"parser 白名单已包含：{user_id}")

    @parserwl.command("remove")
    async def parserwl_remove(self, event: AstrMessageEvent, user_id: str = ""):
        """移除当前群 parser 白名单用户"""
        allowed, reason = await self._can_manage_whitelist(event)
        if not allowed:
            yield event.plain_result(reason)
            return

        group_id = str(event.get_group_id() or "").strip()
        user_id = self._normalize_command_user_id(user_id)
        if not group_id:
            yield event.plain_result("请在群聊中使用 parserwl。")
            return
        if not user_id:
            yield event.plain_result("用法：/parserwl remove <user_id>")
            return

        if self.cfg.remove_whitelist_user(group_id, user_id):
            yield event.plain_result(f"已移除 parser 白名单用户：{user_id}")
        else:
            yield event.plain_result(f"parser 白名单中不存在：{user_id}")

    @parserwl.command("clear")
    async def parserwl_clear(self, event: AstrMessageEvent):
        """清空当前群 parser 白名单"""
        allowed, reason = await self._can_manage_whitelist(event)
        if not allowed:
            yield event.plain_result(reason)
            return

        group_id = str(event.get_group_id() or "").strip()
        if not group_id:
            yield event.plain_result("请在群聊中使用 parserwl。")
            return

        if self.cfg.clear_whitelist_group(group_id):
            yield event.plain_result("已清空当前群 parser 白名单。")
        else:
            yield event.plain_result("当前群 parser 白名单已为空。")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """消息的统一入口"""
        if self._should_skip_router_requeue(event):
            logger.debug("[parser] skip router timeout requeue event")
            return

        umo = event.unified_msg_origin
        group_id = event.get_group_id()
        user_id = self._get_filter_user_id(event)

        # 消息链
        chain = event.get_messages()
        if not chain:
            return

        seg1 = chain[0]
        text = event.message_str

        # 卡片解析：解析Json组件，提取URL
        if isinstance(seg1, Json):
            text = extract_json_url(seg1.data)
            logger.debug(f"解析Json组件: {text}")

        if not text:
            return
        if text.strip().lstrip("/").startswith("parserclip "):
            return

        self_id = event.get_self_id()

        # 指定机制：专门@其他bot的消息不解析
        if isinstance(seg1, At) and str(seg1.qq) != self_id:
            return

        # 核心匹配逻辑 ：关键词 + 正则双重判定，汇集了所有解析器的正则对。
        keyword: str = ""
        searched: re.Match[str] | None = None
        for kw, pat in self.key_pattern_list:
            if kw not in text:
                continue
            if m := pat.search(text):
                keyword, searched = kw, m
                break
        if searched is None:
            return
        logger.debug(f"匹配结果: {keyword}, {searched}")

        parser = self.parser_map[keyword]
        if self.cfg.should_apply_whitelist(parser.platform.name):
            if not self._is_parse_whitelist_allowed(event, group_id, user_id):
                return

        # 仲裁机制
        if isinstance(event, AiocqhttpMessageEvent) and not event.is_private_chat():
            raw = event.message_obj.raw_message
            if not isinstance(raw, dict):
                logger.warning(f"Unexpected raw_message type: {type(raw)}")
                return
            is_win = await self.arbiter.compete(
                bot=event.bot,
                ctx=ArbiterContext(
                    message_id=int(raw["message_id"]),
                    msg_time=int(raw["time"]),
                    self_id=int(raw["self_id"]),
                ),
            )
            if not is_win:
                logger.debug("Bot在仲裁中输了, 跳过解析")
                return
            logger.debug("Bot在仲裁中胜出, 准备解析...")

        # 基于link防抖
        link = searched.group(0)
        if self.debouncer.hit_link(umo, link):
            logger.warning(f"[链接防抖] 链接 {link} 在防抖时间内，跳过解析")
            return

        # 解析
        parse_res = await parser.parse(keyword, searched)

        # 基于资源ID防抖
        resource_id = parse_res.get_resource_id()
        if self.debouncer.check_resource(umo, resource_id):
            logger.warning(f"[资源防抖] 资源 {resource_id} 在防抖时间内，跳过发送")
            return

        # 发送
        if await self.sender.send_parse_result(event, parse_res):
            self.debouncer.mark_resource(umo, resource_id)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("登录B站", alias={"blogin", "登录b站"})
    async def login_bilibili(self, event: AstrMessageEvent):
        """扫码登录B站"""
        parser: BilibiliParser = self._get_parser_by_type(BilibiliParser)  # type: ignore
        qrcode = await parser.login.login_with_qrcode()
        yield event.chain_result([Image.fromBytes(qrcode)])
        async for msg in parser.login.check_qr_state():
            yield event.plain_result(msg)

    @filter.command("parserclip", alias={"/parserclip"})
    async def parserclip_command(self, event: AstrMessageEvent):
        """Reviewed parser cache video slice command."""
        result = await self.video_slice_service.handle(event)
        if result.status == "ignored" or result.sent:
            return
        if result.message:
            yield event.plain_result(result.message)
