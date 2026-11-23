"""危险规则与禁止规则（设计 6.2、6.3）：纯函数，确定性判断，不靠模型。

- check(*texts)：对"发往会话的回复文本"和"会话正在请求确认的内容"一起判断；
  先去 ANSI/控制字符、归一空白、转小写，再逐条匹配，任一段命中即可。拿不准宁可误报（走审批）。
- forbidden（审批也不放行）优先于 dangerous（需要审批码）。
- 新增形式只需往 _RULES / _FORBIDDEN 里加一项。
"""
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
        r"\bgit\s+push\s+(?:\S+\s+)*(?:-f|--force(?:-with-lease)?(?:=\S+)?)(?:\s|$)",
        r"\bgit\s+reset\s+(?:\S+\s+)*--hard\b", r"\bgit\s+(?:filter-branch|filter-repo)\b", r"\bgit\s+reflog\s+expire\b",
    ),
    "sql-write": _c(
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


_REPLY_BAD = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def sanitize_reply(text: str, max_len: int = 500) -> str:
    """发往会话的回复文本净化：去 ANSI；含控制字符、空白、超长则拒绝（ValueError）。"""
    if not isinstance(text, str):
        raise ValueError("回复必须是文本")
    text = strip_ansi(text)
    if _REPLY_BAD.search(text):
        raise ValueError("回复里不能有控制字符")
    text = text.strip()
    if not text:
        raise ValueError("回复不能为空")
    if len(text) > max_len:
        raise ValueError(f"回复不能超过 {max_len} 字符")
    return text
