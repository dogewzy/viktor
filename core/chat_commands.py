"""聊天魔法指令解析：钉钉 / Agent 入口共用。"""

from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata

_CLEAR_ALIASES = frozenset(
    {
        "/clear",
        "/new",
        "/reset",
        "清空会话",
        "新开对话",
        "忘记之前",
        "开启新对话",
    }
)
_HELP_ALIASES = frozenset({"/help", "?", "？", "帮助"})
_RESUME_ALIASES = frozenset({"/resume", "/恢复", "继续执行"})

_CLEAR_ALIASES_LOWER = frozenset(s.lower() for s in _CLEAR_ALIASES)
_HELP_ALIASES_LOWER = frozenset(s.lower() for s in _HELP_ALIASES)
_RESUME_ALIASES_LOWER = frozenset(s.lower() for s in _RESUME_ALIASES)

_WEEK_REPORT_PREFIX = re.compile(r"^/week_report\b", re.IGNORECASE)
_WEEK_REPORT_FULL = re.compile(r"^/week_report\s+(\S+)\s*$", re.IGNORECASE)
_REPORT_BUG_PREFIX = re.compile(r"^/report_bug\b", re.IGNORECASE)
_SAMPLE_ID_SPLIT_RE = re.compile(r"[\s,，;；]+")


@dataclass(frozen=True)
class RepoScriptCommand:
    """确定性仓库脚本命令，绕过普通 Agent 澄清流程。"""

    name: str
    repo_connector_id: str
    script_path: str
    sample_ids: list[int]
    execute_args: list[str]


@dataclass(frozen=True)
class ReportBugCommand:
    """网页聊天 Bug 上报命令。"""

    description: str
    repo_connector_id: str = ""
    target_branch: str = ""
    no_coding: bool = False


_REPO_SCRIPT_COMMANDS: tuple[dict[str, object], ...] = ()

# 同一条消息里「指令前缀 + 正文」，如 /clear 接着问 Q2、或「开启新对话 查一下…」
_SLASH_TOPIC_PREFIX = re.compile(r"^(/clear|/new|/reset)\b\s*", re.IGNORECASE | re.DOTALL)
_CN_TOPIC_PREFIX = re.compile(
    r"^(清空会话|新开对话|忘记之前|开启新对话)\s*",
    re.DOTALL,
)


def normalize_user_command_text(text: str) -> str:
    """去掉零宽字符、全角斜杠、前置 @昵称 等，便于识别 /clear。"""
    t = (text or "").replace("\ufeff", "").replace("\u200b", "")
    t = unicodedata.normalize("NFC", t).strip()
    t = t.replace("／", "/")
    # 钉钉群内 @机器人 常拼在正文前；允许多段 @xxx
    t = re.sub(r"^(\s*@[^\s\u200b]+\s*)+", "", t)
    return t.strip()


def match_magic_command(text: str) -> str | None:
    """仅在归一化后与别名完全相等时触发，大小写不敏感。"""
    t = (text or "").strip().lower()
    if not t:
        return None
    if t in _CLEAR_ALIASES_LOWER:
        return "clear"
    if t in _HELP_ALIASES_LOWER:
        return "help"
    if t in _RESUME_ALIASES_LOWER:
        return "resume"
    return None


def parse_week_report_username(text: str) -> tuple[bool, str | None]:
    """解析 `/week_report <GitLab用户名>`（最近七天代码提交周报）。

    返回 `(是否为 week_report 指令, 用户名)`：
    - 非该指令 → `(False, None)`
    - 指令缺少用户名 → `(True, None)`（调用方应提示用法）
    - 否则 → `(True, username)`，支持 `{name}` 形式占位。
    """
    raw = (text or "").strip()
    if not _WEEK_REPORT_PREFIX.match(raw):
        return False, None
    m = _WEEK_REPORT_FULL.match(raw)
    if not m:
        return True, None
    u = m.group(1).strip()
    if u.startswith("{") and u.endswith("}"):
        u = u[1:-1].strip()
    return True, (u if u else None)


def parse_report_bug_command(text: str) -> tuple[ReportBugCommand | None, str | None]:
    """解析 `/report_bug [--repo id] [--branch name] [--no-coding] <描述>`。"""
    raw = normalize_user_command_text(text)
    if not _REPORT_BUG_PREFIX.match(raw):
        return None, None
    rest = _REPORT_BUG_PREFIX.sub("", raw, count=1).strip()
    if not rest:
        return None, "请在命令后描述 Bug，例如：/report_bug 复现步骤：... 实际结果：... 期望结果：..."

    repo_connector_id = ""
    target_branch = ""
    no_coding = False
    tokens = rest.split()
    consumed = 0
    while consumed < len(tokens):
        token = tokens[consumed]
        if token == "--no-coding":
            no_coding = True
            consumed += 1
            continue
        if token == "--repo" and consumed + 1 < len(tokens):
            repo_connector_id = tokens[consumed + 1].strip()
            consumed += 2
            continue
        if token.startswith("--repo="):
            repo_connector_id = token.split("=", 1)[1].strip()
            consumed += 1
            continue
        if token == "--branch" and consumed + 1 < len(tokens):
            target_branch = tokens[consumed + 1].strip()
            consumed += 2
            continue
        if token.startswith("--branch="):
            target_branch = token.split("=", 1)[1].strip()
            consumed += 1
            continue
        break
    description = " ".join(tokens[consumed:]).strip()
    if not description:
        return None, "请在 /report_bug 后填写 Bug 描述"
    return ReportBugCommand(
        description=description,
        repo_connector_id=repo_connector_id,
        target_branch=target_branch,
        no_coding=no_coding,
    ), None


def _parse_positive_int_tokens(raw: str) -> list[int]:
    tokens = [item for item in _SAMPLE_ID_SPLIT_RE.split((raw or "").strip()) if item]
    if not tokens:
        raise ValueError("请在命令后提供至少一个 sample_id")

    invalid = [token for token in tokens if not token.isdigit() or int(token) <= 0]
    if invalid:
        raise ValueError(f"sample_id 只能是正整数，非法值: {', '.join(invalid)}")

    result: list[int] = []
    seen: set[int] = set()
    for token in tokens:
        sample_id = int(token)
        if sample_id in seen:
            continue
        seen.add(sample_id)
        result.append(sample_id)
    return result


def parse_repo_script_command(text: str) -> tuple[RepoScriptCommand | None, str | None]:
    """解析需要后端确定性执行的仓库脚本命令。

    返回 `(command, error)`：
    - 非命令：`(None, None)`
    - 命令格式错误：`(None, error)`
    - 解析成功：`(RepoScriptCommand, None)`
    """
    raw = normalize_user_command_text(text)
    if not raw.startswith("/"):
        return None, None

    for spec in _REPO_SCRIPT_COMMANDS:
        for prefix in spec["prefixes"]:
            if raw == prefix:
                try:
                    _parse_positive_int_tokens("")
                except ValueError as e:
                    return None, str(e)
            if not raw.startswith(prefix):
                continue
            rest = raw[len(prefix):].strip()
            if rest and not rest[0].isdigit():
                continue
            try:
                sample_ids = _parse_positive_int_tokens(rest)
            except ValueError as e:
                return None, str(e)
            return RepoScriptCommand(
                name=str(spec["name"]),
                repo_connector_id=str(spec["repo_connector_id"]),
                script_path=str(spec["script_path"]),
                sample_ids=sample_ids,
                execute_args=list(spec["execute_args"]),
            ), None

    return None, None


def strip_new_topic_prefix(text: str) -> tuple[bool, str]:
    """识别「新开议题」指令前缀。

    - 整句仅为 /clear 等别名时 → (True, 空串)；
    - `/clear Q2` / `开启新对话 Q2` → (True, 剩余正文)；
    - 无此前缀 → (False, 原文 strip 后)。
    """
    raw = (text or "").strip()
    if not raw:
        return False, raw
    if match_magic_command(raw) == "clear":
        return True, ""
    m = _SLASH_TOPIC_PREFIX.match(raw)
    if m:
        rest = raw[m.end():].strip()
        return True, rest
    m = _CN_TOPIC_PREFIX.match(raw)
    if m:
        rest = raw[m.end():].strip()
        return True, rest
    return False, raw
