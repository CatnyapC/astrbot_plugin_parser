# debounce.py

import time

from .config import PluginConfig


class Debouncer:
    """
    会话级防抖器
    - 支持 link 防抖
    - 支持 resource_id 防抖
    """

    def __init__(self, config: PluginConfig):
        self.cfg = config
        self.interval = self.cfg.debounce_interval
        self._cache: dict[str, dict[str, float]] = {}  # {session: {key: ts}}

    def _cleanup(self, session: str) -> dict[str, float]:
        # 禁用
        if self.interval <= 0:
            return {}

        now = time.time()
        bucket = self._cache.setdefault(session, {})

        expire = now - self.interval
        for k, ts in list(bucket.items()):
            if ts < expire:
                bucket.pop(k, None)
        return bucket

    def _hit(self, session: str, key: str) -> bool:
        bucket = self._cleanup(session)
        if self.interval <= 0:
            return False
        now = time.time()

        if key in bucket:
            return True

        bucket[key] = now
        return False

    def _check(self, session: str, key: str) -> bool:
        bucket = self._cleanup(session)
        if self.interval <= 0:
            return False
        return key in bucket

    def _mark(self, session: str, key: str) -> None:
        bucket = self._cleanup(session)
        if self.interval <= 0:
            return
        bucket[key] = time.time()

    def hit_link(self, session: str, link: str) -> bool:
        """基于 link 的防抖"""
        return self._hit(session, f"link:{link}")

    def check_resource(self, session: str, resource_id: str) -> bool:
        """检查资源 ID 是否仍在防抖窗口内，不写入缓存"""
        return self._check(session, f"res:{resource_id}")

    def mark_resource(self, session: str, resource_id: str) -> None:
        """在资源发送成功后写入防抖缓存"""
        self._mark(session, f"res:{resource_id}")
