"""Orca 命令行客户端：只读，固定子命令。

- 每个公共方法对应一条写死的 argv 模板；没有"传入任意 argv"的公共方法。
- 子进程用 shell=False；用户可控的值（handle、搜索词）要么经正则校验，
  要么以 `--query=<原文>` 单个 argv 元素传入（真实 CLI 实测：这是唯一不会把以 - 开头的搜索词
  当成选项的写法；`--query <x>` 和 `--` 分隔符都会报 Unknown flag）。
- 真实 CLI 在失败时返回码为 1 但 stdout 仍是 JSON 信封，所以要先解析信封再看返回码。
"""
import json
import re
import subprocess
from typing import Callable, List, Optional, Tuple

Runner = Callable[[List[str]], Tuple[int, str, str]]

_HANDLE_RE = re.compile(r"^term_[0-9A-Za-z-]{8,64}$")  # 真实格式：term_<uuid>
_UNAVAILABLE_CODES = {"runtime_unavailable", "remote_runtime_unavailable", "runtime_not_ready", "runtime_timeout"}
_NOT_FOUND_CODES = {"terminal_handle_stale", "terminal_not_found", "terminal_handle_not_found"}


def valid_handle(handle: object) -> bool:
    return isinstance(handle, str) and bool(_HANDLE_RE.fullmatch(handle))


class OrcaError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code, self.message = code, message


class OrcaUnavailable(OrcaError):
    """Orca 运行时连不上、超时、命令行缺失。"""


class OrcaSearchDisabled(OrcaUnavailable):
    """Orca 能连上，但这台主机没有开会话全文搜索。"""


class OrcaNotFound(OrcaError):
    """终端句柄失效或不存在。"""


def _join_wrapped(tail: List[str], rows: List[dict]) -> List[str]:
    """把终端按列宽软换行的行接回逻辑行。

    真实 CLI 的 --screen 输出里，tailRows[i].wrapped 为 True 表示第 i 行是上一行的延续；
    不接回的话，凭据可能被换行切开而躲过脱敏。上一行被剥掉的行尾空格（trailingSpaces）要补回。
    """
    out: List[str] = []
    last_spaces = 0
    for i, line in enumerate(tail):
        row = rows[i] if i < len(rows) and isinstance(rows[i], dict) else {}
        if row.get("wrapped") and out:
            out[-1] += " " * last_spaces + str(line)
        else:
            out.append(str(line))
        last_spaces = int(row.get("trailingSpaces") or 0)
    return out


def default_runner(timeout: float) -> Runner:
    def run(argv: List[str]) -> Tuple[int, str, str]:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, shell=False)
        return p.returncode, p.stdout, p.stderr
    return run


class OrcaClient:
    def __init__(self, environment: str, runner: Optional[Runner] = None, binary: str = "orca", timeout: float = 15.0):
        self._env, self._bin = environment, binary
        self._runner = runner or default_runner(timeout)

    # ---- 内部：执行并解析信封 ----
    def _call(self, args: List[str]) -> dict:
        argv = [self._bin, *args, "--environment", self._env, "--json"]
        try:
            _rc, out, _err = self._runner(argv)
        except subprocess.TimeoutExpired:
            raise OrcaUnavailable("orca_unavailable", "调用 Orca 超时") from None
        except FileNotFoundError:
            raise OrcaUnavailable("orca_unavailable", "找不到 Orca 命令行") from None
        try:
            envelope = json.loads(out)
        except (ValueError, TypeError):
            raise OrcaError("bad_output", "Orca 命令行返回了无法解析的输出") from None
        if not isinstance(envelope, dict):
            raise OrcaError("bad_output", "Orca 命令行返回了无法解析的输出")
        if not envelope.get("ok"):
            err = envelope.get("error") or {}
            code, msg = str(err.get("code", "unknown")), str(err.get("message", ""))
            if code in _UNAVAILABLE_CODES:
                raise OrcaUnavailable("orca_unavailable", "无法连接 Orca 运行时")
            if code in _NOT_FOUND_CODES:
                raise OrcaNotFound(code, "终端句柄已失效或不存在")
            raise OrcaError(code, msg)
        return envelope.get("result") or {}

    # ---- 公共只读方法 ----
    def list_terminals(self) -> List[dict]:
        return list(self._call(["terminal", "list"]).get("terminals") or [])

    def read_terminal(self, handle: str, limit: int = 200, screen: bool = True) -> dict:
        if not isinstance(handle, str) or not _HANDLE_RE.fullmatch(handle):
            raise ValueError("非法的终端句柄")
        if not 1 <= limit <= 2000:
            raise ValueError("limit 必须在 1..2000")
        args = ["terminal", "read", "--terminal", handle, "--limit", str(limit)]
        if screen:  # 读渲染后的屏幕：TUI 类 agent 的累计输出是重绘碎片，不可读
            args.append("--screen")
        t = dict(self._call(args).get("terminal") or {})
        t["tail"] = _join_wrapped(t.get("tail") or [], t.pop("tailRows", None) or [])
        return t

    def search(self, query: str, limit: int = 20) -> List[dict]:
        if not isinstance(query, str) or not query.strip() or len(query) > 500:
            raise ValueError("搜索词不能为空且不能超过 500 字符")
        if not 1 <= limit <= 100:
            raise ValueError("limit 必须在 1..100")
        res = self._call(["search", f"--query={query}", "--limit", str(limit)])
        if res.get("kind") == "unavailable":
            raise OrcaSearchDisabled("search_unavailable", "Orca 主机未开启会话全文搜索")
        return list(res.get("hits") or [])

    def agent_snapshot(self) -> dict:
        """监视器用：一次采集里的存活终端句柄 + 每个 agent 的钩子状态（方案 A，见 p2-detect-spike.md）。

        用 paneKey == tabId:leafId 把 worktree ps 的 agents[] 行对应到终端句柄；对不上的行忽略。
        返回 {"alive": [句柄...], "agents": [{handle,title,agent,state,state_since_ms,prompt,tool,tool_input}]}。
        """
        terms = self.list_terminals()
        by_pane = {f"{t.get('tabId')}:{t.get('leafId')}": t for t in terms}
        agents = []
        for w in self.worktree_ps():
            for a in w.get("agents") or []:
                t = by_pane.get(a.get("paneKey"))
                if not t or not t.get("handle"):
                    continue
                agents.append({
                    "handle": t["handle"], "title": t.get("title") or "", "agent": a.get("agentType") or t.get("agentIdentity"),
                    "state": a.get("state"), "state_since_ms": a.get("stateStartedAt"),
                    "prompt": a.get("prompt") or "", "tool": a.get("toolName"), "tool_input": a.get("toolInput"),
                })
        return {"alive": [t["handle"] for t in terms if t.get("handle")], "agents": agents}

    def send_text(self, handle: str, text: str) -> dict:
        """【写】向终端输入一段文本并回车。固定 argv；文本以 --text=<原文> 单个元素传入（实测以 - 开头也不会被当成选项）。

        调用方（ReplyService）负责先做危险规则判断；这里只做格式校验。文本必须是单行：换行会被终端当成多次回车，
        一条回复就能回答多个确认，所以含 \\n、\\r 的文本一律拒绝。
        """
        if not valid_handle(handle):
            raise ValueError("非法的终端句柄")
        if not isinstance(text, str) or not text.strip() or len(text) > 2000 or re.search("[\x00-\x08\x0a-\x1f\x7f\x85\u2028\u2029]", text):
            raise ValueError("非法的回复文本")
        return dict(self._call(["terminal", "send", "--terminal", handle, f"--text={text}", "--enter"]).get("send") or {})

    def worktree_ps(self) -> List[dict]:
        return list(self._call(["worktree", "ps"]).get("worktrees") or [])
