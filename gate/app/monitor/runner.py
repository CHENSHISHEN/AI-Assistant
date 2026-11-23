"""监视器调度：周期性执行应用监视与会话监视，异常彼此隔离。

采集是同步代码（子进程 + 同步 HTTP），所以放到线程里跑，不阻塞 FastAPI 事件循环。
异常只记录类型名（异常文本可能带 URL 或内容），写进快照的 errors。
"""
import asyncio
import contextlib
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional

from .store import MonitorStore
from .timefmt import iso_utc


class MonitorRunner:
    def __init__(self, apps, sessions, notifier, store: MonitorStore, interval_s: float, audit=None,
                 digest_s: int = 3600, now: Optional[Callable[[], datetime]] = None):
        self.apps, self.sessions, self.notifier, self.store = apps, sessions, notifier, store
        self.interval_s = interval_s
        self.audit, self.digest_s = audit, digest_s
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.last: Optional[Dict] = None
        self._task: Optional[asyncio.Task] = None

    def run_once(self) -> Dict:
        snap: Dict = {"started_at": iso_utc(datetime.now(timezone.utc)), "apps": None, "sessions": None, "errors": []}
        errors: List[str] = snap["errors"]
        for name, step in (("apps", self.apps), ("sessions", self.sessions)):
            if step is None:
                continue
            try:
                snap[name] = step.poll()
            except Exception as e:  # noqa: BLE001  任一步骤失败都不影响其他步骤
                errors.append(f"{name}: {type(e).__name__}")
        if self.audit is not None:
            try:
                self.digest_auto_confirms()
            except Exception as e:  # noqa: BLE001
                errors.append(f"digest_auto_confirms: {type(e).__name__}")
        for name in ("flush_pending", "flush_merged"):
            try:
                getattr(self.notifier, name)()
            except Exception as e:  # noqa: BLE001
                errors.append(f"{name}: {type(e).__name__}")
        snap["finished_at"] = iso_utc(datetime.now(timezone.utc))
        self.last = snap
        return snap

    def digest_auto_confirms(self) -> int:
        """每个窗口把期间的自动确认（审计 decision=auto）汇成一条非紧急通知（设计 7 节：每小时汇总，不逐条推送）。

        窗口起点存在库里，重启不重复；没有自动确认就不发。返回汇总的条数。
        """
        now = self._now()
        last = self.store.kv_get("auto_digest_last")
        if last is None:
            self.store.kv_set("auto_digest_last", iso_utc(now))
            return 0
        if now - datetime.fromisoformat(last) < timedelta(seconds=self.digest_s):
            return 0
        rows = self.store.audit_rows("reply", "auto", last)
        self.store.kv_set("auto_digest_last", iso_utc(now))
        if not rows:
            return 0
        per: Dict[str, List[str]] = {}
        for r in rows:
            per.setdefault(r["target"], []).append(r["detail"].replace("user_requested: ", ""))
        lines = []
        for handle, texts in list(per.items())[:10]:
            uniq = "、".join(dict.fromkeys(t[:30] for t in texts))
            lines.append(f"- 会话 …{handle[-6:]}：{len(texts)} 次（回复：{uniq}）")
        minutes = max(1, int((now - datetime.fromisoformat(last)).total_seconds() // 60))
        self.notifier.notify("auto-digest", "auto-digest",
                             f"过去 {minutes} 分钟自动确认了 {len(rows)} 次：\n" + "\n".join(lines), urgent=False)
        return len(rows)

    async def _loop(self) -> None:
        while True:
            await asyncio.to_thread(self.run_once)
            await asyncio.sleep(self.interval_s)

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
