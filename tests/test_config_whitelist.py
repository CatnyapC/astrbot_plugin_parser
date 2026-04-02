import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.config import PluginConfig


def build_cfg(group_user_whitelist: dict[str, set[str]]) -> PluginConfig:
    cfg = object.__new__(PluginConfig)
    cfg.group_user_whitelist = group_user_whitelist
    return cfg


def test_cfg_group_user_whitelist_parses_json_string():
    parsed = PluginConfig._cfg_group_user_whitelist(
        '{"123456":["10001","10002"],"654321":["20001"]}'
    )

    assert parsed == {
        "123456": {"10001", "10002"},
        "654321": {"20001"},
    }


def test_cfg_group_user_whitelist_parses_commented_json_string():
    parsed = PluginConfig._cfg_group_user_whitelist(
        """
        {
            // 群一
            "123456": ["10001", "10002"], # 行尾注释
            /* 群二 */
            "654321": ["20001"]
        }
        """
    )

    assert parsed == {
        "123456": {"10001", "10002"},
        "654321": {"20001"},
    }


def test_cfg_group_user_whitelist_keeps_legacy_list_as_global_fallback():
    parsed = PluginConfig._cfg_group_user_whitelist(["10001", "10002"])

    assert parsed == {"*": {"10001", "10002"}}


def test_is_whitelist_allowed_checks_current_group_and_user():
    cfg = build_cfg({"123456": {"10001", "10002"}})

    assert cfg.is_whitelist_allowed("123456", "10001") is True
    assert cfg.is_whitelist_allowed("123456", "99999") is False
    assert cfg.is_whitelist_allowed("654321", "10001") is False


def test_is_whitelist_allowed_allows_private_when_whitelist_exists():
    cfg = build_cfg({"123456": {"10001"}})

    assert cfg.is_whitelist_allowed("", "99999") is True


def test_should_apply_whitelist_only_for_youtube_and_twitter():
    assert PluginConfig.should_apply_whitelist("youtube") is True
    assert PluginConfig.should_apply_whitelist("twitter") is True
    assert PluginConfig.should_apply_whitelist("bilibili") is False
    assert PluginConfig.should_apply_whitelist("douyin") is False
