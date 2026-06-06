from __future__ import annotations

import json
import zoneinfo
from collections.abc import Mapping, MutableMapping
from pathlib import Path
from types import MappingProxyType, UnionType
from typing import Any, Union, get_args, get_origin, get_type_hints

from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.star.context import Context
from astrbot.core.star.star_tools import StarTools
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_path


class ConfigNode:
    """
    配置节点, 把 dict 变成强类型对象。

    规则：
    - schema 来自子类类型注解
    - 声明字段：读写，写回底层 dict
    - 未声明字段和下划线字段：仅挂载属性，不写回
    - 支持 ConfigNode 多层嵌套（lazy + cache）
    """

    _SCHEMA_CACHE: dict[type, dict[str, type]] = {}
    _FIELDS_CACHE: dict[type, set[str]] = {}

    @classmethod
    def _schema(cls) -> dict[str, type]:
        return cls._SCHEMA_CACHE.setdefault(cls, get_type_hints(cls))

    @classmethod
    def _fields(cls) -> set[str]:
        return cls._FIELDS_CACHE.setdefault(
            cls,
            {k for k in cls._schema() if not k.startswith("_")},
        )

    @staticmethod
    def _is_optional(tp: type) -> bool:
        if get_origin(tp) in (Union, UnionType):
            return type(None) in get_args(tp)
        return False

    def __init__(self, data: MutableMapping[str, Any]):
        object.__setattr__(self, "_data", data)
        object.__setattr__(self, "_children", {})
        for key, tp in self._schema().items():
            if key.startswith("_"):
                continue
            if key in data:
                continue
            if hasattr(self.__class__, key):
                continue
            if self._is_optional(tp):
                continue
            logger.warning(f"[config:{self.__class__.__name__}] 缺少字段: {key}")

    def __getattr__(self, key: str) -> Any:
        if key in self._fields():
            value = self._data.get(key)
            tp = self._schema().get(key)

            if isinstance(tp, type) and issubclass(tp, ConfigNode):
                children: dict[str, ConfigNode] = self.__dict__["_children"]
                if key not in children:
                    if not isinstance(value, MutableMapping):
                        raise TypeError(
                            f"[config:{self.__class__.__name__}] "
                            f"字段 {key} 期望 dict，实际是 {type(value).__name__}"
                        )
                    children[key] = tp(value)
                return children[key]

            return value

        if key in self.__dict__:
            return self.__dict__[key]

        raise AttributeError(key)

    def __setattr__(self, key: str, value: Any) -> None:
        if key in self._fields():
            self._data[key] = value
            return
        object.__setattr__(self, key, value)

    def raw_data(self) -> Mapping[str, Any]:
        """
        底层配置 dict 的只读视图
        """
        return MappingProxyType(self._data)

    def save_config(self) -> None:
        """
        保存配置到磁盘（仅允许在根节点调用）
        """
        if not isinstance(self._data, AstrBotConfig):
            raise RuntimeError(
                f"{self.__class__.__name__}.save_config() 只能在根配置节点上调用"
            )
        self._data.save_config()


class ConfigNodeContainer:
    """
    配置节点容器, 把 list 的 dict 变成 dict 的对象集合。

    - nodes: list[dict[str, Any]]
    - item_cls 用于包装 dict 成强类型节点
    - key_name 作为属性名访问, 默认为 "__template_key"
    """

    def __init__(
        self,
        nodes: list[dict[str, Any]],
        item_cls: type[ConfigNode],
        key_name="__template_key",
    ):
        self._nodes: dict[str, ConfigNode] = {}
        for node in nodes:
            key = node.get(key_name)
            if not key:
                logger.warning(f"[node] 缺少 {key_name}，已跳过")
                continue
            if key in self._nodes:
                logger.warning(f"[node] {key} 重复配置，已覆盖")
            self._nodes[key] = item_cls(node)

    def __getattr__(self, name: str) -> ConfigNode:
        if name in self._nodes:
            return self._nodes[name]
        raise AttributeError(name)

    def __iter__(self):
        return iter(self._nodes.values())

    def keys(self):
        return self._nodes.keys()

    def items(self):
        return self._nodes.items()


# ================ 插件自定义配置 ==================


class ParserItem(ConfigNode):
    __template_key: str
    enable: bool
    use_proxy: bool
    cookies: str | None
    x_api_enable: bool | None
    x_api_auth_mode: str | None
    x_api_bearer_token: str | None
    x_api_user_bearer_token: str | None
    x_api_timeout_seconds: int | None
    x_api_cache_ttl_seconds: int | None
    show_body_text: bool | None
    video_send_mode: str | None
    video_codecs: str | None
    video_quality: str | None

    @property
    def name(self) -> str:
        return self._data.get("__template_key")


class ParserConfig(ConfigNodeContainer):
    acfun: ParserItem
    bilibili: ParserItem
    douyin: ParserItem
    instagram: ParserItem
    kuaishou: ParserItem
    ncm: ParserItem
    nga: ParserItem
    tiktok: ParserItem
    twitter: ParserItem
    weibo: ParserItem
    xiaoheihe: ParserItem
    zhihu: ParserItem
    xhs: ParserItem
    youtube: ParserItem

    def __init__(self, nodes: list[dict[str, Any]]):
        super().__init__(nodes, item_cls=ParserItem)

    def platforms(self) -> list[str]:
        return list(self._nodes.keys())

    def enabled_platforms(self) -> list[str]:
        return [k for k, v in self._nodes.items() if getattr(v, "enable", True)]


class PluginConfig(ConfigNode):
    whitelist: str

    arbiter: bool
    debounce_interval: int

    source_max_size: int
    source_max_minute: int

    audio_to_file: bool
    single_heavy_render_card: bool
    forward_threshold: int
    merge_sender_name: str | None

    show_download_fail_tip: bool
    download_timeout: int
    download_retry_times: int
    common_timeout: int

    proxy: str | None
    parser_video_slice_controller_ids: str | list[str] | None
    parser_video_slice_max_duration_sec: int | None
    parser_video_slice_timeout_sec: int | None

    clean_cron: str

    parsers_template: list[dict[str, Any]]

    _plugin_name = "astrbot_plugin_parser"

    @staticmethod
    def ensure_dir(path: Path) -> Path:
        real_path = path.resolve(strict=False) if path.is_symlink() else path
        real_path.mkdir(parents=True, exist_ok=True)
        return real_path

    def __init__(self, config: AstrBotConfig, context: Context):
        super().__init__(config)
        self.context = context
        self.admins_id = self.context.get_config().get("admins_id", [])

        # ---------- 内置配置 ----------
        self.emoji_cdn = "https://cdn.jsdelivr.net/npm/emoji-datasource-facebook@14.0.0/img/facebook/64/"
        self.emoji_style = "FACEBOOK"  # 可选：APPLE、FACEBOOK、GOOGLE、TWITTER

        # ---------- 派生字段 ----------
        self.proxy = self.proxy or None
        self.max_duration = self.source_max_minute * 60
        self.max_size = self.source_max_size * 1024 * 1024
        self.group_user_whitelist = self._cfg_group_user_whitelist(self.whitelist)

        tz = context.get_config().get("timezone")
        self.timezone = (
            zoneinfo.ZoneInfo(tz) if tz else zoneinfo.ZoneInfo("Asia/Shanghai")
        )

        # ---------- 路径 ----------
        self.data_dir = StarTools.get_data_dir(self._plugin_name)
        self.plugin_dir = Path(get_astrbot_plugin_path()) / self._plugin_name
        self.cache_dir = self.data_dir / "cache"
        self.ensure_dir(self.cache_dir)
        self.cookie_dir = self.data_dir / "cookies"
        self.ensure_dir(self.cookie_dir)
        self.default_template_file = self.plugin_dir / "default_template.json"

        # ---------- Parser ----------
        if not self.parsers_template:
            self.parsers_template[:] = self.load_parser_template(
                self.default_template_file
            )
            self.save_config()

        self.parser = ParserConfig(self.parsers_template)

    @staticmethod
    def load_parser_template(file: Path) -> list[dict[str, Any]]:
        try:
            with file.open(encoding="utf-8-sig") as f:
                template = json.loads(f.read())
                logger.info(f"[parser] 加载模板成功: {file}")
                return template
        except Exception as e:
            logger.error(f"[parser] 加载模板失败: {e}")
            return []

    @staticmethod
    def _cfg_group_user_whitelist(value: Any) -> dict[str, set[str]]:
        raw = value
        if isinstance(raw, str):
            text = raw.strip()
            if not text:
                return {}
            try:
                raw = PluginConfig._loads_json_with_comments(text)
            except Exception:
                logger.warning("[parser] whitelist JSON 解析失败，已回退为空")
                return {}

        # 兼容旧版 list[str] 全局白名单；升级后建议改成按 group_id 配置。
        if isinstance(raw, list):
            normalized = PluginConfig._normalize_whitelist_users(raw)
            return {"*": normalized} if normalized else {}

        if not isinstance(raw, dict):
            return {}

        result: dict[str, set[str]] = {}
        for group_id, users in raw.items():
            gid = str(group_id or "").strip()
            if not gid:
                continue
            normalized = PluginConfig._normalize_whitelist_users(users)
            if normalized:
                result[gid] = normalized
        return result

    @staticmethod
    def _normalize_whitelist_users(users: Any) -> set[str]:
        if isinstance(users, str):
            users = [users]
        if not isinstance(users, list):
            return set()
        return {str(user_id).strip() for user_id in users if str(user_id).strip()}

    @staticmethod
    def _loads_json_with_comments(text: str) -> Any:
        return json.loads(PluginConfig._strip_json_comments(text))

    @staticmethod
    def _strip_json_comments(text: str) -> str:
        chars: list[str] = []
        in_string = False
        string_quote = ""
        escaped = False
        in_line_comment = False
        in_block_comment = False
        i = 0

        while i < len(text):
            ch = text[i]
            next_ch = text[i + 1] if i + 1 < len(text) else ""

            if in_line_comment:
                if ch == "\n":
                    in_line_comment = False
                    chars.append(ch)
                i += 1
                continue

            if in_block_comment:
                if ch == "*" and next_ch == "/":
                    in_block_comment = False
                    i += 2
                    continue
                if ch == "\n":
                    chars.append(ch)
                i += 1
                continue

            if in_string:
                chars.append(ch)
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == string_quote:
                    in_string = False
                i += 1
                continue

            if ch in ('"', "'"):
                in_string = True
                string_quote = ch
                chars.append(ch)
                i += 1
                continue

            if ch == "/" and next_ch == "/":
                in_line_comment = True
                i += 2
                continue

            if ch == "/" and next_ch == "*":
                in_block_comment = True
                i += 2
                continue

            if ch == "#":
                in_line_comment = True
                i += 1
                continue

            chars.append(ch)
            i += 1

        return "".join(chars)

    @staticmethod
    def should_apply_whitelist(platform_name: str) -> bool:
        return platform_name in {"youtube", "twitter"}

    def is_admin_user(self, user_id: str) -> bool:
        uid = str(user_id or "").strip()
        if not uid:
            return False
        return uid in {
            str(admin_id).strip()
            for admin_id in getattr(self, "admins_id", [])
            if str(admin_id).strip()
        }

    def whitelist_data(self) -> dict[str, list[str]]:
        return {
            str(group_id): sorted(str(user_id) for user_id in users)
            for group_id, users in self.group_user_whitelist.items()
            if users
        }

    def _save_whitelist_data(self, data: dict[str, list[str]]) -> None:
        cleaned = {
            str(group_id): sorted(
                {str(user_id).strip() for user_id in users if str(user_id).strip()}
            )
            for group_id, users in data.items()
            if str(group_id).strip()
        }
        cleaned = {group_id: users for group_id, users in cleaned.items() if users}
        self.whitelist = json.dumps(cleaned, ensure_ascii=False, sort_keys=True)
        self.group_user_whitelist = self._cfg_group_user_whitelist(self.whitelist)
        self.save_config()

    def add_whitelist_user(self, group_id: str, user_id: str) -> bool:
        gid = str(group_id or "").strip()
        uid = str(user_id or "").strip()
        if not gid or not uid:
            return False

        data = self.whitelist_data()
        users = set(data.get(gid, []))
        if uid in users:
            return False
        users.add(uid)
        data[gid] = sorted(users)
        self._save_whitelist_data(data)
        return True

    def remove_whitelist_user(self, group_id: str, user_id: str) -> bool:
        gid = str(group_id or "").strip()
        uid = str(user_id or "").strip()
        data = self.whitelist_data()
        users = set(data.get(gid, []))
        if not gid or not uid or uid not in users:
            return False

        users.remove(uid)
        if users:
            data[gid] = sorted(users)
        else:
            data.pop(gid, None)
        self._save_whitelist_data(data)
        return True

    def clear_whitelist_group(self, group_id: str) -> bool:
        gid = str(group_id or "").strip()
        data = self.whitelist_data()
        if not gid or gid not in data:
            return False

        data.pop(gid, None)
        self._save_whitelist_data(data)
        return True

    def is_whitelist_allowed(self, group_id: str, user_id: str) -> bool:
        uid = str(user_id or "").strip()
        if self.is_admin_user(uid):
            return True
        if not self.group_user_whitelist:
            return True

        gid = str(group_id or "").strip()
        if not uid:
            return False
        if not gid:
            return True

        allowed_users = self.group_user_whitelist.get(gid)
        if allowed_users is None:
            allowed_users = self.group_user_whitelist.get("*")
        if allowed_users is None:
            return False
        return uid in allowed_users
