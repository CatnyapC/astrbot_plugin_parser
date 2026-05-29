import asyncio
from itertools import chain
from pathlib import Path
from random import SystemRandom
from uuid import uuid4

from PIL import Image as PILImage
from PIL import ImageEnhance, ImageOps

from astrbot.api import logger
from astrbot.core.message.components import (
    BaseMessageComponent,
    File,
    Image,
    Node,
    Nodes,
    Plain,
    Record,
    Video,
)
from astrbot.core.platform.astr_message_event import AstrMessageEvent

from .config import PluginConfig
from .data import (
    AudioContent,
    DynamicContent,
    FileContent,
    GraphicsContent,
    ImageContent,
    ParseResult,
    SendGroup,
    TextContent,
    VideoContent,
)
from .exception import (
    DownloadException,
    DownloadLimitException,
    SizeLimitException,
    ZeroSizeException,
)
from .render import Renderer
from .video_slice import VideoSliceCacheIndex, record_video_slice_cache_from_group

SEND_TIMEOUT_SECONDS = 300.0
SEND_RETRY_TIMEOUT_SECONDS = 600.0
SEND_TIMEOUT_RETRIES = 1


class MessageSender:
    """
    消息发送器

    职责：
    - 根据解析结果（ParseResult）规划发送策略
    - 控制是否渲染卡片、是否强制合并转发
    - 将不同类型的内容转换为 AstrBot 消息组件并发送

    重要原则：
    - 不在此处做解析
    - 不在此处决定“内容是什么”
    - 只负责“怎么发”
    """

    def __init__(
        self,
        config: PluginConfig,
        renderer: Renderer,
        context=None,
        video_slice_cache: VideoSliceCacheIndex | None = None,
    ):
        self.cfg = config
        self.renderer = renderer
        self.context = context
        self._rand = SystemRandom()
        self.video_slice_cache = video_slice_cache

    def _to_file_uri(self, path: Path) -> str:
        path = path.resolve()
        posix_path = path.as_posix()
        if posix_path.startswith("/"):
            # AstrBot currently strips `file:///` via url[8:], so keep one extra slash.
            return f"file:////{posix_path.lstrip('/')}"
        return path.as_uri()

    def _send_timeout_seconds(self) -> float:
        value = getattr(self.cfg, "send_timeout_seconds", SEND_TIMEOUT_SECONDS)
        try:
            timeout = float(value)
        except (TypeError, ValueError):
            timeout = SEND_TIMEOUT_SECONDS
        return timeout if timeout > 0 else SEND_TIMEOUT_SECONDS

    def _send_retry_timeout_seconds(self) -> float:
        value = getattr(
            self.cfg,
            "send_retry_timeout_seconds",
            SEND_RETRY_TIMEOUT_SECONDS,
        )
        try:
            timeout = float(value)
        except (TypeError, ValueError):
            timeout = SEND_RETRY_TIMEOUT_SECONDS
        return timeout if timeout > 0 else SEND_RETRY_TIMEOUT_SECONDS

    @staticmethod
    def _send_timeout_retries() -> int:
        return SEND_TIMEOUT_RETRIES

    async def _send_chain(self, event: AstrMessageEvent, chain: list[BaseMessageComponent]):
        max_attempts = 1 + self._send_timeout_retries()
        seg_meta = self._collect_seg_meta(chain)

        for attempt in range(1, max_attempts + 1):
            timeout_seconds = (
                self._send_timeout_seconds()
                if attempt == 1
                else self._send_retry_timeout_seconds()
            )
            try:
                await asyncio.wait_for(
                    event.send(event.chain_result(chain)),
                    timeout=timeout_seconds,
                )
                return
            except asyncio.TimeoutError as exc:
                if attempt < max_attempts:
                    logger.warning(
                        "发送解析结果超时，准备重试: "
                        f"attempt={attempt}/{max_attempts}, "
                        f"timeout={timeout_seconds}s, segments={seg_meta}"
                    )
                    continue
                raise TimeoutError(
                    f"send timeout after {timeout_seconds}s (attempt {attempt}/{max_attempts})"
                ) from exc

    @staticmethod
    def _iter_contents(result: ParseResult):
        return chain(result.contents, result.repost.contents if result.repost else ())

    def _build_send_plan(
        self,
        result: ParseResult,
        contents: list | tuple | None = None,
        *,
        force_merge_override: bool | None = None,
        render_card_override: bool | None = None,
    ) -> dict:
        """
        根据解析结果生成发送计划（plan）

        plan 只做“策略决策”，不做任何 IO 或发送动作。
        后续发送流程严格按 plan 执行，避免逻辑分散。
        """
        light, heavy = [], []

        # 合并主内容 + 转发内容，统一参与发送策略计算
        iterable = contents if contents is not None else self._iter_contents(result)
        for cont in iterable:
            match cont:
                case ImageContent() | GraphicsContent() | TextContent():
                    light.append(cont)
                case VideoContent() | AudioContent() | FileContent() | DynamicContent():
                    heavy.append(cont)
                case _:
                    light.append(cont)

        # 仅在“单一重媒体且无其他内容”时，才允许渲染卡片
        is_single_heavy = len(heavy) == 1 and not light
        render_card = is_single_heavy and self.cfg.single_heavy_render_card
        if render_card_override is not None:
            render_card = render_card_override
        # 实际消息段数量（卡片也算一个段）
        seg_count = len(light) + len(heavy) + (1 if render_card else 0)

        # 达到阈值后，强制合并转发，避免刷屏
        force_merge = seg_count >= self.cfg.forward_threshold
        if force_merge_override is not None:
            force_merge = force_merge_override

        return {
            "light": light,
            "heavy": heavy,
            "render_card": render_card,
            # 预览卡片：仅在“渲染卡片 + 不合并”时独立发送
            "preview_card": render_card and not force_merge,
            "force_merge": force_merge,
        }

    def _resolve_thread_archive_writer(self):
        if self.context is None or not callable(
            getattr(self.context, "get_registered_star", None)
        ):
            return None
        try:
            meta = self.context.get_registered_star("astrbot_plugin_thread_archive")
        except Exception:
            meta = None
        star = getattr(meta, "star_cls", None) if meta is not None else None
        if star is None or not callable(getattr(star, "archive_sent_chain", None)):
            return None
        return star

    async def _archive_sent_chain(
        self,
        event: AstrMessageEvent,
        chain: list[BaseMessageComponent],
        *,
        source: str,
        raw_json_extra: dict | None = None,
    ) -> None:
        archive = self._resolve_thread_archive_writer()
        if archive is None or not chain:
            return
        try:
            await archive.archive_sent_chain(
                event=event,
                chain=list(chain),
                source="after_message_sent",
                raw_json_extra={
                    "origin_source": source,
                    **(raw_json_extra or {}),
                },
                include_current_event_raw_id=True,
                use_current_event_raw_id_as_message_raw_id=False,
            )
        except Exception as exc:
            logger.warning(f"parser archive_sent_chain failed: {exc}")

    @staticmethod
    def _file_uri_to_path(uri: str) -> Path | None:
        if uri.startswith("file:////"):
            return Path("/" + uri.removeprefix("file:////"))
        if uri.startswith("file:///"):
            return Path("/" + uri.removeprefix("file:///"))
        return None

    async def _normalize_image_for_retry(self, path: Path) -> Path | None:
        src = path.resolve()
        if not src.is_file():
            return None

        def _rewrite() -> Path:
            dst = src.with_name(f"{src.stem}_qqsafe_{uuid4().hex[:8]}.jpg")
            with PILImage.open(src) as img:
                img = ImageOps.exif_transpose(img)
                if img.mode in ("RGBA", "LA") or (
                    img.mode == "P" and "transparency" in img.info
                ):
                    rgba = img.convert("RGBA")
                    rgb = PILImage.new("RGB", rgba.size, (255, 255, 255))
                    rgb.paste(rgba, mask=rgba.getchannel("A"))
                else:
                    rgb = img.convert("RGB")
                rgb.save(dst, format="JPEG", quality=95, progressive=False)
            return dst

        try:
            return await asyncio.to_thread(_rewrite)
        except Exception as exc:
            logger.warning(f"parser normalize image for retry failed: path={src} err={exc}")
            return None

    async def _perturb_image_for_retry(self, path: Path) -> Path | None:
        src = path.resolve()
        if not src.is_file():
            return None

        def _rewrite() -> Path:
            dst = src.with_name(f"{src.stem}_qqsafe_{uuid4().hex[:8]}.png")
            with PILImage.open(src) as img:
                img = ImageOps.exif_transpose(img).convert("RGB")
                width, height = img.size
                scale = self._rand.uniform(0.94, 1.08)
                resized = img.resize(
                    (
                        max(64, int(width * scale)),
                        max(64, int(height * scale)),
                    ),
                    resample=PILImage.Resampling.LANCZOS,
                )
                angle = self._rand.uniform(-2.2, 2.2)
                rotated = resized.rotate(
                    angle,
                    resample=PILImage.Resampling.BICUBIC,
                    expand=True,
                    fillcolor=(
                        self._rand.randint(232, 255),
                        self._rand.randint(232, 255),
                        self._rand.randint(232, 255),
                    ),
                )
                bright = ImageEnhance.Brightness(rotated).enhance(
                    self._rand.uniform(0.92, 1.08)
                )
                contrast = ImageEnhance.Contrast(bright).enhance(
                    self._rand.uniform(0.9, 1.12)
                )
                colorized = ImageEnhance.Color(contrast).enhance(
                    self._rand.uniform(0.88, 1.18)
                )
                border = (
                    self._rand.randint(8, 28),
                    self._rand.randint(8, 28),
                    self._rand.randint(8, 28),
                    self._rand.randint(8, 28),
                )
                fill = (
                    self._rand.randint(228, 255),
                    self._rand.randint(228, 255),
                    self._rand.randint(228, 255),
                )
                img = ImageOps.expand(colorized, border=border, fill=fill)
                img.save(dst, format="PNG")
            return dst

        try:
            return await asyncio.to_thread(_rewrite)
        except Exception as exc:
            logger.warning(f"parser perturb image for retry failed: path={src} err={exc}")
            return None

    async def _normalize_image_segments_for_retry(
        self, segs: list[BaseMessageComponent]
    ) -> list[BaseMessageComponent] | None:
        normalized: list[BaseMessageComponent] = []
        changed = False

        for seg in segs:
            if not isinstance(seg, Image):
                normalized.append(seg)
                continue
            file_uri = str(getattr(seg, "file", "") or "")
            if not file_uri:
                normalized.append(seg)
                continue
            local_path = self._file_uri_to_path(file_uri)
            if local_path is None:
                normalized.append(seg)
                continue
            retry_path = await self._normalize_image_for_retry(local_path)
            if retry_path is None:
                normalized.append(seg)
                continue
            normalized.append(Image(self._to_file_uri(retry_path)))
            changed = True

        return normalized if changed else None

    async def _perturb_image_segments_for_retry(
        self, segs: list[BaseMessageComponent]
    ) -> list[BaseMessageComponent] | None:
        normalized: list[BaseMessageComponent] = []
        changed = False

        for seg in segs:
            if not isinstance(seg, Image):
                normalized.append(seg)
                continue
            file_uri = str(getattr(seg, "file", "") or "")
            if not file_uri:
                normalized.append(seg)
                continue
            local_path = self._file_uri_to_path(file_uri)
            if local_path is None:
                normalized.append(seg)
                continue
            retry_path = await self._perturb_image_for_retry(local_path)
            if retry_path is None:
                normalized.append(seg)
                continue
            normalized.append(Image(self._to_file_uri(retry_path)))
            changed = True

        return normalized if changed else None

    async def _send_preview_card(
        self,
        event: AstrMessageEvent,
        result: ParseResult,
        plan: dict,
    ):
        """
        发送预览卡片（独立消息）

        场景：
        - 只有一个重媒体
        - 未触发合并转发
        - 卡片作为“预览”，不与正文混合
        """
        if not plan["preview_card"]:
            return

        if image_path := await self.renderer.render_card(result):
            chain = [Image(self._to_file_uri(image_path))]
            try:
                await self._send_chain(event, chain)
                await self._archive_sent_chain(
                    event,
                    chain,
                    source="parser_preview_card",
                )
            except Exception as exc:
                logger.warning(
                    f"发送预览卡片失败，跳过预览继续发送正文: error={exc}, "
                    f"segments={self._collect_seg_meta(chain)}"
                )

    async def _build_segments(
        self,
        result: ParseResult,
        plan: dict,
    ) -> list[BaseMessageComponent]:
        """
        根据发送计划构建消息段列表

        这里负责：
        - 下载媒体
        - 转换为 AstrBot 消息组件
        """
        segs: list[BaseMessageComponent] = []

        # 合并转发时，卡片以内联形式作为一个消息段参与合并
        if plan["render_card"] and plan["force_merge"]:
            if image_path := await self.renderer.render_card(result):
                segs.append(Image(self._to_file_uri(image_path)))

        # 轻媒体处理
        for cont in plan["light"]:
            if isinstance(cont, TextContent):
                if cont.text:
                    segs.append(Plain(cont.text))
                continue

            try:
                path: Path = await cont.get_path()
            except (DownloadLimitException, ZeroSizeException):
                continue
            except DownloadException:
                if self.cfg.show_download_fail_tip:
                    segs.append(Plain("此项媒体下载失败"))
                continue

            match cont:
                case ImageContent():
                    segs.append(Image(self._to_file_uri(path)))
                case GraphicsContent() as g:
                    # OneBot/aiocqhttp 本地文件参数要求 file:// URI，而非裸本地路径。
                    segs.append(Image(self._to_file_uri(path)))
                    # GraphicsContent 允许携带补充文本
                    if g.text:
                        segs.append(Plain(g.text))
                    if g.alt:
                        segs.append(Plain(g.alt))

        # 重媒体处理
        for cont in plan["heavy"]:
            try:
                path: Path = await cont.get_path()
            except SizeLimitException:
                segs.append(Plain("此项媒体超过大小限制"))
                continue
            except DownloadException:
                if self.cfg.show_download_fail_tip:
                    segs.append(Plain("此项媒体下载失败"))
                continue

            match cont:
                case VideoContent() | DynamicContent():
                    segs.append(Video(self._to_file_uri(path)))
                case AudioContent():
                    segs.append(
                        File(name=path.name, file=self._to_file_uri(path))
                        if self.cfg.audio_to_file
                        else Record(self._to_file_uri(path))
                    )
                case FileContent():
                    segs.append(File(name=path.name, file=self._to_file_uri(path)))

        return segs

    def _merge_segments_if_needed(
        self,
        event: AstrMessageEvent,
        segs: list[BaseMessageComponent],
        force_merge: bool,
    ) -> list[BaseMessageComponent]:
        """
        根据策略决定是否将消息段合并为转发节点

        合并后的消息结构：
        - 每个原始消息段成为一个 Node
        - 统一使用机器人自身身份
        """
        if not force_merge or not segs:
            return segs

        nodes = Nodes([])
        self_id = event.get_self_id()
        sender_name = getattr(self.cfg, "merge_sender_name", None) or "狐米"

        for seg in segs:
            nodes.nodes.append(Node(uin=self_id, name=sender_name, content=[seg]))

        return [nodes]

    @staticmethod
    def _build_text_fallback(result: ParseResult) -> list[BaseMessageComponent]:
        body_lines: list[str] = []
        if result.text:
            body_lines.append(result.text)
        elif result.extra.get("info"):
            body_lines.append(str(result.extra["info"]))

        if not body_lines:
            return []

        lines: list[str] = []
        if result.header:
            lines.append(result.header)
        lines.extend(body_lines)

        text = "\n".join(line for line in lines if line).strip()
        return [Plain(text)] if text else []

    def _resolve_groups(self, result: ParseResult) -> list[SendGroup]:
        if result.send_groups:
            return result.send_groups
        return [SendGroup(contents=list(MessageSender._iter_contents(result)))]

    async def _send_group(
        self,
        event: AstrMessageEvent,
        result: ParseResult,
        group: SendGroup,
    ) -> bool:
        plan = self._build_send_plan(
            result,
            group.contents,
            force_merge_override=group.force_merge,
            render_card_override=group.render_card,
        )

        await self._send_preview_card(event, result, plan)

        segs = await self._build_segments(result, plan)
        segs = self._merge_segments_if_needed(event, segs, plan["force_merge"])
        if not segs:
            return False

        try:
            await self._send_chain(event, segs)
            await self._archive_sent_chain(
                event,
                segs,
                source="parser_send_group",
                raw_json_extra={"force_merge": bool(plan["force_merge"])},
            )
            await self._record_video_slice_cache(event, group)
            return True
        except Exception as e:
            if isinstance(e, TimeoutError):
                seg_meta = self._collect_seg_meta(segs)
                logger.error(f"发送解析结果失败： error={e}, segments={seg_meta}")
                return False
            retry_segs = await self._normalize_image_segments_for_retry(segs)
            if retry_segs is not None:
                try:
                    logger.warning(
                        f"发送图片失败，使用重编码图片重试: segments={self._collect_seg_meta(retry_segs)}"
                    )
                    await self._send_chain(event, retry_segs)
                    await self._archive_sent_chain(
                        event,
                        retry_segs,
                        source="parser_send_group",
                        raw_json_extra={
                            "force_merge": bool(plan["force_merge"]),
                            "image_retry": "normalized_reencode",
                        },
                    )
                    await self._record_video_slice_cache(event, group)
                    return True
                except Exception as retry_exc:
                    e = retry_exc
                    segs = retry_segs
            retry_segs = await self._perturb_image_segments_for_retry(segs)
            if retry_segs is not None:
                try:
                    logger.warning(
                        f"发送图片二次失败，使用扰动 PNG 重试: segments={self._collect_seg_meta(retry_segs)}"
                    )
                    await self._send_chain(event, retry_segs)
                    await self._archive_sent_chain(
                        event,
                        retry_segs,
                        source="parser_send_group",
                        raw_json_extra={
                            "force_merge": bool(plan["force_merge"]),
                            "image_retry": "perturbed_png",
                        },
                    )
                    await self._record_video_slice_cache(event, group)
                    return True
                except Exception as retry_exc:
                    e = retry_exc
                    segs = retry_segs
            seg_meta = self._collect_seg_meta(segs)
            logger.error(f"发送解析结果失败： error={e}, segments={seg_meta}")
            return False

    async def _record_video_slice_cache(self, event: AstrMessageEvent, group: SendGroup) -> None:
        if self.video_slice_cache is None:
            return
        await record_video_slice_cache_from_group(self.video_slice_cache, event=event, group=group)

    @staticmethod
    def _collect_seg_meta(segs: list[BaseMessageComponent]) -> list[dict[str, str]]:
        """提取消息段元信息，用于失败日志定位。"""
        meta: list[dict[str, str]] = []

        for seg in segs:
            item = {"type": seg.__class__.__name__}
            for attr in ("file", "path", "url"):
                value = getattr(seg, attr, None)
                if value:
                    item["media"] = str(value)
                    break
            meta.append(item)

        return meta

    async def send_parse_result(
        self,
        event: AstrMessageEvent,
        result: ParseResult,
    ) -> bool:
        """
        发送解析结果的统一入口

        执行顺序固定：
        1. 构建发送计划
        2. 发送预览卡片（如有）
        3. 构建消息段
        4. 必要时合并转发
        5. 最终发送
        """
        groups = self._resolve_groups(result)

        sent = False
        for group in groups:
            sent = await self._send_group(event, result, group) or sent

        if not sent:
            logger.warning("发送解析结果失败，已放弃发送，不再执行纯文本兜底")
            return False

        return True
