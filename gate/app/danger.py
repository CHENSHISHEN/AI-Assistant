"""危险规则与禁止规则（设计 6.2、6.3）：纯函数，确定性判断，不靠模型。

- check(*texts)：对"发往会话的回复文本"和"会话正在请求确认的内容"一起判断；
  先去 ANSI/控制字符、归一空白、转小写，再逐条匹配，任一段命中即可。拿不准宁可误报（走审批）。
- forbidden（审批也不放行）优先于 dangerous（需要审批码）。
- 新增形式只需往 _RULES / _FORBIDDEN 里加一项。
"""
import json
import re
from dataclasses import dataclass
from typing import Dict, List, Pattern, Tuple

from .redact import strip_ansi

_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")
_WS_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class Verdict:
    level: str                      # "ok" | "dangerous" | "forbidden"
    rules: Tuple[str, ...]


def _c(*patterns: str) -> List[Pattern]:
    return [re.compile(p) for p in patterns]


_RM_FLAG = r"(?:-[a-z]*[rf][a-z]*|--recursive|--force|--no-preserve-root)"
_CRED_PATH = (r"(?:^|[\s/='\"])(?:\.env(?:\.[\w-]+)?|credentials(?:\.[\w-]+)?|id_(?:rsa|ed25519|ecdsa)|[\w.-]+\.pem"
              r"|\.ssh/|secrets?/|\.netrc|\.npmrc|\.pypirc|\.aws/)")
_WRITE_VERB = r"(?:>>?|\btee\b|\bsed\s+-i|\brm\b|\bmv\b|\bcp\b|\bchmod\b|\bchown\b|\bshred\b|\btruncate\b|\bdd\b)"

# 规则名 -> 模式列表（任一命中）
_RULES: Dict[str, List[Pattern]] = {
    "delete": _c(
        rf"\brm\s+(?:\S+\s+)*?{_RM_FLAG}(?:\s|$)",
        r"\bgit\s+clean\b", r"\bgit\s+branch\s+(?:\S+\s+)*-d\b", r"\bgit\s+worktree\s+(?:remove|prune)\b",
        r"\bgit\s+push\s+(?:\S+\s+)*--delete\b", r"\bfind\b[^;|&]*\s-delete\b", r"\bshred\b",
    ),
    "rewrite-history": _c(
        r"\bgit\s+push\s+(?:\S+\s+)*(?:-f|--force(?:-with-lease)?(?:=\S+)?|--mirror)(?:\s|$)",
        r"\bgit\s+push\s+(?:\S+\s+)*\+\S",                       # git push origin +main（以 + 开头的 refspec 即强推）
        r"\bgit\s+reset\s+(?:\S+\s+)*--hard\b", r"\bgit\s+(?:filter-branch|filter-repo)\b", r"\bgit\s+reflog\s+expire\b",
    ),
    "discard-changes": _c(
        r"\bgit\s+checkout\s+(?:\S+\s+)*--(?:\s|$)", r"\bgit\s+restore\b",      # 丢弃工作区改动
    ),
    "sql-write": _c(
        r"\bupdate\s+[\w.\"`\[\]]+\s+set\b",
        r"\binsert\s+(?:or\s+\w+\s+)?into\s+[\w.\"`\[\]]+\s*(?:\(|values\b|select\b|set\b|default\b)",
        r"\bdrop\s+(?:table|database|schema|index|view|user|role|column|trigger|function|procedure|sequence|materialized|if)\b",
        r"\btruncate\s+table\b", r"\btruncate\s+[\w.\"`]+\s*;", r"\bdelete\s+from\b",
        r"\balter\s+(?:table|database|schema|user|role|system|index|view)\b",
    ),
    "permissions": _c(
        r"\b(?:chmod|chown|chgrp)\s+(?:\S+\s+)*?(?:-[a-z]*r[a-z]*|--recursive)(?:\s|$)", r"\bsudo\b",
    ),
    "remote-script": _c(
        r"\b(?:curl|wget)\b[^|;&\n]*\|\s*(?:sudo\s+)?(?:ba|z|da|k)?sh\b",
        r"\b(?:ba|z)?sh\s+<\(\s*(?:curl|wget)", r"\bsource\s+<\(\s*(?:curl|wget)", r"\beval\s+\"?\$\(\s*(?:curl|wget)",
    ),
    "service-container": _c(
        r"\bdocker\s+(?:container\s+|image\s+|volume\s+|network\s+|system\s+)?(?:rm|rmi|prune|kill|stop)\b",
        r"\bdocker[- ]compose\s+(?:\S+\s+)*?(?:down|rm|stop|kill)\b",
        r"\bsystemctl\s+(?:--\S+\s+)*(?:stop|disable|mask|kill|reset-failed)\b", r"\bservice\s+\S+\s+stop\b",
        r"\bkill\s+(?:-9|-kill|-sigkill)\b", r"\b(?:pkill|killall)\b",
    ),
    "disk": _c(r"\bdd\s+(?:\S+\s+)*?if=", r"\bmkfs(?:\.\w+)?\b", r"\b(?:fdisk|parted|wipefs|sfdisk)\b", r">\s*/dev/(?:sd|nvme|vd|hd)"),
    "orca-admin": _c(
        r"\borca\s+worktree\s+rm\b", r"\borca\s+terminal\s+close\b", r"\borca\s+automations?\s+(?:remove|rm|delete)\b",
    ),
    "power": _c(
        r"\b(?:reboot|poweroff)\b", r"\bshutdown\s+(?:-\S+|now|\+\d+)", r"\bhalt\s*(?:-\S+|$)", r"\binit\s+0\b",
        r":\(\)\s*\{\s*:\|:&\s*\}\s*;\s*:",
    ),
}
# 需要"凭据路径 + 写动词"同时出现
_CREDENTIAL = (re.compile(_CRED_PATH), re.compile(_WRITE_VERB))

_FORBIDDEN: List[Pattern] = _c(
    r"\borca\s+computer\b", r"\borca\s+skills\s+share\b", r"\borca\s+artifacts?\s+share\b",
    r"\borca\s+account\s+\w+", r"\borca\s+environment\s+\w+",
)


def _normalize(text: str) -> str:
    text = strip_ansi(text)
    text = _CTRL_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip().lower()


def check(*texts) -> Verdict:
    rules: List[str] = []
    forbidden = False
    for raw in texts:
        if not isinstance(raw, str) or not raw:
            continue
        t = _normalize(raw)
        if any(p.search(t) for p in _FORBIDDEN):
            forbidden = True
            if "forbidden-orca" not in rules:
                rules.append("forbidden-orca")
        for name, patterns in _RULES.items():
            if name not in rules and any(p.search(t) for p in patterns):
                rules.append(name)
        if "credential-file" not in rules and all(p.search(t) for p in _CREDENTIAL):
            rules.append("credential-file")
    if forbidden:
        return Verdict("forbidden", tuple(rules))
    return Verdict("dangerous" if rules else "ok", tuple(rules))


# 换行（\n、\r 及 Unicode 行分隔符）会被终端当成多次回车，一条回复就能回答多个确认，所以一律拒绝
def as_text(value) -> str:
    """把 agent 状态里的请求内容变成可判断的文本：字符串原样；dict/list 先 json.dumps；其他类型 str()；None 为空。

    转换失败（循环引用等）抛 ValueError，由调用方按 dangerous 处理。
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        if isinstance(value, (dict, list, tuple)):
            return json.dumps(value, ensure_ascii=False, default=str)
        return str(value)
    except Exception as e:  # noqa: BLE001
        raise ValueError("无法转换成文本") from e


def _leaves(value) -> List[str]:
    if isinstance(value, dict):
        return [x for v in value.values() for x in _leaves(v)]
    if isinstance(value, (list, tuple)):
        return [x for v in value for x in _leaves(v)]
    return [] if value is None else [str(value)]


def as_check_text(value) -> str:
    """用于危险判断的文本：as_text 的结果，容器类再追加"所有叶子值用空格连起来"（argv 列表 ["git","push","-f"] 才能命中规则）。"""
    text = as_text(value)
    if isinstance(value, (dict, list, tuple)):
        text += "\n" + " ".join(_leaves(value))
    return text


def check_context(reply: str, tool_input=None, prompt=None, screen=None, tool=None) -> Verdict:
    """对"回复 + 会话正在请求确认的内容"一起判断（设计 6.2）。

    - forbidden 只看**回复文本和 tool_input**：这两处出现 orca 的禁止命令才是助手要去执行/批准的；
    - prompt、屏幕尾部、工具名里出现这些字样（比如 agent 在讲解、或历史输出里有）最多升到 dangerous，走审批，不会让会话永久无法回复；
    - tool_input 之外任何一处读不到（screen 为 None 表示读取失败）按 dangerous 处理（rule: unreadable-context）。
    """
    rules: List[str] = []
    try:
        ti = as_check_text(tool_input)
    except ValueError:
        ti = ""
        rules.append("unreadable-context")
    primary = check(reply, ti)
    if primary.level == "forbidden":
        return primary
    rules.extend(r for r in primary.rules if r not in rules)
    try:
        pr, tl = as_text(prompt), as_text(tool)
    except ValueError:
        pr = tl = ""
        rules.append("unreadable-context")
    if screen is None:
        rules.append("unreadable-context")
    secondary = check(pr, tl, screen if isinstance(screen, str) else "")
    for r in secondary.rules:
        r = "forbidden-orca-in-context" if r == "forbidden-orca" else r      # 只升级，不禁止
        if r not in rules:
            rules.append(r)
    return Verdict("dangerous" if rules else "ok", tuple(rules))


_REPLY_BAD = re.compile("[\x00-\x08\x0a-\x1f\x7f\x85\u2028\u2029]")


def sanitize_reply(text: str, max_len: int = 500) -> str:
    """发往会话的回复文本净化：去 ANSI；含换行/控制字符、空白、超长则拒绝（ValueError）。"""
    if not isinstance(text, str):
        raise ValueError("回复必须是文本")
    text = strip_ansi(text)
    if _REPLY_BAD.search(text):
        raise ValueError("回复必须是单行文本，不能含换行或控制字符")
    text = text.strip()
    if not text:
        raise ValueError("回复不能为空")
    if len(text) > max_len:
        raise ValueError(f"回复不能超过 {max_len} 字符")
    return text
