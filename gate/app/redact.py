"""输出关卡：网关所有对外文本必须经过 sanitize()（或 redact_json()）。

顺序固定：去 ANSI -> 脱敏 -> 截断。
- 先去 ANSI：转义序列可能把凭据从中间切开，导致正则漏掉；
- 先脱敏后截断：截断边界落在凭据中间时，不会漏出半截。
新增凭据形式只需在 _RULES 里加一行。
"""
import re
from typing import Any, Optional, Tuple

REDACTED = "[REDACTED]"

# ---------- 去 ANSI ----------
_ANSI_RE = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"      # OSC（标题、超链接），以 BEL 或 ST 结束
    r"|\x1b\[[0-?]*[ -/]*[@-~]"                  # CSI（颜色、光标）
    r"|\x1b[()*+][0-9A-Za-z]"                    # 字符集选择
    r"|\x1b[@-Z\\-_=>]"                          # 其余两字节 ESC 序列
    # 命令行回显里的字面写法（反斜杠文本，如 printf 的 \033[0m），同样会把凭据切开
    r"|\\(?:033|x1[bB]|e|u001[bB])\[[0-?]*[ -/]*[@-~]"
)


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


# ---------- 脱敏规则 ----------
# 敏感键名：含 secret/token/password/passwd/pwd/api_key 等（不区分大小写）；
# 另外全大写的环境变量名含 KEY 也算（避免误伤 monkey、key 参数之类普通词）。
_SENSITIVE_WORD = (
    r"(?i:secret|token|passw(?:or)?d|passwd|pwd|api[_-]?key|access[_-]?key|private[_-]?key|credential|auth[_-]?key)"
)
_KEY_NAME = rf"(?:[A-Za-z0-9_.-]*{_SENSITIVE_WORD}[A-Za-z0-9_.-]*|[A-Z][A-Z0-9_]*KEY[A-Z0-9_]*)"
# value：带引号的取到闭合引号，否则取到空白/引号/逗号/括号为止
_VALUE = r"""(?:\[REDACTED\]|"[^"\n]*"|'[^'\n]*'|[^\s"',;)\]}]+)"""

_RULES = [
    ("pem", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S), REDACTED),
    ("conn_string", re.compile(r"(\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s:/@]+:)[^\s@/]+(@)"), rf"\1{REDACTED}\2"),
    ("bearer", re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"), rf"\1{REDACTED}"),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}"), REDACTED),
    ("telegram_bot", re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"), REDACTED),
    ("zhipu_key", re.compile(r"\b[0-9a-f]{32}\.[A-Za-z0-9]{16}\b"), REDACTED),
    ("sk_key", re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"), REDACTED),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), REDACTED),
    ("aws_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), REDACTED),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), REDACTED),
    # key=value / key: value / "key": "value"（键名保留，值遮住）
    ("kv", re.compile(rf"""(?P<k>(["']?){_KEY_NAME}\2\s*[:=]\s*)(?:{_VALUE})"""), rf"\g<k>{REDACTED}"),
    # 中文标注：密码是 xxx / 令牌：xxx / 密钥=xxx
    ("zh_label", re.compile(r"((?:密码|口令|令牌|密钥|秘钥|私钥|访问码)\s*(?:是|为|:|：|=|＝)\s*)[^\s,，。;；、)）]+"), rf"\1{REDACTED}"),
]

_SENSITIVE_KEY_RE = re.compile(rf"^{_KEY_NAME}$")


def redact(text: str) -> str:
    for _name, pattern, repl in _RULES:
        text = pattern.sub(repl, text)
    return text


def redact_json(value: Any) -> Any:
    """递归脱敏 dict/list 中所有字符串；键保留；敏感键名下的字符串值整体遮住。不修改入参。"""
    if isinstance(value, str):
        return redact(strip_ansi(value))
    if isinstance(value, list):
        return [redact_json(v) for v in value]
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if isinstance(v, str) and isinstance(k, str) and _SENSITIVE_KEY_RE.match(k):
                out[k] = REDACTED
            else:
                out[k] = redact_json(v)
        return out
    return value


def sanitize(text: str, limit: Optional[int] = None, keep: str = "head") -> Tuple[str, bool]:
    """去 ANSI -> 脱敏 -> 截断；返回 (文本, 是否被截断)。

    keep="head" 保留开头，keep="tail" 保留结尾（读会话输出时最近的内容更重要）。
    """
    if keep not in ("head", "tail"):
        raise ValueError("keep 只能是 head 或 tail")
    text = redact(strip_ansi(text))
    if limit is not None and len(text) > limit:
        return (text[:limit] if keep == "head" else text[len(text) - limit:]), True
    return text, False
