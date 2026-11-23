"""监视器状态库（SQLite，标准库）：告警状态、纳管名单、通知日志与待重试队列、键值。

不保存通知全文（只存哈希），避免把会话内容落盘。所有 SQL 参数化；时间由调用方传入（UTC ISO8601）。
"""
import sqlite3
import threading
from typing import Dict, Iterable, List, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts(key TEXT PRIMARY KEY, bad INTEGER NOT NULL, since TEXT NOT NULL, notified INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS managed(handle TEXT PRIMARY KEY, source TEXT NOT NULL, since TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS notif_log(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, kind TEXT NOT NULL, key TEXT NOT NULL, text_hash TEXT NOT NULL, outcome TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS notif_queue(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, kind TEXT NOT NULL, key TEXT NOT NULL, text TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, delivered INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS approvals(id TEXT PRIMARY KEY, kind TEXT NOT NULL, summary TEXT NOT NULL, payload_json TEXT NOT NULL, executor_key TEXT NOT NULL, code_hmac TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, expires_at TEXT NOT NULL, result_json TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS approval_fail(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL, decision TEXT NOT NULL, rules TEXT NOT NULL, detail TEXT NOT NULL, text_hash TEXT NOT NULL);
"""


class MonitorStore:
    def __init__(self, path: str):
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        if path != ":memory:":
            self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _exec(self, sql: str, params: Iterable = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._db.execute(sql, tuple(params))
            self._db.commit()
            return cur

    def _all(self, sql: str, params: Iterable = ()) -> List[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, tuple(params)).fetchall()

    # ---- 告警状态 ----
    def get_alert(self, key: str) -> Optional[Dict]:
        rows = self._all("SELECT * FROM alerts WHERE key=?", (key,))
        return self._alert(rows[0]) if rows else None

    @staticmethod
    def _alert(r: sqlite3.Row) -> Dict:
        return {"key": r["key"], "bad": bool(r["bad"]), "since": r["since"], "notified": bool(r["notified"])}

    def set_alert(self, key: str, bad: bool, since: str, notified: bool) -> None:
        self._exec("INSERT INTO alerts(key,bad,since,notified) VALUES(?,?,?,?) "
                   "ON CONFLICT(key) DO UPDATE SET bad=excluded.bad, since=excluded.since, notified=excluded.notified",
                   (key, int(bad), since, int(notified)))

    def delete_alert(self, key: str) -> None:
        self._exec("DELETE FROM alerts WHERE key=?", (key,))

    def all_alerts(self) -> List[Dict]:
        return [self._alert(r) for r in self._all("SELECT * FROM alerts ORDER BY key")]

    # ---- 纳管名单 ----
    def managed_add(self, handle: str, source: str, now: str) -> None:
        self._exec("INSERT OR IGNORE INTO managed(handle,source,since) VALUES(?,?,?)", (handle, source, now))

    def managed_remove(self, handle: str) -> None:
        self._exec("DELETE FROM managed WHERE handle=?", (handle,))

    def is_managed(self, handle: str) -> bool:
        return bool(self._all("SELECT 1 FROM managed WHERE handle=?", (handle,)))

    def managed_list(self) -> List[Dict]:
        return [dict(r) for r in self._all("SELECT handle,source,since FROM managed ORDER BY since,handle")]

    def managed_prune(self, alive: Iterable[str]) -> List[str]:
        alive = set(alive)
        dead = [m["handle"] for m in self.managed_list() if m["handle"] not in alive]
        for h in dead:
            self.managed_remove(h)
        return dead

    # ---- 通知日志与待重试队列 ----
    def log_notification(self, ts: str, kind: str, key: str, text_hash: str, outcome: str) -> None:
        self._exec("INSERT INTO notif_log(ts,kind,key,text_hash,outcome) VALUES(?,?,?,?,?)", (ts, kind, key, text_hash, outcome))

    def recent_log(self, limit: int = 50) -> List[Dict]:
        rows = self._all("SELECT ts,kind,key,text_hash,outcome FROM notif_log ORDER BY id DESC LIMIT ?", (int(limit),))
        return [dict(r) for r in rows]

    def enqueue_notification(self, ts: str, kind: str, key: str, text: str) -> int:
        return int(self._exec("INSERT INTO notif_queue(ts,kind,key,text) VALUES(?,?,?,?)", (ts, kind, key, text)).lastrowid)

    def pending_notifications(self) -> List[Dict]:
        return [dict(r) for r in self._all("SELECT * FROM notif_queue WHERE delivered=0 ORDER BY id")]

    def bump_attempt(self, nid: int) -> int:
        self._exec("UPDATE notif_queue SET attempts=attempts+1 WHERE id=?", (nid,))
        return int(self._all("SELECT attempts FROM notif_queue WHERE id=?", (nid,))[0]["attempts"])

    def mark_delivered(self, nid: int) -> None:
        self._exec("UPDATE notif_queue SET delivered=1, text='' WHERE id=?", (nid,))

    # ---- 键值 ----
    def kv_get(self, k: str) -> Optional[str]:
        rows = self._all("SELECT v FROM kv WHERE k=?", (k,))
        return rows[0]["v"] if rows else None

    def kv_set(self, k: str, v: str) -> None:
        self._exec("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))

    # ---- 审批单 ----
    def approval_insert(self, aid: str, kind: str, summary: str, payload_json: str, executor_key: str,
                        code_hmac: str, created_at: str, expires_at: str) -> None:
        self._exec("INSERT INTO approvals(id,kind,summary,payload_json,executor_key,code_hmac,status,created_at,expires_at) "
                   "VALUES(?,?,?,?,?,?,'pending',?,?)",
                   (aid, kind, summary, payload_json, executor_key, code_hmac, created_at, expires_at))

    def approval_get(self, aid: str) -> Optional[Dict]:
        rows = self._all("SELECT * FROM approvals WHERE id=?", (aid,))
        return dict(rows[0]) if rows else None

    def approval_claim(self, aid: str, from_status: str, to_status: str) -> bool:
        """原子状态迁移：只有一个调用者能把 from_status 改成 to_status。"""
        return self._exec("UPDATE approvals SET status=? WHERE id=? AND status=?", (to_status, aid, from_status)).rowcount == 1

    def approval_update(self, aid: str, status: Optional[str] = None, result_json: Optional[str] = None,
                        clear_code: bool = False) -> None:
        if status is not None:
            self._exec("UPDATE approvals SET status=? WHERE id=?", (status, aid))
        if result_json is not None:
            self._exec("UPDATE approvals SET result_json=? WHERE id=?", (result_json, aid))
        if clear_code:
            self._exec("UPDATE approvals SET code_hmac='' WHERE id=?", (aid,))

    def approval_bump_attempts(self, aid: str) -> int:
        self._exec("UPDATE approvals SET attempts=attempts+1 WHERE id=?", (aid,))
        return int(self._all("SELECT attempts FROM approvals WHERE id=?", (aid,))[0]["attempts"])

    def approvals_pending(self, now_iso: str) -> List[Dict]:
        rows = self._all("SELECT id,kind,summary,expires_at FROM approvals WHERE status='pending' AND expires_at>? ORDER BY created_at", (now_iso,))
        return [dict(r) for r in rows]

    def approval_fail_add(self, ts: str) -> None:
        self._exec("INSERT INTO approval_fail(ts) VALUES(?)", (ts,))

    def approval_fail_count(self, since: str) -> int:
        return int(self._all("SELECT COUNT(*) c FROM approval_fail WHERE ts>=?", (since,))[0]["c"])

    # ---- 审计 ----
    def audit_insert(self, ts: str, actor: str, action: str, target: str, decision: str, rules: str,
                     detail: str, text_hash: str) -> int:
        return int(self._exec("INSERT INTO audit(ts,actor,action,target,decision,rules,detail,text_hash) VALUES(?,?,?,?,?,?,?,?)",
                              (ts, actor, action, target, decision, rules, detail, text_hash)).lastrowid)

    def audit_recent(self, limit: int, since: Optional[str]) -> List[Dict]:
        if since:
            rows = self._all("SELECT * FROM audit WHERE ts>=? ORDER BY id DESC LIMIT ?", (since, int(limit)))
        else:
            rows = self._all("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (int(limit),))
        return [dict(r) for r in rows]

    def audit_count(self, action: str, decision: str, since: str) -> int:
        return int(self._all("SELECT COUNT(*) c FROM audit WHERE action=? AND decision=? AND ts>=?", (action, decision, since))[0]["c"])

    def audit_rows(self, action: str, decision: str, since: str) -> List[Dict]:
        return [dict(r) for r in self._all("SELECT * FROM audit WHERE action=? AND decision=? AND ts>=? ORDER BY id", (action, decision, since))]

    def dump_text(self) -> str:
        """仅供测试：把所有表里的全部文本拼起来，用来断言某个字符串（审批码）从未被存下来。"""
        out = []
        for t in ("alerts", "managed", "notif_log", "notif_queue", "kv", "approvals", "approval_fail", "audit"):
            out.append(repr([tuple(r) for r in self._all(f"SELECT * FROM {t}")]))
        return "\n".join(out)
