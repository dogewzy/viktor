"""Coding Agent task policy：限制后台任务的写路径、命令和高风险操作。"""
from __future__ import annotations

import fnmatch
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from settings import coding_agent_config


DEFAULT_DENY_PATHS = [
    ".env",
    ".env.*",
    "**/.env",
    "**/.env.*",
    "secrets/**",
    "config/prod/**",
    "conf.prod.*",
    ".git/**",
]

DEFAULT_WRITE_PATHS = ["**"]

# 只读探索命令：agent 在 plan/执行前需要看代码、看 git 状态、查目录。
# 这些命令始终被允许（即使调用方给了更严格的 allowed_commands），
# 因为它们不写文件、不改状态；写/危险操作仍由分段校验和 deny 规则拦截。
DEFAULT_READONLY_COMMANDS = [
    "ls", "pwd", "cat", "head", "tail", "wc", "stat", "file", "tree", "basename", "dirname",
    "find", "grep", "egrep", "fgrep", "rg", "which", "echo", "env", "true", "date",
    "git status", "git log", "git diff", "git show", "git branch", "git remote",
    "git rev-parse", "git ls-files", "git tag", "git describe", "git config --get",
    "git --no-pager log", "git --no-pager diff", "git --no-pager show",
]

DEFAULT_BUILD_TEST_COMMANDS = [
    "npm test",
    "npm run test",
    "npm run lint",
    "npm run typecheck",
    "npm run build",
    "pnpm test",
    "pnpm lint",
    "pnpm build",
    "pytest",
    "python -m pytest",
    "ruff check",
    "go test",
    "mvn test",
    "gradle test",
    "./gradlew test",
]

DEFAULT_ALLOWED_COMMANDS = [*DEFAULT_READONLY_COMMANDS, *DEFAULT_BUILD_TEST_COMMANDS]


@dataclass
class CodingPolicy:
    write_paths: list[str] = field(default_factory=lambda: list(DEFAULT_WRITE_PATHS))
    deny_paths: list[str] = field(default_factory=lambda: list(DEFAULT_DENY_PATHS))
    allowed_commands: list[str] = field(default_factory=lambda: list(DEFAULT_ALLOWED_COMMANDS))
    allow_dependency_change: bool = False
    allow_schema_change: bool = False
    allow_ci_change: bool = False
    allow_delete_files: bool = False
    allow_push_branch: bool = False
    allow_create_mr: bool = False
    max_runtime_minutes: int = 60
    max_changed_files: int = 30
    max_diff_lines: int = 3000
    command_timeout_sec: int = coding_agent_config.command_timeout_sec

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "CodingPolicy":
        base = cls()
        data = raw or {}
        for key in cls.__dataclass_fields__:
            if key in data and data[key] is not None:
                setattr(base, key, data[key])
        base.write_paths = list(base.write_paths or DEFAULT_WRITE_PATHS)
        base.deny_paths = list(base.deny_paths or DEFAULT_DENY_PATHS)
        base.allowed_commands = list(base.allowed_commands or DEFAULT_ALLOWED_COMMANDS)
        # 只读探索命令始终并入：保证 agent 永远能 ls / git status / find 等，
        # 即使调用方传了更窄的 allowed_commands。
        for cmd in DEFAULT_READONLY_COMMANDS:
            if cmd not in base.allowed_commands:
                base.allowed_commands.append(cmd)
        return base

    def to_dict(self) -> dict[str, Any]:
        return {
            "write_paths": self.write_paths,
            "deny_paths": self.deny_paths,
            "allowed_commands": self.allowed_commands,
            "allow_dependency_change": self.allow_dependency_change,
            "allow_schema_change": self.allow_schema_change,
            "allow_ci_change": self.allow_ci_change,
            "allow_delete_files": self.allow_delete_files,
            "allow_push_branch": self.allow_push_branch,
            "allow_create_mr": self.allow_create_mr,
            "max_runtime_minutes": self.max_runtime_minutes,
            "max_changed_files": self.max_changed_files,
            "max_diff_lines": self.max_diff_lines,
            "command_timeout_sec": self.command_timeout_sec,
        }

    def check_read_path(self, path: str) -> None:
        self._check_path_syntax(path)

    def check_write_path(self, path: str, *, deleting: bool = False) -> None:
        self._check_path_syntax(path)
        norm = _norm(path)
        if deleting and not self.allow_delete_files:
            raise PermissionError("policy 禁止删除文件")
        if any(_match(norm, pat) for pat in self.deny_paths):
            raise PermissionError(f"policy 禁止修改路径: {path}")
        if not any(_match(norm, pat) for pat in self.write_paths):
            raise PermissionError(f"路径不在 write_paths 允许范围内: {path}")

    def check_command(self, command: str) -> None:
        text = (command or "").strip()
        if not text:
            raise PermissionError("命令不能为空")
        if _looks_dangerous(text):
            raise PermissionError(f"policy 拒绝高风险命令: {text}")
        if _has_command_substitution(text):
            raise PermissionError(f"policy 拒绝命令替换/子 shell: {text}")
        try:
            tokens = shlex.split(text)
        except ValueError as e:
            raise PermissionError(f"命令解析失败: {e}")
        if _has_write_redirect(tokens):
            raise PermissionError(f"policy 拒绝写文件重定向（请用写文件工具）: {text}")
        # 命令可能用 | && || ; 串联多个子命令；逐段校验，任一段不允许即拒绝，
        # 避免 `ls && rm -rf x` 这类靠白名单前缀蒙混过关。
        segments = _split_segments(tokens)
        if not segments:
            raise PermissionError("命令不能为空")
        for seg in segments:
            if not any(_segment_allowed(seg, allowed) for allowed in self.allowed_commands):
                raise PermissionError(f"命令不在 allowed_commands 中: {' '.join(seg)}")

    @staticmethod
    def _check_path_syntax(path: str) -> None:
        raw = (path or "").strip()
        if not raw:
            raise PermissionError("路径不能为空")
        if raw.startswith("/") or "\x00" in raw:
            raise PermissionError("路径必须是 workspace 内相对路径")
        parts = Path(raw).parts
        if any(part == ".." for part in parts):
            raise PermissionError("路径不能包含目录穿越")


def _norm(path: str) -> str:
    return str(Path(path.strip()).as_posix()).lstrip("./")


def _match(path: str, pattern: str) -> bool:
    pat = _norm(pattern)
    return fnmatch.fnmatch(path, pat) or fnmatch.fnmatch(path, pat.lstrip("**/"))


_SHELL_OPERATORS = {"|", "||", "&&", ";", "&", "\n"}
_REDIRECT_STANDALONE_RE = re.compile(r"^\d*>>?$")        # '>' '>>' '2>' '1>>'
_REDIRECT_INLINE_RE = re.compile(r"^\d*>>?(\S+)$")       # '>file' '2>/dev/null' '2>&1'


def _has_command_substitution(command: str) -> bool:
    return "$(" in command or "`" in command or "<(" in command or ">(" in command


def _redirect_target_ok(target: str) -> bool:
    t = (target or "").strip()
    if not t:
        return True
    if t.startswith("&"):  # 2>&1 之类，合并 fd，不写文件
        return True
    return t in {"/dev/null", "/dev/stdout", "/dev/stderr"}


def _has_write_redirect(tokens: list[str]) -> bool:
    """检测把输出写到真实文件的重定向（> >> 1> 2>file 等），但放过 2>/dev/null、2>&1。"""
    for i, tok in enumerate(tokens):
        if _REDIRECT_STANDALONE_RE.match(tok):
            target = tokens[i + 1] if i + 1 < len(tokens) else ""
            if not _redirect_target_ok(target):
                return True
            continue
        inline = _REDIRECT_INLINE_RE.match(tok)
        if inline and not _redirect_target_ok(inline.group(1)):
            return True
    return False


def _split_segments(tokens: list[str]) -> list[list[str]]:
    """按 shell 控制符把 token 流切成多个子命令段。

    用 shlex 分词后再按算子切分：引号内的 | ; 等不会被当成算子（已并入 token），
    因此 grep "a|b" 不会被误切；而未加引号的 ls && rm 会被切成两段分别校验。
    """
    segments: list[list[str]] = []
    cur: list[str] = []
    for tok in tokens:
        if tok in _SHELL_OPERATORS:
            if cur:
                segments.append(cur)
                cur = []
        else:
            cur.append(tok)
    if cur:
        segments.append(cur)
    return segments


def _segment_allowed(seg_tokens: list[str], allowed: str) -> bool:
    if not seg_tokens:
        return True
    cleaned = [
        t for t in seg_tokens
        if not _REDIRECT_STANDALONE_RE.match(t) and not _REDIRECT_INLINE_RE.match(t)
    ]
    cmd = " ".join(cleaned)
    base = " ".join(shlex.split(allowed))
    return cmd == base or cmd.startswith(base + " ")


def _looks_dangerous(command: str) -> bool:
    lowered = command.lower()
    dangerous = [
        "rm -rf /",
        "rm -rf .",
        "git reset --hard",
        "git clean -fd",
        "git push --force",
        "sudo ",
        "chmod 777",
        "mkfs",
        ":(){",
    ]
    return any(item in lowered for item in dangerous)
