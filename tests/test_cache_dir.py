import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.config import PluginConfig


def test_ensure_dir_recreates_dangling_symlink_target(tmp_path):
    target = tmp_path / "downloads_cache"
    link = tmp_path / "cache"
    link.symlink_to(target)

    ensured = PluginConfig.ensure_dir(link)

    assert ensured == target
    assert target.is_dir()
    assert link.is_symlink()
