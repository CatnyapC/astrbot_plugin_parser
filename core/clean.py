import asyncio
import shutil
import time
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from astrbot.api import logger

from .config import PluginConfig


class CacheCleaner:
    """
    每天固定时间自动清理插件缓存目录的调度器封装。
    """

    JOBNAME = "CacheCleaner"

    def __init__(
        self,
        config: PluginConfig,
        *,
        video_slice_cache: Any | None = None,
        start_scheduler: bool = True,
    ):
        self.cfg = config
        self.video_slice_cache = video_slice_cache
        self.scheduler = AsyncIOScheduler(timezone=self.cfg.timezone)
        if start_scheduler:
            self.scheduler.start()

        self.register_task()

        logger.info(f"{self.JOBNAME} 已启动，任务周期：{self.cfg.clean_cron}")

    def register_task(self):
        try:
            self.trigger = CronTrigger.from_crontab(self.cfg.clean_cron)
            self.scheduler.add_job(
                func=self._clean_plugin_cache,
                trigger=self.trigger,
                name=f"{self.JOBNAME}_scheduler",
                max_instances=1,
            )
        except Exception as e:
            logger.error(f"[{self.JOBNAME}] Cron 格式错误：{e}")

    async def _clean_plugin_cache(self) -> None:
        """删除并重建缓存目录"""
        loop = asyncio.get_running_loop()
        try:
            cache_dir = self.cfg.ensure_dir(self.cfg.cache_dir)
            await loop.run_in_executor(None, shutil.rmtree, cache_dir)
            self.cfg.ensure_dir(self.cfg.cache_dir)
            if self.video_slice_cache is not None:
                removed = self.video_slice_cache.prune(older_than=time.time())
                logger.info(f"Video slice cache index pruned: removed={removed}")
            logger.info("Cache directory cleaned and recreated.")
        except Exception:
            logger.exception("Error while cleaning cache directory.")

    async def stop(self):
        self.scheduler.remove_all_jobs()
        logger.info(f"[{self.JOBNAME}] 已停止")
