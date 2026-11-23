"""通知器：经 Telegram Bot API 向用户私聊发消息。

边界（设计 6.4 / 5.2）：
- 网关只"发"不"收"：本模块只调用一个出站接口 sendMessage；一个 Bot 只能有一个消费者，
  收消息由 OpenClaw 负责，这里绝不碰收消息和 webhook 相关的接口。
- 文本发出前一律经过 sanitize（凭据脱敏 + 限长）；日志里只存文本哈希。
- 发送失败不抛异常：进待重试队列，最多重试 5 次，之后丢弃并记录。
- 限流：即时提醒在滑动 1 小时窗口内最多 max_alerts_per_hour 条，超出的合并，等窗口有空位后汇成一条。
- dry-run：只记录"本来要发什么"，不发 HTTP 请求。
"""
import hashlib
import time
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

import httpx

from ..config import Settings
from ..redact import sanitize
from .store import MonitorStore

MAX_TEXT = 3500           # Telegram 单条上限 4096，留余量
MAX_ATTEMPTS = 5
WINDOW_S = 3600
MERGED_LINES = 10


class Notifier:
    def __init__(self, settings: Settings, store: MonitorStore, client: Optional[httpx.Client] = None,
                 clock: Callable[[], float] = time.time):
        self._s, self._store, self._clock = settings, store, clock
        self._client = client or httpx.Client(timeout=10.0)
        self._sent_times: List[float] = []      # 滑动窗口内已占用名额的时间戳
        self._merged: List[Dict[str, str]] = []  # 被限流合并的提醒（内存中）

    def __repr__(self) -> str:
        return f"Notifier(mode={self._s.notify_mode})"

    # ---- 内部 ----
    def _ts(self) -> str:
        return datetime.fromtimestamp(self._clock(), timezone.utc).isoformat(timespec="seconds")

    def _within_limit(self) -> bool:
        cutoff = self._clock() - WINDOW_S
        self._sent_times = [t for t in self._sent_times if t > cutoff]
        return len(self._sent_times) < self._s.max_alerts_per_hour

    def _send(self, text: str) -> bool:
        """真正调用 sendMessage。任何失败都返回 False，且不让异常文本（可能含 URL/token）外泄。"""
        try:
            with open(self._s.telegram_token_file, encoding="utf-8") as f:
                token = f.read().strip()
            r = self._client.post(
                f"{self._s.telegram_api_base}/bot{token}/sendMessage",
                json={"chat_id": self._s.telegram_chat_id, "text": text, "disable_web_page_preview": True},
                timeout=10.0,
            )
            return r.status_code == 200 and bool(r.json().get("ok"))
        except Exception:  # noqa: BLE001  网络错误、读文件失败、非 JSON 都按失败处理
            return False

    def _deliver(self, kind: str, key: str, text: str) -> str:
        h = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        if self._s.notify_mode != "live":
            self._store.log_notification(self._ts(), kind, key, h, "dry-run")
            return "dry-run"
        if self._send(text):
            self._store.log_notification(self._ts(), kind, key, h, "sent")
            return "sent"
        self._store.enqueue_notification(self._ts(), kind, key, text)
        self._store.log_notification(self._ts(), kind, key, h, "queued")
        return "queued"

    # ---- 对外 ----
    def notify(self, kind: str, key: str, text: str, urgent: bool = True) -> str:
        """返回 "sent" | "dry-run" | "queued" | "merged"。"""
        clean = sanitize(self._s.notify_prefix + text, MAX_TEXT)[0]
        if urgent:
            if not self._within_limit():
                self._merged.append({"kind": kind, "key": key, "text": clean})
                self._store.log_notification(self._ts(), kind, key,
                                             hashlib.sha256(clean.encode("utf-8")).hexdigest()[:16], "merged")
                return "merged"
            self._sent_times.append(self._clock())
        return self._deliver(kind, key, clean)

    def delivers(self) -> bool:
        """是否真的会把消息发给用户。dry-run 下为 False：审批码这类"必须送达"的消息不能当作发送成功。"""
        return self._s.notify_mode == "live"

    def send_now(self, kind: str, key: str, text: str) -> bool:
        """立即发送，不入重试队列，不受限流约束（审批码这类消息：发不出去就应作废，不能在库里排队）。

        dry-run 模式不发 HTTP，返回 True（只记日志）。
        """
        clean = sanitize(self._s.notify_prefix + text, MAX_TEXT)[0]
        h = hashlib.sha256(clean.encode("utf-8")).hexdigest()[:16]
        if self._s.notify_mode != "live":
            self._store.log_notification(self._ts(), kind, key, h, "dry-run")
            return True
        ok = self._send(clean)
        self._store.log_notification(self._ts(), kind, key, h, "sent" if ok else "failed")
        return ok

    def flush_pending(self) -> int:
        """重试发送失败的通知；返回本次成功条数。"""
        ok = 0
        for n in self._store.pending_notifications():
            h = hashlib.sha256(n["text"].encode("utf-8")).hexdigest()[:16]
            if self._send(n["text"]):
                self._store.mark_delivered(n["id"])
                self._store.log_notification(self._ts(), n["kind"], n["key"], h, "sent")
                ok += 1
            elif self._store.bump_attempt(n["id"]) >= MAX_ATTEMPTS:
                self._store.mark_delivered(n["id"])   # 放弃：清掉文本，不再重试
                self._store.log_notification(self._ts(), n["kind"], n["key"], h, "dropped")
        return ok

    def flush_merged(self) -> int:
        """窗口有空位时，把被限流合并的提醒汇成一条发出；返回合并条数（没发则 0）。"""
        if not self._merged or not self._within_limit():
            return 0
        items, self._merged = self._merged, []
        lines = [f"- {i['text'].splitlines()[0] if i['text'] else ''}" for i in items[:MERGED_LINES]]
        more = len(items) - MERGED_LINES
        text = f"另有 {len(items)} 条提醒已合并：\n" + "\n".join(lines) + (f"\n……还有 {more} 条" if more > 0 else "")
        self._sent_times.append(self._clock())
        self._deliver("merged", "merged", sanitize(text, MAX_TEXT)[0])
        return len(items)

    def merged_count(self) -> int:
        return len(self._merged)
